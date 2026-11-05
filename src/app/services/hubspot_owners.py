import time

from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.owners import HubSpotOwner, HubSpotOwnersClient
from app.services.hubspot_contacts import AccessTokenProvider
from app.services.hubspot_scopes import require_scope


class HubSpotOwnersService:
    def __init__(
        self,
        client: HubSpotOwnersClient,
        token_provider: AccessTokenProvider,
        *,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._client = client
        self._token_provider = token_provider
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, list[HubSpotOwner]]] = {}

    def clear_cache(self, tenant_id: str | None = None) -> None:
        if tenant_id:
            self._cache.pop(tenant_id, None)
        else:
            self._cache.clear()

    async def list_owners(self, context: TenantContext) -> list[HubSpotOwner]:
        now = time.monotonic()
        if context.tenant_id in self._cache:
            cached_at, cached_owners = self._cache[context.tenant_id]
            if now - cached_at < self._cache_ttl:
                return cached_owners

        access_token, token = await self._token_provider.get_access_token(context.tenant_id)
        require_scope(token, "crm.objects.owners.read")
        resolved = TenantContext(
            tenant_id=context.tenant_id,
            hubspot_account_id=token.hubspot_account_id,
            credential_reference="hubspot-oauth-token",
        )
        owners = await self._client.list_owners(resolved, access_token)
        self._cache[context.tenant_id] = (now, owners)
        return owners
