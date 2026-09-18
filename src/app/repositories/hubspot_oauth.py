from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.oauth_models import HubSpotOAuthTokenRecord, OAuthStateRecord
from app.integrations.hubspot.oauth import (
    OAuthState,
    OAuthStateStore,
    OAuthTokenStore,
    StoredOAuthToken,
)


class HubSpotOAuthRepository(OAuthStateStore, OAuthTokenStore):
    """Tenant-scoped OAuth persistence; transaction ownership stays with services."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, state: OAuthState) -> None:
        self._session.add(
            OAuthStateRecord(
                nonce=state.nonce,
                tenant_id=state.tenant_id,
                expires_at=state.expires_at,
            )
        )

    async def consume(self, nonce: str) -> OAuthState | None:
        result = await self._session.execute(
            select(OAuthStateRecord)
            .where(
                OAuthStateRecord.nonce == nonce,
                OAuthStateRecord.expires_at > datetime.now(UTC),
            )
            .with_for_update()
        )
        record = result.scalar_one_or_none()
        if record is None:
            await self._session.execute(
                delete(OAuthStateRecord).where(OAuthStateRecord.nonce == nonce)
            )
            return None
        await self._session.delete(record)
        return OAuthState(record.tenant_id, record.nonce, record.expires_at)

    async def save_token(self, token: StoredOAuthToken) -> None:
        existing = await self._session.get(HubSpotOAuthTokenRecord, token.tenant_id)
        values = {
            "hubspot_account_id": token.hubspot_account_id,
            "encrypted_access_token": token.encrypted_access_token,
            "encrypted_refresh_token": token.encrypted_refresh_token,
            "expires_at": token.expires_at,
            "scopes": token.scopes,
            "updated_at": token.updated_at,
        }
        if existing is None:
            self._session.add(
                HubSpotOAuthTokenRecord(
                    tenant_id=token.tenant_id,
                    created_at=token.created_at,
                    **values,
                )
            )
        else:
            for field, value in values.items():
                setattr(existing, field, value)

    async def get_token(self, tenant_id: str) -> StoredOAuthToken | None:
        record = await self._session.get(HubSpotOAuthTokenRecord, tenant_id)
        if record is None:
            return None
        return StoredOAuthToken(
            tenant_id=record.tenant_id,
            hubspot_account_id=record.hubspot_account_id,
            encrypted_access_token=record.encrypted_access_token,
            encrypted_refresh_token=record.encrypted_refresh_token,
            expires_at=record.expires_at,
            scopes=record.scopes,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )