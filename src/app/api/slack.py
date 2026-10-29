import json
import logging
from asyncio import Lock
from inspect import Parameter, signature
from time import perf_counter
from typing import Annotated
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.agent.schemas import AgentRequest, AgentResponse
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.factory import create_ai_provider
from app.ai.service import AIService
from app.api.errors import AppError
from app.api.hubspot_companies import get_companies_service
from app.api.hubspot_contacts import get_contacts_service
from app.core.secrets import SecretCipher
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.associations import HubSpotAssociationsClient
from app.integrations.hubspot.deals import HubSpotDealsClient
from app.integrations.hubspot.oauth import HubSpotOAuthTokenClient
from app.integrations.hubspot.owners import HubSpotOwnersClient
from app.integrations.hubspot.properties import HubSpotPropertiesClient
from app.integrations.slack.client import SlackClient
from app.integrations.slack.events import (
    SlackRequestVerifier,
    SlackSignatureError,
    SlackTenantResolver,
    parse_app_home_opened,
    parse_message,
)
from app.integrations.slack.home import (
    HomeInteraction,
    build_home_view,
    parse_home_interaction,
)
from app.integrations.slack.http_client import SlackWebApiClient
from app.integrations.slack.response_formatting import format_slack_response
from app.integrations.slack.welcome_banner import build_welcome_banner_url
from app.repositories.hubspot_oauth import HubSpotOAuthRepository
from app.services.action_safety import ActionSafetyService
from app.services.hubspot_associations import HubSpotAssociationsService
from app.services.hubspot_contacts import HubSpotAccessTokenProvider
from app.services.hubspot_deals import HubSpotDealsService
from app.services.hubspot_owners import HubSpotOwnersService
from app.services.hubspot_properties import HubSpotPropertiesService
from app.services.slack_authorized_users import SlackAuthorizationService
from app.services.slack_conversation import SlackConversationService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/slack", tags=["slack"])


def _log_timing(operation: str, started: float, tenant_id: str) -> None:
    logger.info(
        "Slack Home timing",
        extra={
            "operation": operation,
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
            "tenant_id": tenant_id,
        },
    )


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
        max_read_pages=settings.hubspot_read_max_pages,
        properties=HubSpotPropertiesService(HubSpotPropertiesClient(settings), token_provider),
        owners=HubSpotOwnersService(HubSpotOwnersClient(settings), token_provider),
    )

    action_safety = ActionSafetyService(
        request.app.state.session_factory,
    )

    return AccountIntelligenceAgent(
        tools,
        AIService(create_ai_provider(request.app.state.settings)),
        action_safety,
        deal_follow_up_batch_size=settings.deal_follow_up_batch_size,
        lead_analysis_limit=settings.lead_analysis_limit,
        lead_ai_batch_size=settings.lead_ai_batch_size,
    )


def get_slack_client(request: Request) -> SlackClient:
    return SlackWebApiClient(request.app.state.settings)


def get_action_safety(request: Request) -> ActionSafetyService:
    return ActionSafetyService(request.app.state.session_factory)


def get_conversation_history(request: Request) -> SlackConversationService:
    return SlackConversationService(request.app.state.session_factory)


def get_slack_authorization(request: Request) -> SlackAuthorizationService:
    return SlackAuthorizationService(request.app.state.session_factory)


async def _is_authorized_slack_user(
    authorization: SlackAuthorizationService,
    *,
    tenant_id: str,
    team_id: str,
    user_id: str,
) -> bool:
    try:
        return await authorization.is_authorized(
            tenant_id=tenant_id,
            slack_team_id=team_id,
            slack_user_id=user_id,
        )
    except Exception as exc:
        logger.warning(
            "Slack user authorization lookup failed; request denied: %s: %s",
            type(exc).__name__,
            exc,
            extra={
                "tenant_id": tenant_id,
                "error_type": type(exc).__name__,
                "error_detail": str(exc),
            },
        )
        return False


def _client_supports_updating(client: object) -> bool:
    try:
        update_fn = getattr(client, "update_message", None)
        if not callable(update_fn):
            return False
        if (
            isinstance(client, SlackClient)
            and type(client).update_message is SlackClient.update_message
        ):
            return False
        return True
    except Exception:
        return False


def _update_message_accepts_blocks(client: object) -> bool:
    try:
        parameters = signature(client.update_message).parameters.values()
    except (TypeError, ValueError, AttributeError):
        return True
    return any(
        parameter.name == "blocks" or parameter.kind is Parameter.VAR_KEYWORD
        for parameter in parameters
    )


class _SlackProgressTracker:
    def __init__(self, client: SlackClient, channel_id: str) -> None:
        self._client = client
        self._channel_id = channel_id
        self._message_ts: str | None = None
        self._last_status: str | None = None
        self._failed: bool = False

    @property
    def message_ts(self) -> str | None:
        return self._message_ts

    async def update(self, status: str) -> None:
        if self._failed or status == self._last_status:
            return
        self._last_status = status

        if not _client_supports_updating(self._client):
            return

        try:
            progress_text = f":hourglass_flowing_sand: {status}..."
            has_blocks = _post_message_accepts_blocks(self._client)
            blocks = (
                [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":hourglass_flowing_sand: *{status}...*",
                        },
                    }
                ]
                if has_blocks
                else None
            )

            if self._message_ts is None:
                if has_blocks:
                    result = await self._client.post_message(
                        self._channel_id, progress_text, blocks=blocks
                    )
                else:
                    result = await self._client.post_message(
                        self._channel_id, progress_text
                    )
                if isinstance(result, str) and result:
                    self._message_ts = result
            else:
                if _update_message_accepts_blocks(self._client):
                    await self._client.update_message(
                        self._channel_id, self._message_ts, progress_text, blocks=blocks
                    )
                else:
                    await self._client.update_message(
                        self._channel_id, self._message_ts, progress_text
                    )
        except Exception:
            logger.warning("Slack progress indicator update failed", exc_info=True)
            self._failed = True


async def _respond(
    agent: AccountIntelligenceAgent,
    client: SlackClient,
    authorization: SlackAuthorizationService | object,
    message: object = None,
    tenant_id: str | None = None,
    request_id: str | None = None,
    conversation_history: SlackConversationService | None = None,
) -> None:
    if request_id is None and isinstance(tenant_id, str):
        request_id = tenant_id
        tenant_id = str(message)
        message = authorization
        authorization = None

    parsed = parse_message(message)
    if parsed is None:
        return
    if authorization is not None and not await _is_authorized_slack_user(
        authorization,
        tenant_id=tenant_id,
        team_id=parsed.team_id,
        user_id=parsed.user_id,
    ):
        return

    progress_tracker = _SlackProgressTracker(client, parsed.channel_id)
    conversation_context = None
    if conversation_history is not None:
        try:
            prior_turns = await conversation_history.latest(
                tenant_id=tenant_id,
                team_id=parsed.team_id,
                user_id=parsed.user_id,
                limit=3,
            )
            conversation_context = "\n".join(
                f"User: {turn.question}\nAssistant: {turn.response_text[:400]}"
                for turn in prior_turns
            )[-2000:] or None
        except Exception:
            logger.warning(
                "Slack conversation context lookup failed",
                extra={"tenant_id": tenant_id},
            )

    agent_request = AgentRequest(
        tenant_id=tenant_id,
        actor_id=parsed.user_id,
        message=parsed.text,
        request_id=request_id,
        channel_id=parsed.channel_id,
        message_ts=parsed.ts,
        event_id=parsed.event_id,
        conversation_context=conversation_context,
    )
    if "on_progress" in signature(agent.respond).parameters:
        response = await agent.respond(agent_request, on_progress=progress_tracker.update)
    else:
        response = await agent.respond(agent_request)

    if response.status == "duplicate_request":
        # Redelivery of a message that was already handled; the first delivery replied.
        logger.info(
            "Skipping reply for duplicate Slack delivery",
            extra={"tenant_id": tenant_id},
        )
        if progress_tracker.message_ts is not None and hasattr(client, "delete_message"):
            try:
                await client.delete_message(parsed.channel_id, progress_tracker.message_ts)
            except Exception:
                logger.warning(
                    "Failed to delete progress message for duplicate request",
                    exc_info=True,
                )
        return

    try:
        formatted = format_slack_response(response, surface="channel")
        for index, blocks in enumerate(formatted.messages):
            fallback_text = (
                formatted.fallback_text
                if index == 0
                else "Continued response details."
            )
            if (
                index == 0
                and progress_tracker.message_ts is not None
                and _client_supports_updating(client)
            ):
                try:
                    if _update_message_accepts_blocks(client):
                        await client.update_message(
                            parsed.channel_id,
                            progress_tracker.message_ts,
                            fallback_text,
                            blocks=blocks,
                        )
                    else:
                        await client.update_message(
                            parsed.channel_id,
                            progress_tracker.message_ts,
                            fallback_text,
                        )
                    continue
                except Exception:
                    logger.warning(
                        "Slack progress message update with final response failed; "
                        "falling back to post_message",
                        exc_info=True,
                    )

            if _post_message_accepts_blocks(client):
                await client.post_message(parsed.channel_id, fallback_text, blocks=blocks)
            else:
                # Keep compatibility with existing injected text-only clients.
                # The production SlackClient accepts Block Kit payloads.
                await client.post_message(parsed.channel_id, fallback_text)

        if conversation_history is not None:
            try:
                await conversation_history.save_completed(
                    tenant_id=tenant_id,
                    team_id=parsed.team_id,
                    user_id=parsed.user_id,
                    delivery_id=parsed.ts,
                    question=parsed.text,
                    response=response,
                )
            except Exception:
                logger.exception(
                    "Slack channel conversation history save failed", extra={"tenant_id": tenant_id}
                )
    except IntegrationError:
        logger.error(
            "Slack response delivery failed",
            extra={"tenant_id": tenant_id},
        )


def _post_message_accepts_blocks(client: SlackClient) -> bool:
    try:
        parameters = signature(client.post_message).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == "blocks" or parameter.kind is Parameter.VAR_KEYWORD
        for parameter in parameters
    )


async def _publish_home(
    client: SlackClient,
    action_safety: ActionSafetyService,
    conversation_history: SlackConversationService,
    *,
    tenant_id: str,
    team_id: str,
    user_id: str,
    request_text: str | None = None,
    response: AgentResponse | None = None,
    history_detail: object | None = None,
    history_page: int = 0,
    working: bool = False,
    progress_status: str | None = None,
    draft: str | None = None,
    timing_label: str | None = None,
    banner_image_url: str | None = None,
) -> bool:
    started = perf_counter()
    activity_started = perf_counter()
    try:
        recent = await action_safety.recent_actions(tenant_id=tenant_id, actor_id=user_id)
        recent_unavailable = False
    except Exception:
        logger.exception("Slack App Home activity lookup failed", extra={"tenant_id": tenant_id})
        recent = []
        recent_unavailable = True
    finally:
        _log_timing("slack_home.recent_activity", activity_started, tenant_id)

    history_started = perf_counter()
    try:
        history = await conversation_history.latest(
            tenant_id=tenant_id, team_id=team_id, user_id=user_id, limit=5
        )
    except Exception:
        logger.exception(
            "Slack Home conversation history lookup failed",
            extra={"tenant_id": tenant_id},
        )
        history = []
    finally:
        _log_timing("slack_home.conversation_history", history_started, tenant_id)

    build_started = perf_counter()
    view = build_home_view(
        recent=recent,
        request_text=request_text,
        response=response,
        working=working,
        progress_status=progress_status,
        draft=draft,
        recent_unavailable=recent_unavailable,
        history=history,
        history_detail=history_detail,
        history_page=history_page,
        active_delivery_id=(
            history[-1].delivery_id
            if history and history[-1].question == request_text and response is not None
            else None
        ),
        banner_image_url=banner_image_url,
    )
    _log_timing("slack_home.view_build", build_started, tenant_id)

    publish_started = perf_counter()
    try:
        await client.publish_home_view(user_id, view)
    except IntegrationError as exc:
        logger.error(
            "Slack App Home publish failed",
            extra={"tenant_id": tenant_id, "error": str(exc)},
        )
        return False
    finally:
        _log_timing("slack_home.views_publish", publish_started, tenant_id)
        if timing_label is not None:
            _log_timing(f"slack_home.{timing_label}_publish_total", started, tenant_id)
    return True


class _SlackHomeProgressTracker:
    def __init__(
        self,
        client: SlackClient,
        action_safety: ActionSafetyService,
        conversation_history: SlackConversationService,
        *,
        tenant_id: str,
        team_id: str,
        user_id: str,
        request_text: str,
        banner_image_url: str | None,
    ) -> None:
        self._client = client
        self._action_safety = action_safety
        self._conversation_history = conversation_history
        self._tenant_id = tenant_id
        self._team_id = team_id
        self._user_id = user_id
        self._request_text = request_text
        self._banner_image_url = banner_image_url
        self._last_status: str | None = None
        self._failed = False
        self._lock = Lock()

    async def update(self, status: str) -> None:
        async with self._lock:
            if self._failed or not status or status == self._last_status:
                return
            self._last_status = status
            try:
                published = await _publish_home(
                    self._client,
                    self._action_safety,
                    self._conversation_history,
                    tenant_id=self._tenant_id,
                    team_id=self._team_id,
                    user_id=self._user_id,
                    request_text=self._request_text,
                    working=True,
                    progress_status=status,
                    banner_image_url=self._banner_image_url,
                )
                if not published:
                    self._failed = True
            except Exception:
                self._failed = True
                logger.warning(
                    "Slack App Home progress update failed",
                    extra={"tenant_id": self._tenant_id},
                    exc_info=True,
                )


async def _publish_authorized_home(
    client: SlackClient,
    action_safety: ActionSafetyService,
    conversation_history: SlackConversationService,
    authorization: SlackAuthorizationService,
    *,
    tenant_id: str,
    team_id: str,
    user_id: str,
    banner_image_url: str | None = None,
) -> None:
    if not await _is_authorized_slack_user(
        authorization,
        tenant_id=tenant_id,
        team_id=team_id,
        user_id=user_id,
    ):
        return
    await _publish_home(
        client,
        action_safety,
        conversation_history,
        tenant_id=tenant_id,
        team_id=team_id,
        user_id=user_id,
        banner_image_url=banner_image_url,
    )


async def _handle_home_interaction(
    agent: AccountIntelligenceAgent,
    client: SlackClient,
    action_safety: ActionSafetyService,
    conversation_history: SlackConversationService,
    authorization: SlackAuthorizationService,
    interaction: HomeInteraction,
    tenant_id: str,
    request_id: str,
    banner_image_url: str | None = None,
) -> None:
    if not await _is_authorized_slack_user(
        authorization,
        tenant_id=tenant_id,
        team_id=interaction.team_id,
        user_id=interaction.user_id,
    ):
        return
    if interaction.kind == "view_full_response":
        try:
            exchange = await conversation_history.get_owned(
                tenant_id=tenant_id,
                team_id=interaction.team_id,
                user_id=interaction.user_id,
                delivery_id=interaction.history_delivery_id,
            )
        except Exception:
            logger.warning(
                "Slack Home full response lookup failed",
                extra={"tenant_id": tenant_id},
            )
            return
        if exchange is None:
            # Do not distinguish missing records from records owned by another user.
            return
        await _publish_home(
            client,
            action_safety,
            conversation_history,
            tenant_id=tenant_id,
            team_id=interaction.team_id,
            user_id=interaction.user_id,
            history_detail=exchange,
            history_page=interaction.history_page,
            banner_image_url=banner_image_url,
        )
        return

    if interaction.kind == "quick_action":
        await _publish_home(
            client,
            action_safety,
            conversation_history,
            tenant_id=tenant_id,
            team_id=interaction.team_id,
            user_id=interaction.user_id,
            draft=interaction.template,
            banner_image_url=banner_image_url,
            )
        return

    if interaction.kind == "input":
        await _publish_home(
            client,
            action_safety,
            conversation_history,
            tenant_id=tenant_id,
            team_id=interaction.team_id,
            user_id=interaction.user_id,
            draft=interaction.text,
            banner_image_url=banner_image_url,
            )
        return

    if interaction.kind == "select_contact":
        message = f"Update contact {interaction.action_id}"
    elif interaction.kind == "select_field":
        contact_id, _, field = interaction.text.partition(":")
        if field == "lifecyclestage":
            message = f"Update contact {contact_id}'s lifecycle stage"
        elif field in {"hs_lead_status", "lead_status"}:
            message = f"Update contact {contact_id}'s lead status"
        else:
            message = f"Update contact {contact_id}'s {field}"
    elif interaction.kind == "select_value":
        parts = interaction.text.split(":")
        if len(parts) >= 3:
            contact_id, field, value = parts[0], parts[1], parts[2]
            message = f"Update contact {contact_id} with {field} {value}"
        else:
            message = interaction.text
    else:
        message = (
            f"confirm {interaction.action_id}"
            if interaction.kind == "confirm"
            else interaction.text
        )
    if not message.strip():
        response = AgentResponse(
            status="missing_fields",
            text="Enter a request before sending it.",
            request_id=request_id,
        )
        await _publish_home(
            client,
            action_safety,
            conversation_history,
            tenant_id=tenant_id,
            team_id=interaction.team_id,
            user_id=interaction.user_id,
            response=response,
            banner_image_url=banner_image_url,
            )
        return

    await _publish_home(
        client,
        action_safety,
        conversation_history,
        tenant_id=tenant_id,
        team_id=interaction.team_id,
        user_id=interaction.user_id,
        request_text=message,
        working=True,
        timing_label="loading",
        banner_image_url=banner_image_url,
    )
    agent_started = perf_counter()
    conversation_context = None
    if interaction.kind in {"submit", "select_contact", "select_field", "select_value", "confirm"}:
        try:
            prior_turns = await conversation_history.latest(
                tenant_id=tenant_id,
                team_id=interaction.team_id,
                user_id=interaction.user_id,
                limit=3,
            )
            conversation_context = "\n".join(
                f"User: {turn.question}\nAssistant: {turn.response_text[:400]}"
                for turn in prior_turns
            )[-2000:] or None
        except Exception:
            logger.warning(
                "Slack Home conversation context lookup failed",
                extra={"tenant_id": tenant_id},
            )
    home_progress = _SlackHomeProgressTracker(
        client,
        action_safety,
        conversation_history,
        tenant_id=tenant_id,
        team_id=interaction.team_id,
        user_id=interaction.user_id,
        request_text=message,
        banner_image_url=banner_image_url,
    )
    agent_request = AgentRequest(
        tenant_id=tenant_id,
        actor_id=interaction.user_id,
        message=message,
        request_id=request_id or uuid4().hex,
        channel_id=f"apphome:{interaction.user_id}",
        message_ts=interaction.action_ts or None,
        event_id=interaction.action_ts or None,
        conversation_context=conversation_context,
    )
    try:
        if "on_progress" in signature(agent.respond).parameters:
            response = await agent.respond(agent_request, on_progress=home_progress.update)
        else:
            response = await agent.respond(agent_request)
    except Exception:
        logger.exception(
            "Slack App Home agent request failed",
            extra={"tenant_id": tenant_id},
        )
        response = AgentResponse(
            status="unavailable",
            text="Something went wrong while processing your request. Please try again.",
            request_id=request_id,
        )
    finally:
        _log_timing("agent.respond", agent_started, tenant_id)
    if interaction.kind in {"submit", "select_contact", "select_field", "select_value", "confirm"}:
        try:
            await conversation_history.save_completed(
                tenant_id=tenant_id,
                team_id=interaction.team_id,
                user_id=interaction.user_id,
                delivery_id=interaction.action_ts,
                question=message,
                response=response,
            )
        except Exception:
            logger.exception(
                "Slack Home conversation history save failed", extra={"tenant_id": tenant_id}
            )
    await _publish_home(
        client,
        action_safety,
        conversation_history,
        tenant_id=tenant_id,
        team_id=interaction.team_id,
        user_id=interaction.user_id,
        request_text=message,
        response=response,
        timing_label="final",
        banner_image_url=banner_image_url,
    )


@router.post("/interactions")
async def interactions(
    request: Request,
    background_tasks: BackgroundTasks,
    client: Annotated[SlackClient, Depends(get_slack_client)],
    agent: Annotated[AccountIntelligenceAgent, Depends(get_agent)],
    action_safety: Annotated[ActionSafetyService, Depends(get_action_safety)],
    conversation_history: Annotated[
        SlackConversationService, Depends(get_conversation_history)
    ],
    authorization: Annotated[
        SlackAuthorizationService, Depends(get_slack_authorization)
    ],
) -> Response:
    """Handle input and actions from the in-Slack App Home conversation."""
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

    if not await _is_authorized_slack_user(
        authorization,
        tenant_id=tenant_id,
        team_id=interaction.team_id,
        user_id=interaction.user_id,
    ):
        return Response(status_code=200)

    background_tasks.add_task(
        _handle_home_interaction,
        agent,
        client,
        action_safety,
        conversation_history,
        authorization,
        interaction,
        tenant_id=tenant_id,
        request_id=request.state.request_id,
        banner_image_url=build_welcome_banner_url(settings.web_app_base_url),
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
    action_safety: Annotated[ActionSafetyService, Depends(get_action_safety)],
    conversation_history: Annotated[
        SlackConversationService, Depends(get_conversation_history)
    ],
    authorization: Annotated[
        SlackAuthorizationService, Depends(get_slack_authorization)
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

        if not await _is_authorized_slack_user(
            authorization,
            tenant_id=home_tenant_id,
            team_id=home_opened.team_id,
            user_id=home_opened.user_id,
        ):
            return Response(status_code=200)

        background_tasks.add_task(
            _publish_authorized_home,
            client,
            action_safety,
            conversation_history,
            authorization,
            tenant_id=home_tenant_id,
            team_id=home_opened.team_id,
            user_id=home_opened.user_id,
            banner_image_url=build_welcome_banner_url(settings.web_app_base_url),
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

    if not await _is_authorized_slack_user(
        authorization,
        tenant_id=tenant_id,
        team_id=parsed.team_id,
        user_id=parsed.user_id,
    ):
        return Response(status_code=200)

    background_tasks.add_task(
        _respond,
        agent,
        client,
        authorization,
        payload,
        tenant_id,
        request.state.request_id,
        conversation_history,
    )

    return Response(status_code=200)
