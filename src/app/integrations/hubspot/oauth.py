from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.integrations.errors import (
    IntegrationAuthenticationError,
    IntegrationError,
    IntegrationTimeoutError,
)


class OAuthStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class OAuthStartResponse(BaseModel):
    authorization_url: str
    state: str


class OAuthCallbackRequest(BaseModel):
    code: str | None = Field(default=None, min_length=1)
    state: str | None = Field(default=None, min_length=1)
    error: str | None = None
    error_description: str | None = None


class OAuthCallbackResponse(BaseModel):
    status: str
    message: str


class HubSpotTokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    expires_in: int = Field(gt=0)
    hub_id: int
    scopes: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class OAuthState:
    tenant_id: str
    nonce: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class StoredOAuthToken:
    tenant_id: str
    hubspot_account_id: str
    encrypted_access_token: str
    encrypted_refresh_token: str
    expires_at: datetime
    scopes: list[str]
    created_at: datetime
    updated_at: datetime


class OAuthStateStore(ABC):
    """Persistence boundary for single-use, tenant-bound OAuth state."""

    @abstractmethod
    async def save(self, state: OAuthState) -> None: ...

    @abstractmethod
    async def consume(self, nonce: str) -> OAuthState | None: ...


class OAuthTokenStore(ABC):
    @abstractmethod
    async def save_token(self, token: StoredOAuthToken) -> None: ...

    @abstractmethod
    async def get_token(self, tenant_id: str) -> StoredOAuthToken | None: ...


class OAuthTokenClient(ABC):
    @abstractmethod
    async def exchange_code(self, code: str) -> HubSpotTokenResponse: ...

    @abstractmethod
    async def refresh_token(self, refresh_token: str) -> HubSpotTokenResponse: ...


class HubSpotOAuthTokenClient(OAuthTokenClient):
    """HubSpot v3 OAuth transport; token values never enter logs or responses."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def exchange_code(self, code: str) -> HubSpotTokenResponse:
        return await self._post(
            {
                "grant_type": "authorization_code",
                "code": code,
            }
        )

    async def refresh_token(self, refresh_token: str) -> HubSpotTokenResponse:
        return await self._post(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    async def _post(self, values: dict[str, str]) -> HubSpotTokenResponse:
        if not self._settings.hubspot_client_id or not self._settings.hubspot_client_secret:
            raise IntegrationAuthenticationError("HubSpot OAuth client is not configured")
        form = {
            **values,
            "client_id": self._settings.hubspot_client_id,
            "client_secret": self._settings.hubspot_client_secret,
            "redirect_uri": self._settings.hubspot_redirect_uri or "",
        }
        try:
            if self._client is not None:
                response = await self._client.post(
                    self._settings.hubspot_oauth_token_url, data=form
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.post(self._settings.hubspot_oauth_token_url, data=form)
        except httpx.TimeoutException as exc:
            raise IntegrationTimeoutError("HubSpot OAuth request timed out") from exc
        except httpx.HTTPError as exc:
            raise IntegrationError("HubSpot OAuth request failed") from exc
        if response.is_error:
            raise IntegrationAuthenticationError("HubSpot OAuth token request was rejected")
        try:
            return HubSpotTokenResponse.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise IntegrationError("HubSpot OAuth response was invalid") from exc


class HubSpotOAuthContract:
    """Builds OAuth protocol values; token exchange is intentionally not implemented."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create_state(self, tenant_id: str) -> OAuthState:
        return OAuthState(
            tenant_id=tenant_id,
            nonce=token_urlsafe(32),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )

    def authorization_url(self, state: OAuthState) -> str:
        if not self._settings.hubspot_client_id or not self._settings.hubspot_redirect_uri:
            raise ValueError("HubSpot OAuth client configuration is incomplete")
        query = urlencode(
            {
                "client_id": self._settings.hubspot_client_id,
                "redirect_uri": self._settings.hubspot_redirect_uri,
                "scope": self._settings.hubspot_oauth_scopes,
                "state": state.nonce,
            }
        )
        return f"{self._settings.hubspot_oauth_authorize_url}?{query}"