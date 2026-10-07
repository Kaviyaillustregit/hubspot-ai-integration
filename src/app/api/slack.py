import json
import logging
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.agent.schemas import AgentRequest
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.factory import create_ai_provider
from app.ai.service import AIService
from app.api.errors import AppError
from app.api.hubspot_companies import get_companies_service
from app.api.hubspot_contacts import get_contacts_service
from app.core.config import Settings
from app.core.secrets import SecretCipher
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.associations import HubSpotAssociationsClient
from app.integrations.hubspot.deals import HubSpotDealsClient
from app.integrations.hubspot.oauth import HubSpotOAuthTokenClient
from app.integrations.slack.client import SlackClient
from app.integrations.slack.events import (
    SlackRequestVerifier,
    SlackSignatureError,
    SlackTenantResolver,
    parse_app_home_opened,
    parse_message,
)
from app.integrations.slack.home import (
    build_home_view,
    parse_home_interaction,
)
from app.integrations.slack.http_client import SlackWebApiClient
from app.repositories.hubspot_oauth import HubSpotOAuthRepository
from app.services.action_safety import ActionSafetyService
from app.services.hubspot_associations import HubSpotAssociationsService
from app.services.hubspot_contacts import HubSpotAccessTokenProvider
from app.services.hubspot_deals import HubSpotDealsService
from app.web.session import LINK_TTL_SECONDS, WebIdentity, WebSessionSigner

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/slack", tags=["slack"])


def _token_provider(request: Request) -> HubSpotAccessTokenProvider:
    settings = request.app.state.settings
    if not settings.hubspot_token_encryption_key:
        raise AppError("oauth_not_configured", "HubSpot OAuth is not configured", 503)
    return HubSpotAccessTokenProvider(
        request.app.state.session_factory,
        HubSpotOAuthTokenClient(settings),
        SecretCipher(settings.hubspot_token_encryption_key),
        HubSpotOAuthRepository,
    )


def get_agent(request: Request) -> AccountIntelligenceAgent:
    settings = request.app.state.settings
    token_provider = _token_provider(request)
    tools = HubSpotToolRegistry(
        get_companies_service(request),
        get_contacts_service(request),
        HubSpotDealsService(HubSpotDealsClient(settings), token_provider),
        HubSpotAssociationsService(HubSpotAssociationsClient(settings), token_provider),
    )

    action_safety = ActionSafetyService(
        request.app.state.session_factory,
    )

    return AccountIntelligenceAgent(
        tools,
        AIService(create_ai_provider(request.app.state.settings)),
        action_safety,
    )


def get_slack_client(request: Request) -> SlackClient:
    return SlackWebApiClient(request.app.state.settings)


def get_action_safety(request: Request) -> ActionSafetyService:
    return ActionSafetyService(request.app.state.session_factory)


def web_assistant_link(settings: Settings, tenant_id: str, user_id: str) -> str | None:
    """A short-lived link that signs this Slack user into the web assistant."""
    if not settings.web_app_base_url or not settings.slack_signing_secret:
        return None
    token = WebSessionSigner(settings.slack_signing_secret).issue(
        WebIdentity(tenant_id=tenant_id, user_id=user_id),
        purpose="link",
        ttl_seconds=LINK_TTL_SECONDS,
    )
    return f"{settings.web_app_base_url.rstrip('/')}/assistant?token={token}"


async def _respond(
    agent: AccountIntelligenceAgent,
    client: SlackClient,
    message: object,
    tenant_id: str,
    request_id: str,
) -> None:
    parsed = parse_message(message)
    if parsed is None:
        return

    response = await agent.respond(
        AgentRequest(
            tenant_id=tenant_id,
            actor_id=parsed.user_id,
            message=parsed.text,
            request_id=request_id,
            channel_id=parsed.channel_id,
            message_ts=parsed.ts,
            event_id=parsed.event_id,
        )
    )

    if response.status == "duplicate_request":
        # Redelivery of a message that was already handled; the first delivery replied.
        logger.info(
            "Skipping reply for duplicate Slack delivery",
            extra={"tenant_id": tenant_id},
        )
        return

    try:
        await client.post_message(
            parsed.channel_id,
            response.text,
        )
    except IntegrationError:
        logger.error(
            "Slack response delivery failed",
            extra={"tenant_id": tenant_id},
        )


async def _publish_home(
    client: SlackClient,
    *,
    tenant_id: str,
    user_id: str,
    web_url: str | None = None,
) -> None:
    try:
        await client.publish_home_view(user_id, build_home_view(web_url=web_url))
    except IntegrationError as exc:
        logger.error(
            "Slack App Home publish failed",
            extra={"tenant_id": tenant_id, "error": str(exc)},
        )


@router.post("/interactions")
async def interactions(
    request: Request,
    background_tasks: BackgroundTasks,
    client: Annotated[SlackClient, Depends(get_slack_client)],
) -> Response:
    """Slack interactivity for the App Home tab.

    The Home tab is only a launch screen for the web assistant. Interactions from an older
    Home view (input, quick actions) simply redraw the launch screen; they never run CRM
    requests whose results the Home tab would no longer show.
    """
    settings = request.app.state.settings

    if not settings.slack_signing_secret:
        return JSONResponse(
            {"code": "slack_not_configured", "message": "Slack is not configured"},
            status_code=503,
        )

    raw_body = await request.body()

    try:
        SlackRequestVerifier(settings.slack_signing_secret).verify(request.headers, raw_body)
        payload = json.loads(parse_qs(raw_body.decode("utf-8"))["payload"][0])
    except (
        SlackSignatureError,
        UnicodeDecodeError,
        KeyError,
        IndexError,
        json.JSONDecodeError,
    ):
        return JSONResponse(
            {"code": "invalid_slack_request", "message": "Invalid Slack request"},
            status_code=401,
        )

    interaction = parse_home_interaction(payload)

    if interaction is None:
        return Response(status_code=200)

    tenant_id = SlackTenantResolver(settings.slack_team_tenant_map).resolve(interaction.team_id)

    if tenant_id is None:
        logger.warning("Slack workspace is not mapped to a tenant")
        return Response(status_code=200)

    background_tasks.add_task(
        _publish_home,
        client,
        tenant_id=tenant_id,
        user_id=interaction.user_id,
        web_url=web_assistant_link(settings, tenant_id, interaction.user_id),
    )

    return Response(status_code=200)


@router.post("/events")
async def events(
    request: Request,
    background_tasks: BackgroundTasks,
    agent: Annotated[
        AccountIntelligenceAgent,
        Depends(get_agent),
    ],
    client: Annotated[
        SlackClient,
        Depends(get_slack_client),
    ],
) -> Response:
    settings = request.app.state.settings

    if not settings.slack_signing_secret:
        return JSONResponse(
            {
                "code": "slack_not_configured",
                "message": "Slack is not configured",
            },
            status_code=503,
        )

    raw_body = await request.body()

    try:
        SlackRequestVerifier(
            settings.slack_signing_secret
        ).verify(
            request.headers,
            raw_body,
        )
        payload = json.loads(raw_body)
    except (
        SlackSignatureError,
        json.JSONDecodeError,
    ):
        return JSONResponse(
            {
                "code": "invalid_slack_request",
                "message": "Invalid Slack request",
            },
            status_code=401,
        )

    if (
        payload.get("type") == "url_verification"
        and isinstance(payload.get("challenge"), str)
    ):
        return JSONResponse(
            {"challenge": payload["challenge"]}
        )

    home_opened = parse_app_home_opened(payload)

    if home_opened is not None:
        home_tenant_id = SlackTenantResolver(
            settings.slack_team_tenant_map
        ).resolve(home_opened.team_id)

        if home_tenant_id is None:
            logger.warning("Slack workspace is not mapped to a tenant")
            return Response(status_code=200)

        background_tasks.add_task(
            _publish_home,
            client,
            tenant_id=home_tenant_id,
            user_id=home_opened.user_id,
            web_url=web_assistant_link(settings, home_tenant_id, home_opened.user_id),
        )
        return Response(status_code=200)

    parsed = parse_message(payload)

    if parsed is None:
        return Response(status_code=200)

    tenant_id = SlackTenantResolver(
        settings.slack_team_tenant_map
    ).resolve(parsed.team_id)

    if tenant_id is None:
        logger.warning(
            "Slack workspace is not mapped to a tenant"
        )
        return Response(status_code=200)

    background_tasks.add_task(
        _respond,
        agent,
        client,
        payload,
        tenant_id,
        request.state.request_id,
    )

    return Response(status_code=200)