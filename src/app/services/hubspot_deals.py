from collections.abc import Sequence
from typing import Protocol

from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import HubSpotDeal, HubSpotDealsPage, HubSpotPipeline
from app.services.hubspot_contacts import AccessTokenProvider
from app.services.hubspot_scopes import require_scope


class DealsClient(Protocol):
    async def search_deals(
        self,
        context: TenantContext,
        access_token: str,
        *,
        query: str,
        limit: int = 100,
        properties: Sequence[str] = (),
    ) -> HubSpotDealsPage: ...

    async def get_deal(
        self,
        context: TenantContext,
        access_token: str,
        *,
        deal_id: str,
        properties: Sequence[str] = (),
    ) -> HubSpotDeal: ...

    async def create_deal(
        self, context: TenantContext, access_token: str, *, properties: dict[str, str]
    ) -> HubSpotDeal: ...

    async def update_deal(
        self,
        context: TenantContext,
        access_token: str,
        *,
        deal_id: str,
        properties: dict[str, str],
    ) -> HubSpotDeal: ...

    async def list_pipelines(
        self, context: TenantContext, access_token: str
    ) -> list[HubSpotPipeline]: ...


class HubSpotDealsService:
    def __init__(self, client: DealsClient, token_provider: AccessTokenProvider) -> None:
        self._client = client
        self._token_provider = token_provider

    async def search_deals(
        self,
        context: TenantContext,
        *,
        query: str,
        limit: int = 100,
        properties: Sequence[str] = (),
    ) -> HubSpotDealsPage:
        access_token, resolved = await self._authorized(context, "crm.objects.deals.read")
        return await self._client.search_deals(
            resolved, access_token, query=query, limit=limit, properties=properties
        )

    async def get_deal(
        self,
        context: TenantContext,
        *,
        deal_id: str,
        properties: Sequence[str] = (),
    ) -> HubSpotDeal:
        access_token, resolved = await self._authorized(context, "crm.objects.deals.read")
        return await self._client.get_deal(
            resolved, access_token, deal_id=deal_id, properties=properties
        )

    async def create_deal(
        self,
        context: TenantContext,
        *,
        properties: dict[str, str],
    ) -> HubSpotDeal:
        access_token, resolved = await self._authorized(context, "crm.objects.deals.write")
        return await self._client.create_deal(resolved, access_token, properties=properties)

    async def update_deal(
        self,
        context: TenantContext,
        *,
        deal_id: str,
        properties: dict[str, str],
    ) -> HubSpotDeal:
        access_token, resolved = await self._authorized(context, "crm.objects.deals.write")
        return await self._client.update_deal(
            resolved, access_token, deal_id=deal_id, properties=properties
        )

    async def list_pipelines(self, context: TenantContext) -> list[HubSpotPipeline]:
        access_token, resolved = await self._authorized(context, "crm.objects.deals.read")
        return await self._client.list_pipelines(resolved, access_token)

    async def _authorized(
        self, context: TenantContext, scope: str
    ) -> tuple[str, TenantContext]:
        access_token, token = await self._token_provider.get_access_token(context.tenant_id)
        require_scope(token, scope)
        return access_token, TenantContext(
            tenant_id=context.tenant_id,
            hubspot_account_id=token.hubspot_account_id,
            credential_reference="hubspot-oauth-token",
        )
