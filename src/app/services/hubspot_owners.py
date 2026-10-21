from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.owners import HubSpotOwner, HubSpotOwnersClient
from app.services.hubspot_contacts import AccessTokenProvider
from app.services.hubspot_scopes import require_scope


class HubSpotOwnersService:
    def __init__(self, client: HubSpotOwnersClient, token_provider: AccessTokenProvider) -> None:
        self._client = client
        self._token_provider = token_provider

    async def list_owners(self, context: TenantContext) -> list[HubSpotOwner]:
        access_token, token = await self._token_provider.get_access_token(context.tenant_id)
        require_scope(token, "crm.objects.owners.read")
        resolved = TenantContext(
            tenant_id=context.tenant_id,
            hubspot_account_id=token.hubspot_account_id,
            credential_reference="hubspot-oauth-token",
        )
        return await self._client.list_owners(resolved, access_token)
