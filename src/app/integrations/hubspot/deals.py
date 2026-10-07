from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.errors import IntegrationError
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.http import HubSpotApiClient
from app.integrations.hubspot.models import HubSpotDeal, HubSpotDealsPage, HubSpotPipeline


class _DealsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[HubSpotDeal] = Field(default_factory=list)


class _PipelinesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[HubSpotPipeline] = Field(default_factory=list)


class HubSpotDealsClient(HubSpotApiClient):
    """HubSpot Deals and deal Pipelines API adapter."""

    async def search_deals(
        self,
        context: TenantContext,
        access_token: str,
        *,
        query: str,
        limit: int = 100,
        properties: Sequence[str] = (),
    ) -> HubSpotDealsPage:
        body: dict[str, object] = {"query": query, "limit": limit}
        if properties:
            body["properties"] = list(properties)
        payload = await self._call(
            "POST",
            "/crm/v3/objects/deals/search",
            access_token,
            label="HubSpot deal search",
            json_body=body,
        )
        try:
            return HubSpotDealsPage(results=_DealsResponse.model_validate(payload).results)
        except (ValueError, TypeError) as exc:
            raise IntegrationError("HubSpot deal search response was invalid") from exc

    async def get_deal(
        self,
        context: TenantContext,
        access_token: str,
        *,
        deal_id: str,
        properties: Sequence[str] = (),
    ) -> HubSpotDeal:
        params: dict[str, str | int] = {"properties": ",".join(properties)} if properties else {}
        payload = await self._call(
            "GET",
            f"/crm/v3/objects/deals/{deal_id}",
            access_token,
            label="HubSpot Deals",
            params=params,
        )
        return self._validate_deal(payload)

    async def create_deal(
        self,
        context: TenantContext,
        access_token: str,
        *,
        properties: dict[str, str],
    ) -> HubSpotDeal:
        payload = await self._call(
            "POST",
            "/crm/v3/objects/deals",
            access_token,
            label="HubSpot Deals create",
            json_body={"properties": properties},
        )
        return self._validate_deal(payload)

    async def update_deal(
        self,
        context: TenantContext,
        access_token: str,
        *,
        deal_id: str,
        properties: dict[str, str],
    ) -> HubSpotDeal:
        payload = await self._call(
            "PATCH",
            f"/crm/v3/objects/deals/{deal_id}",
            access_token,
            label="HubSpot Deals update",
            json_body={"properties": properties},
        )
        return self._validate_deal(payload)

    async def list_pipelines(
        self,
        context: TenantContext,
        access_token: str,
    ) -> list[HubSpotPipeline]:
        payload = await self._call(
            "GET",
            "/crm/v3/pipelines/deals",
            access_token,
            label="HubSpot deal pipelines",
        )
        try:
            return _PipelinesResponse.model_validate(payload).results
        except (ValueError, TypeError) as exc:
            raise IntegrationError("HubSpot deal pipelines response was invalid") from exc

    @staticmethod
    def _validate_deal(payload: object) -> HubSpotDeal:
        try:
            return HubSpotDeal.model_validate(payload)
        except (ValueError, TypeError) as exc:
            raise IntegrationError("HubSpot Deals response was invalid") from exc
