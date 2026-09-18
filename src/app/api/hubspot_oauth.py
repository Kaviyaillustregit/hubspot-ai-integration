from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api.errors import AppError
from app.core.secrets import SecretCipher
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.oauth import (
    HubSpotOAuthTokenClient,
    OAuthCallbackRequest,
    OAuthCallbackResponse,
    OAuthStartResponse,
)
from app.services.hubspot_oauth import HubSpotOAuthService

router = APIRouter(prefix="/auth/hubspot", tags=["hubspot-oauth"])


def get_oauth_service(request: Request) -> HubSpotOAuthService:
    settings = request.app.state.settings
    if not settings.hubspot_token_encryption_key:
        raise AppError("oauth_not_configured", "HubSpot OAuth is not configured", 503)
    return HubSpotOAuthService(
        settings,
        request.app.state.session_factory,
        HubSpotOAuthTokenClient(settings),
        SecretCipher(settings.hubspot_token_encryption_key),
    )


@router.get("/start", response_model=OAuthStartResponse)
async def start_oauth(
    oauth: Annotated[HubSpotOAuthService, Depends(get_oauth_service)],
    tenant_id: str = Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> OAuthStartResponse:
    try:
        authorization_url, state = await oauth.start(tenant_id)
    except ValueError as exc:
        raise AppError("oauth_not_configured", "HubSpot OAuth is not configured", 503) from exc
    return OAuthStartResponse(authorization_url=authorization_url, state=state)


@router.get("/callback", response_model=OAuthCallbackResponse)
async def oauth_callback(
    oauth: Annotated[HubSpotOAuthService, Depends(get_oauth_service)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> JSONResponse:
    callback = OAuthCallbackRequest(
        code=code,
        state=state,
        error=error,
        error_description=error_description,
    )
    if callback.error:
        return JSONResponse(
            status_code=400,
            content=OAuthCallbackResponse(
                status="denied", message="HubSpot authorization was denied"
            ).model_dump(),
        )
    if not callback.code or not callback.state:
        raise AppError("invalid_oauth_callback", "OAuth code and state are required", 400)
    try:
        await oauth.exchange_code(callback.code, callback.state)
    except ValueError as exc:
        raise AppError("invalid_oauth_state", "OAuth state is invalid or expired", 400) from exc
    except IntegrationError as exc:
        raise AppError("oauth_exchange_failed", "HubSpot OAuth exchange failed", 502) from exc
    return JSONResponse(
        status_code=200,
        content=OAuthCallbackResponse(
            status="connected", message="HubSpot authorization completed"
        ).model_dump(),
    )