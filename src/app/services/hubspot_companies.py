from collections.abc import Sequence
from typing import Protocol

from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotContactCompanyAssociations,
)
from app.services.hubspot_contacts import AccessTokenProvider


class CompaniesClient(Protocol):
    async def list_companies(
        self,
        context: TenantContext,
        access_token: str,
        *,
        limit: int = 100,
        after: str | None = None,
        properties: Sequence[str] = (),
    ) -> HubSpotCompaniesPage: ...

    async def get_contact_company_associations(
        self, context: TenantContext, access_token: str, contact_id: str
    ) -> HubSpotContactCompanyAssociations: ...


class HubSpotCompaniesService:
    def __init__(self, client: CompaniesClient, token_provider: AccessTokenProvider) -> None:
        self._client = client
        self._token_provider = token_provider

    async def list_companies(
        self,
        context: TenantContext,
        *,
        limit: int = 100,
        after: str | None = None,
        properties: Sequence[str] = (),
    ) -> HubSpotCompaniesPage:
        access_token, token = await self._token_provider.get_access_token(context.tenant_id)
        resolved_context = TenantContext(
            tenant_id=context.tenant_id,
            hubspot_account_id=token.hubspot_account_id,
            credential_reference="hubspot-oauth-token",
        )
        return await self._client.list_companies(
            resolved_context,
            access_token,
            limit=limit,
            after=after,
            properties=properties,
        )

    async def get_contact_company_associations(
        self, context: TenantContext, contact_id: str
    ) -> HubSpotContactCompanyAssociations:
        access_token, token = await self._token_provider.get_access_token(context.tenant_id)
        resolved_context = TenantContext(
            tenant_id=context.tenant_id,
            hubspot_account_id=token.hubspot_account_id,
            credential_reference="hubspot-oauth-token",
        )
        return await self._client.get_contact_company_associations(
            resolved_context, access_token, contact_id
        )