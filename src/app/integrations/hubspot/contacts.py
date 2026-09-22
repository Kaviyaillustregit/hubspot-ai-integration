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
from app.integrations.hubspot.models import (
    HubSpotContact,
    HubSpotContactCreate,
    HubSpotContactsPage,
    HubSpotContactUpdate,
)


class HubSpotContactsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[HubSpotContact] = Field(default_factory=list)
    paging: dict[str, dict[str, str]] | None = None


class HubSpotContactsClient:
    """HubSpot Contacts API adapter."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
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
                response = await self._client.get(
                    url,
                    params=params,
                    headers=headers,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.get(
                        url,
                        params=params,
                        headers=headers,
                    )
        except httpx.TimeoutException as exc:
            raise IntegrationTimeoutError(
                "HubSpot Contacts request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise IntegrationError(
                "HubSpot Contacts request failed"
            ) from exc

        if response.status_code == 401:
            raise IntegrationAuthenticationError(
                "HubSpot Contacts request was rejected"
            )

        if response.status_code == 429:
            raise IntegrationRateLimitError(
                "HubSpot Contacts rate limit reached"
            )

        if response.is_error:
            raise IntegrationError("HubSpot Contacts request failed")

        try:
            payload = HubSpotContactsResponse.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise IntegrationError(
                "HubSpot Contacts response was invalid"
            ) from exc

        next_after = None

        if payload.paging and payload.paging.get("next"):
            next_after = payload.paging["next"].get("after")

        return HubSpotContactsPage(
            results=payload.results,
            next_after=next_after,
        )

    async def find_contact_by_email(
        self,
        context: TenantContext,
        access_token: str,
        *,
        email: str,
    ) -> HubSpotContact | None:
        url = "https://api.hubapi.com/crm/v3/objects/contacts/search"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email,
                        }
                    ]
                }
            ],
            "properties": [
                "email",
                "firstname",
                "lastname",
            ],
            "limit": 1,
        }

        try:
            if self._client is not None:
                response = await self._client.post(
                    url,
                    headers=headers,
                    json=payload,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.post(
                        url,
                        headers=headers,
                        json=payload,
                    )
        except httpx.TimeoutException as exc:
            raise IntegrationTimeoutError(
                "HubSpot contact search timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise IntegrationError(
                "HubSpot contact search request failed"
            ) from exc

        if response.status_code == 401:
            raise IntegrationAuthenticationError(
                "HubSpot contact search was rejected"
            )

        if response.status_code == 429:
            raise IntegrationRateLimitError(
                "HubSpot contact search rate limit reached"
            )

        if response.is_error:
            raise IntegrationError(
                "HubSpot contact search request failed"
            )

        try:
            payload_response = HubSpotContactsResponse.model_validate(
                response.json()
            )
        except (ValueError, TypeError) as exc:
            raise IntegrationError(
                "HubSpot contact search response was invalid"
            ) from exc

        if not payload_response.results:
            return None

        return payload_response.results[0]

    async def create_contact(
        self,
        context: TenantContext,
        access_token: str,
        *,
        properties: dict[str, str | None],
    ) -> HubSpotContact:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        url = "https://api.hubapi.com/crm/v3/objects/contacts"

        payload = HubSpotContactCreate(
            properties=properties
        ).model_dump()

        try:
            if self._client is not None:
                response = await self._client.post(
                    url,
                    headers=headers,
                    json=payload,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.post(
                        url,
                        headers=headers,
                        json=payload,
                    )
        except httpx.TimeoutException as exc:
            raise IntegrationTimeoutError(
                "HubSpot Contacts request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise IntegrationError(
                "HubSpot Contacts request failed"
            ) from exc

        if response.status_code == 401:
            raise IntegrationAuthenticationError(
                "HubSpot Contacts request was rejected"
            )

        if response.status_code == 429:
            raise IntegrationRateLimitError(
                "HubSpot Contacts rate limit reached"
            )

        if response.is_error:
            raise IntegrationError(
                "HubSpot Contacts create request failed"
            )

        try:
            return HubSpotContact.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise IntegrationError(
                "HubSpot Contacts create response was invalid"
            ) from exc

    async def update_contact(
        self,
        context: TenantContext,
        access_token: str,
        *,
        contact_id: str,
        properties: dict[str, str | None],
    ) -> HubSpotContact:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        url = (
            "https://api.hubapi.com/crm/v3/objects/contacts/"
            f"{contact_id}"
        )

        payload = HubSpotContactUpdate(
            properties=properties
        ).model_dump()

        try:
            if self._client is not None:
                response = await self._client.patch(
                    url,
                    headers=headers,
                    json=payload,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.patch(
                        url,
                        headers=headers,
                        json=payload,
                    )
        except httpx.TimeoutException as exc:
            raise IntegrationTimeoutError(
                "HubSpot Contacts update request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise IntegrationError(
                "HubSpot Contacts update request failed"
            ) from exc

        if response.status_code == 401:
            raise IntegrationAuthenticationError(
                "HubSpot Contacts update was rejected"
            )

        if response.status_code == 429:
            raise IntegrationRateLimitError(
                "HubSpot Contacts update rate limit reached"
            )

        if response.is_error:
            raise IntegrationError(
                "HubSpot Contacts update request failed"
            )

        try:
            return HubSpotContact.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise IntegrationError(
                "HubSpot Contacts update response was invalid"
            ) from exc