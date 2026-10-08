from collections.abc import Sequence
from typing import Protocol

from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotCompany,
    HubSpotContactCompanyAssociations,
)
from app.services.hubspot_contacts import AccessTokenProvider
from app.services.hubspot_scopes import require_scope


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

    async def search_companies(
        self,
        context: TenantContext,
        access_token: str,
        *,
        query: str,
        limit: int = 100,
        after: str | None = None,
        properties: Sequence[str] = (),
    ) -> HubSpotCompaniesPage: ...

    async def get_contact_company_associations(
        self, context: TenantContext, access_token: str, contact_id: str
    ) -> HubSpotContactCompanyAssociations: ...

    async def get_company(
        self,
        context: TenantContext,
        access_token: str,
        *,
        company_id: str,
        properties: Sequence[str] = (),
    ) -> HubSpotCompany: ...

    async def create_company(
        self, context: TenantContext, access_token: str, *, properties: dict[str, str]
    ) -> HubSpotCompany: ...

    async def update_company(
        self,
        context: TenantContext,
        access_token: str,
        *,
        company_id: str,
        properties: dict[str, str],
    ) -> HubSpotCompany: ...

    async def delete_company(
        self, context: TenantContext, access_token: str, *, company_id: str
    ) -> None: ...

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

    async def search_companies(
        self,
        context: TenantContext,
        *,
        query: str,
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
        return await self._client.search_companies(
            resolved_context,
            access_token,
            query=query,
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

    async def get_company(
        self,
        context: TenantContext,
        *,
        company_id: str,
        properties: Sequence[str] = (),
    ) -> HubSpotCompany:
        access_token, resolved_context = await self._authorized(
            context, "crm.objects.companies.read"
        )
        return await self._client.get_company(
            resolved_context, access_token, company_id=company_id, properties=properties
        )

    async def create_company(
        self,
        context: TenantContext,
        *,
        properties: dict[str, str],
    ) -> HubSpotCompany:
        access_token, resolved_context = await self._authorized(
            context, "crm.objects.companies.write"
        )
        return await self._client.create_company(
            resolved_context, access_token, properties=properties
        )

    async def update_company(
        self,
        context: TenantContext,
        *,
        company_id: str,
        properties: dict[str, str],
    ) -> HubSpotCompany:
        access_token, resolved_context = await self._authorized(
            context, "crm.objects.companies.write"
        )
        return await self._client.update_company(
            resolved_context, access_token, company_id=company_id, properties=properties
        )

    async def delete_company(self, context: TenantContext, *, company_id: str) -> None:
        access_token, resolved_context = await self._authorized(
            context, "crm.objects.companies.write"
        )
        await self._client.delete_company(
            resolved_context, access_token, company_id=company_id
        )

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