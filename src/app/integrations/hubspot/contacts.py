from collections.abc import Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.integrations.errors import (
    IntegrationAuthenticationError,
    IntegrationError,
    IntegrationRateLimitError,
    IntegrationTimeoutError,
)
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import HubSpotContact, HubSpotContactsPage


class HubSpotContactsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[HubSpotContact] = Field(default_factory=list)
    paging: dict[str, dict[str, str]] | None = None


class HubSpotContactsClient:
    """Read-only HubSpot Contacts API adapter."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def list_contacts(
        self,
        context: TenantContext,
        access_token: str,
        *,
        limit: int = 100,
        after: str | None = None,
        properties: Sequence[str] = (),
    ) -> HubSpotContactsPage:
        params: dict[str, str | int] = {"limit": limit}
        if after:
            params["after"] = after
        if properties:
            params["properties"] = ",".join(properties)
        headers = {"Authorization": f"Bearer {access_token}"}
        url = "https://api.hubapi.com/crm/v3/objects/contacts"
        try:
            if self._client is not None:
                response = await self._client.get(url, params=params, headers=headers)
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise IntegrationTimeoutError("HubSpot Contacts request timed out") from exc
        except httpx.HTTPError as exc:
            raise IntegrationError("HubSpot Contacts request failed") from exc
        if response.status_code == 401:
            raise IntegrationAuthenticationError("HubSpot Contacts request was rejected")
        if response.status_code == 429:
            raise IntegrationRateLimitError("HubSpot Contacts rate limit reached")
        if response.is_error:
            raise IntegrationError("HubSpot Contacts request failed")
        try:
            payload = HubSpotContactsResponse.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise IntegrationError("HubSpot Contacts response was invalid") from exc
        next_after = None
        if payload.paging and payload.paging.get("next"):
            next_after = payload.paging["next"].get("after")
        return HubSpotContactsPage(results=payload.results, next_after=next_after)