import json
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.agent.schemas import AgentRequest
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.anthropic import AnthropicProvider
from app.ai.service import AIService
from app.api.hubspot_companies import get_companies_service
from app.api.hubspot_contacts import get_contacts_service
from app.integrations.errors import IntegrationError
from app.integrations.slack.client import SlackClient
from app.integrations.slack.events import (
    SlackRequestVerifier,
    SlackSignatureError,
    SlackTenantResolver,
    parse_message,
)
from app.integrations.slack.http_client import SlackWebApiClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/slack", tags=["slack"])


def get_agent(request: Request) -> AccountIntelligenceAgent:
    tools = HubSpotToolRegistry(
        get_companies_service(request), get_contacts_service(request)
    )
    return AccountIntelligenceAgent(
        tools, AIService(AnthropicProvider(request.app.state.settings))
    )


def get_slack_client(request: Request) -> SlackClient:
    return SlackWebApiClient(request.app.state.settings)


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
        )
    )
    try:
        await client.post_message(parsed.channel_id, response.text)
    except IntegrationError:
        logger.error("Slack response delivery failed", extra={"tenant_id": tenant_id})


@router.post("/events")
async def events(
    request: Request,
    background_tasks: BackgroundTasks,
    agent: Annotated[AccountIntelligenceAgent, Depends(get_agent)],
    client: Annotated[SlackClient, Depends(get_slack_client)],
) -> Response:
    settings = request.app.state.settings
    if not settings.slack_signing_secret:
        return JSONResponse(
            {"code": "slack_not_configured", "message": "Slack is not configured"},
            status_code=503,
        )
    raw_body = await request.body()
    try:
        SlackRequestVerifier(settings.slack_signing_secret).verify(
            request.headers, raw_body
        )
        payload = json.loads(raw_body)
    except (SlackSignatureError, json.JSONDecodeError):
        return JSONResponse(
            {"code": "invalid_slack_request", "message": "Invalid Slack request"},
            status_code=401,
        )
    if payload.get("type") == "url_verification" and isinstance(payload.get("challenge"), str):
        return JSONResponse({"challenge": payload["challenge"]})
    parsed = parse_message(payload)
    if parsed is None:
        return Response(status_code=200)
    tenant_id = SlackTenantResolver(settings.slack_team_tenant_map).resolve(parsed.team_id)
    if tenant_id is None:
        logger.warning("Slack workspace is not mapped to a tenant")
        return Response(status_code=200)
    background_tasks.add_task(_respond, agent, client, payload, tenant_id, request.state.request_id)
    return Response(status_code=200)
