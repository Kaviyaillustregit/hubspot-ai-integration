from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.properties import (
    CRMPropertyObject,
    HubSpotPropertiesClient,
    HubSpotPropertyDefinition,
)
from app.services.hubspot_contacts import AccessTokenProvider
from app.services.hubspot_scopes import require_scope


class HubSpotPropertiesService:
    """Tenant-scoped access to HubSpot's non-sensitive property metadata."""

    def __init__(
        self, client: HubSpotPropertiesClient, token_provider: AccessTokenProvider
    ) -> None:
        self._client = client
        self._token_provider = token_provider

    async def list_properties(
        self, context: TenantContext, object_type: CRMPropertyObject
    ) -> list[HubSpotPropertyDefinition]:
        access_token, token = await self._token_provider.get_access_token(context.tenant_id)
        scope = f"crm.schemas.{object_type}.read"
        require_scope(token, scope)
        resolved = TenantContext(
            tenant_id=context.tenant_id,
            hubspot_account_id=token.hubspot_account_id,
            credential_reference="hubspot-oauth-token",
        )
        return await self._client.list_properties(resolved, access_token, object_type=object_type)
