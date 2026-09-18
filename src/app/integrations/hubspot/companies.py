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
    HubSpotAssociationType,
    HubSpotCompaniesPage,
    HubSpotCompany,
    HubSpotContactCompanyAssociation,
    HubSpotContactCompanyAssociations,
)


class HubSpotCompaniesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[HubSpotCompany] = Field(default_factory=list)
    paging: dict[str, dict[str, str]] | None = None


class HubSpotAssociationsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    results: list[HubSpotContactCompanyAssociation] = Field(default_factory=list)


class HubSpotCompaniesClient:
    """Read-only HubSpot Companies and Contact-Company Associations adapter."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def list_companies(
        self,
        context: TenantContext,
        access_token: str,
        *,
        limit: int = 100,
        after: str | None = None,
        properties: Sequence[str] = (),
    ) -> HubSpotCompaniesPage:
        params: dict[str, str | int] = {"limit": limit}
        if after:
            params["after"] = after
        if properties:
            params["properties"] = ",".join(properties)
        payload = await self._request(
            "https://api.hubapi.com/crm/v3/objects/companies",
            access_token,
            params,
        )
        response = self._validate_companies(payload)
        next_after = response.paging.get("next", {}).get("after") if response.paging else None
        return HubSpotCompaniesPage(results=response.results, next_after=next_after)

    async def get_contact_company_associations(
        self,
        context: TenantContext,
        access_token: str,
        contact_id: str,
    ) -> HubSpotContactCompanyAssociations:
        payload = await self._request(
            f"https://api.hubapi.com/crm/objects/2026-09/contacts/{contact_id}/associations/companies",
            access_token,
            {},
        )
        try:
            if isinstance(payload, list):
                payload = {"results": payload}
            response = HubSpotAssociationsResponse.model_validate(payload)
            return HubSpotContactCompanyAssociations(
                results=[
                    HubSpotContactCompanyAssociation(
                        company_id=item.company_id,
                        association_types=[
                            HubSpotAssociationType.model_validate(association.model_dump())
                            for association in item.association_types
                        ],
                    )
                    for item in response.results
                ]
            )
        except (ValueError, TypeError) as exc:
            raise IntegrationError("HubSpot Associations response was invalid") from exc

    async def _request(
        self,
        url: str,
        access_token: str,
        params: dict[str, str | int],
    ) -> object:
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            if self._client is not None:
                response = await self._client.get(url, params=params, headers=headers)
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise IntegrationTimeoutError("HubSpot CRM request timed out") from exc
        except httpx.HTTPError as exc:
            raise IntegrationError("HubSpot CRM request failed") from exc
        if response.status_code == 401:
            raise IntegrationAuthenticationError("HubSpot CRM request was rejected")
        if response.status_code == 429:
            raise IntegrationRateLimitError("HubSpot CRM rate limit reached")
        if response.is_error:
            raise IntegrationError("HubSpot CRM request failed")
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            raise IntegrationError("HubSpot CRM response was invalid") from exc

    @staticmethod
    def _validate_companies(payload: object) -> HubSpotCompaniesResponse:
        try:
            return HubSpotCompaniesResponse.model_validate(payload)
        except (ValueError, TypeError) as exc:
            raise IntegrationError("HubSpot Companies response was invalid") from exc