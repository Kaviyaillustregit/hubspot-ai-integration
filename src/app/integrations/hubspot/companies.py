from collections.abc import Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.integrations.errors import (
    IntegrationAuthenticationError,
    IntegrationError,
    IntegrationRateLimitError,
    IntegrationTimeoutError,
)
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.http import HubSpotApiClient
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


class HubSpotCompaniesClient(HubSpotApiClient):
    """HubSpot Companies and Contact-Company Associations adapter."""

    async def get_company(
        self,
        context: TenantContext,
        access_token: str,
        *,
        company_id: str,
        properties: Sequence[str] = (),
    ) -> HubSpotCompany:
        params: dict[str, str | int] = {"properties": ",".join(properties)} if properties else {}
        payload = await self._call(
            "GET",
            f"/crm/v3/objects/companies/{company_id}",
            access_token,
            label="HubSpot Companies",
            params=params,
        )
        return self._validate_company(payload)

    async def create_company(
        self,
        context: TenantContext,
        access_token: str,
        *,
        properties: dict[str, str],
    ) -> HubSpotCompany:
        payload = await self._call(
            "POST",
            "/crm/v3/objects/companies",
            access_token,
            label="HubSpot Companies create",
            json_body={"properties": properties},
        )
        return self._validate_company(payload)

    async def update_company(
        self,
        context: TenantContext,
        access_token: str,
        *,
        company_id: str,
        properties: dict[str, str],
    ) -> HubSpotCompany:
        payload = await self._call(
            "PATCH",
            f"/crm/v3/objects/companies/{company_id}",
            access_token,
            label="HubSpot Companies update",
            json_body={"properties": properties},
        )
        return self._validate_company(payload)

    @staticmethod
    def _validate_company(payload: object) -> HubSpotCompany:
        try:
            return HubSpotCompany.model_validate(payload)
        except (ValueError, TypeError) as exc:
            raise IntegrationError("HubSpot Companies response was invalid") from exc

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

    async def search_companies(
        self,
        context: TenantContext,
        access_token: str,
        *,
        query: str,
        limit: int = 100,
        properties: Sequence[str] = (),
    ) -> HubSpotCompaniesPage:
        body: dict[str, object] = {"query": query, "limit": limit}
        if properties:
            body["properties"] = list(properties)
        payload = await self._request(
            "https://api.hubapi.com/crm/v3/objects/companies/search",
            access_token,
            {},
            json_body=body,
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
        *,
        json_body: dict[str, object] | None = None,
    ) -> object:
        # A JSON body means a CRM search request (POST); everything else is a read (GET).
        method = "POST" if json_body is not None else "GET"
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            if self._client is not None:
                response = await self._client.request(
                    method, url, params=params, headers=headers, json=json_body
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.request(
                        method, url, params=params, headers=headers, json=json_body
                    )
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