from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.secrets import SecretCipher
from app.db.session import UnitOfWork
from app.integrations.hubspot.oauth import (
    HubSpotOAuthContract,
    HubSpotTokenResponse,
    OAuthState,
    OAuthTokenClient,
    StoredOAuthToken,
)
from app.repositories.hubspot_oauth import HubSpotOAuthRepository


class OAuthRepositoryFactory(Protocol):
    def __call__(self, session: AsyncSession) -> "OAuthRepository": ...


class OAuthRepository(Protocol):
    async def save(self, state: OAuthState) -> None: ...

    async def consume(self, nonce: str) -> OAuthState | None: ...

    async def save_token(self, token: StoredOAuthToken) -> None: ...

    async def get_token(self, tenant_id: str) -> StoredOAuthToken | None: ...


class OAuthConnectionResult:
    def __init__(self, tenant_id: str, hubspot_account_id: str, expires_at: datetime) -> None:
        self.tenant_id = tenant_id
        self.hubspot_account_id = hubspot_account_id
        self.expires_at = expires_at


class HubSpotOAuthService:
    def __init__(
        self,
        settings: Settings,
        session_factory: Callable[[], AsyncSession],
        token_client: OAuthTokenClient,
        cipher: SecretCipher,
        repository_factory: OAuthRepositoryFactory = HubSpotOAuthRepository,
    ) -> None:
        self._contract = HubSpotOAuthContract(settings)
        self._session_factory = session_factory
        self._token_client = token_client
        self._cipher = cipher
        self._repository_factory = repository_factory

    async def start(self, tenant_id: str) -> tuple[str, str]:
        state = self._contract.create_state(tenant_id)
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = self._repository(unit_of_work)
            await repository.save(state)
            await unit_of_work.commit()
        return self._contract.authorization_url(state), state.nonce

    async def exchange_code(self, code: str, nonce: str) -> OAuthConnectionResult:
        state = await self._consume_state(nonce)
        if state is None:
            raise ValueError("OAuth state is invalid or expired")
        token = await self._token_client.exchange_code(code)
        return await self._store_token(state.tenant_id, token)

    async def refresh(self, tenant_id: str) -> OAuthConnectionResult:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = self._repository(unit_of_work)
            existing = await repository.get_token(tenant_id)
        if existing is None:
            raise ValueError("HubSpot OAuth connection was not found")
        token = await self._token_client.refresh_token(
            self._cipher.decrypt(existing.encrypted_refresh_token)
        )
        return await self._store_token(tenant_id, token, existing.created_at)

    async def _consume_state(self, nonce: str) -> OAuthState | None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = self._repository(unit_of_work)
            state = await repository.consume(nonce)
            await unit_of_work.commit()
            return state

    async def _store_token(
        self,
        tenant_id: str,
        token: HubSpotTokenResponse,
        created_at: datetime | None = None,
    ) -> OAuthConnectionResult:
        now = datetime.now(UTC)
        stored = StoredOAuthToken(
            tenant_id=tenant_id,
            hubspot_account_id=str(token.hub_id),
            encrypted_access_token=self._cipher.encrypt(token.access_token),
            encrypted_refresh_token=self._cipher.encrypt(token.refresh_token),
            expires_at=now + timedelta(seconds=token.expires_in),
            scopes=token.scopes,
            created_at=created_at or now,
            updated_at=now,
        )
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = self._repository(unit_of_work)
            await repository.save_token(stored)
            await unit_of_work.commit()
        return OAuthConnectionResult(tenant_id, stored.hubspot_account_id, stored.expires_at)

    def _repository(self, unit_of_work: UnitOfWork) -> OAuthRepository:
        if unit_of_work.session is None:
            raise RuntimeError("UnitOfWork session is unavailable")
        return self._repository_factory(unit_of_work.session)