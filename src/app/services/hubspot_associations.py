from collections.abc import Sequence
from typing import Protocol

from app.integrations.hubspot.associations import CRMObjectType
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import HubSpotRecord
from app.services.hubspot_contacts import AccessTokenProvider
from app.services.hubspot_scopes import require_scope

_READ_SCOPES: dict[str, str] = {
    "contacts": "crm.objects.contacts.read",
    "companies": "crm.objects.companies.read",
    "deals": "crm.objects.deals.read",
}


class AssociationsClient(Protocol):
    async def associate(
        self,
        context: TenantContext,
        access_token: str,
        *,
        from_type: CRMObjectType,
        from_id: str,
        to_type: CRMObjectType,
        to_id: str,
    ) -> None: ...

    async def list_associated_ids(
        self,
        context: TenantContext,
        access_token: str,
        *,
        from_type: CRMObjectType,
        from_id: str,
        to_type: CRMObjectType,
        limit: int = 100,
    ) -> list[str]: ...

    async def read_records(
        self,
        context: TenantContext,
        access_token: str,
        *,
        object_type: CRMObjectType,
        record_ids: Sequence[str],
        properties: Sequence[str],
    ) -> list[HubSpotRecord]: ...


class HubSpotAssociationsService:
    def __init__(self, client: AssociationsClient, token_provider: AccessTokenProvider) -> None:
        self._client = client
        self._token_provider = token_provider

    async def associate(
        self,
        context: TenantContext,
        *,
        from_type: CRMObjectType,
        from_id: str,
        to_type: CRMObjectType,
        to_id: str,
    ) -> None:
        # HubSpot does not document a single scope for association writes; a refusal
        # surfaces as IntegrationPermissionError from the adapter.
        access_token, resolved = await self._authorized(context, ())
        await self._client.associate(
            resolved,
            access_token,
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            to_id=to_id,
        )

    async def associated_records(
        self,
        context: TenantContext,
        *,
        from_type: CRMObjectType,
        from_id: str,
        to_type: CRMObjectType,
        properties: Sequence[str],
        limit: int = 100,
    ) -> list[HubSpotRecord]:
        access_token, resolved = await self._authorized(
            context, (_READ_SCOPES[from_type], _READ_SCOPES[to_type])
        )
        record_ids = await self._client.list_associated_ids(
            resolved,
            access_token,
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            limit=limit,
        )
        records: list[HubSpotRecord] = []
        for offset in range(0, len(record_ids), 100):
            records.extend(
                await self._client.read_records(
                    resolved,
                    access_token,
                    object_type=to_type,
                    record_ids=record_ids[offset : offset + 100],
                    properties=properties,
                )
            )
        return records

    async def _authorized(
        self, context: TenantContext, scopes: Sequence[str]
    ) -> tuple[str, TenantContext]:
        access_token, token = await self._token_provider.get_access_token(context.tenant_id)
        for scope in scopes:
            require_scope(token, scope)
        return access_token, TenantContext(
            tenant_id=context.tenant_id,
            hubspot_account_id=token.hubspot_account_id,
            credential_reference="hubspot-oauth-token",
        )
