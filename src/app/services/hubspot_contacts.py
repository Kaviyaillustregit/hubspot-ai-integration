from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.secrets import SecretCipher
from app.db.session import UnitOfWork
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import HubSpotContact, HubSpotContactsPage
from app.integrations.hubspot.oauth import OAuthTokenClient, StoredOAuthToken


class OAuthTokenRepository(Protocol):
    async def get_token(self, tenant_id: str) -> StoredOAuthToken | None: ...

    async def save_token(self, token: StoredOAuthToken) -> None: ...


class OAuthRepositoryFactory(Protocol):
    def __call__(self, session: AsyncSession) -> OAuthTokenRepository: ...


class AccessTokenProvider(Protocol):
    async def get_access_token(
        self,
        tenant_id: str,
    ) -> tuple[str, StoredOAuthToken]: ...


class ContactsClient(Protocol):
    async def list_contacts(
        self,
        context: TenantContext,
        access_token: str,
        *,
        limit: int = 100,
        after: str | None = None,
        properties: Sequence[str] = (),
    ) -> HubSpotContactsPage: ...

    async def find_contact_by_email(
        self,
        context: TenantContext,
        access_token: str,
        *,
        email: str,
    ) -> HubSpotContact | None: ...

    async def create_contact(
        self,
        context: TenantContext,
        access_token: str,
        *,
        properties: dict[str, str | None],
    ) -> HubSpotContact: ...


class HubSpotAccessTokenProvider:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        token_client: OAuthTokenClient,
        cipher: SecretCipher,
        repository_factory: OAuthRepositoryFactory,
    ) -> None:
        self._session_factory = session_factory
        self._token_client = token_client
        self._cipher = cipher
        self._repository_factory = repository_factory

    async def get_access_token(
        self,
        tenant_id: str,
    ) -> tuple[str, StoredOAuthToken]:
        existing = await self._get_token(tenant_id)

        if existing is None:
            raise ValueError("HubSpot OAuth connection was not found")

        if existing.expires_at > datetime.now(UTC) + timedelta(minutes=1):
            return (
                self._cipher.decrypt(existing.encrypted_access_token),
                existing,
            )

        refreshed = await self._token_client.refresh_token(
            self._cipher.decrypt(existing.encrypted_refresh_token)
        )

        now = datetime.now(UTC)

        updated = StoredOAuthToken(
            tenant_id=tenant_id,
            hubspot_account_id=str(refreshed.hub_id),
            encrypted_access_token=self._cipher.encrypt(
                refreshed.access_token
            ),
            encrypted_refresh_token=self._cipher.encrypt(
                refreshed.refresh_token
            ),
            expires_at=now + timedelta(seconds=refreshed.expires_in),
            scopes=refreshed.scopes,
            created_at=existing.created_at,
            updated_at=now,
        )

        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = self._repository(unit_of_work)
            await repository.save_token(updated)
            await unit_of_work.commit()

        return refreshed.access_token, updated

    async def _get_token(
        self,
        tenant_id: str,
    ) -> StoredOAuthToken | None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = self._repository(unit_of_work)
            return await repository.get_token(tenant_id)

    def _repository(
        self,
        unit_of_work: UnitOfWork,
    ) -> OAuthTokenRepository:
        if unit_of_work.session is None:
            raise RuntimeError("UnitOfWork session is unavailable")

        return self._repository_factory(unit_of_work.session)


class HubSpotContactsService:
    def __init__(
        self,
        client: ContactsClient,
        token_provider: AccessTokenProvider,
    ) -> None:
        self._client = client
        self._token_provider = token_provider

    async def list_contacts(
        self,
        context: TenantContext,
        *,
        limit: int = 100,
        after: str | None = None,
        properties: Sequence[str] = (),
    ) -> HubSpotContactsPage:
        access_token, token = await self._token_provider.get_access_token(
            context.tenant_id
        )

        context = TenantContext(
            tenant_id=context.tenant_id,
            hubspot_account_id=token.hubspot_account_id,
            credential_reference="hubspot-oauth-token",
        )

        return await self._client.list_contacts(
            context,
            access_token,
            limit=limit,
            after=after,
            properties=properties,
        )

    async def find_contact_by_email(
        self,
        tenant_id: str,
        email: str,
    ) -> HubSpotContact | None:
        access_token, token = await self._token_provider.get_access_token(
            tenant_id
        )

        context = TenantContext(
            tenant_id=tenant_id,
            hubspot_account_id=token.hubspot_account_id,
            credential_reference="hubspot-oauth-token",
        )

        return await self._client.find_contact_by_email(
            context,
            access_token,
            email=email,
        )

    async def create_contact(
        self,
        context: TenantContext,
        *,
        properties: dict[str, str | None],
    ) -> HubSpotContact:
        access_token, token = await self._token_provider.get_access_token(
            context.tenant_id
        )

        context = TenantContext(
            tenant_id=context.tenant_id,
            hubspot_account_id=token.hubspot_account_id,
            credential_reference="hubspot-oauth-token",
        )

        email = properties.get("email")

        if email:
            existing_contact = await self._client.find_contact_by_email(
                context,
                access_token,
                email=email,
            )

            if existing_contact is not None:
                raise ValueError(
                    f"HubSpot contact with email '{email}' already exists"
                )

        return await self._client.create_contact(
            context,
            access_token,
            properties=properties,
        )