from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.secrets import SecretCipher
from app.integrations.hubspot.contacts import HubSpotContactsClient
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import HubSpotContact, HubSpotContactsPage
from app.integrations.hubspot.oauth import HubSpotTokenResponse, StoredOAuthToken
from app.services.hubspot_contacts import HubSpotAccessTokenProvider, HubSpotContactsService


class Session:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        pass


class TokenRepository:
    token: StoredOAuthToken | None = None

    def __init__(self, session: Session) -> None:
        self.session = session

    async def get_token(self, tenant_id: str) -> StoredOAuthToken | None:
        if self.token is not None and self.token.tenant_id == tenant_id:
            return self.token
        return None

    async def save_token(self, token: StoredOAuthToken) -> None:
        self.token = token


class RefreshClient:
    async def exchange_code(self, code: str) -> HubSpotTokenResponse:
        raise AssertionError("exchange is not used")

    async def refresh_token(self, refresh_token: str) -> HubSpotTokenResponse:
        assert refresh_token == "refresh-token"
        return HubSpotTokenResponse(
            access_token="refreshed-access",
            refresh_token="refreshed-refresh",
            expires_in=1800,
            hub_id=42,
            scopes=["crm.objects.contacts.read"],
        )


@pytest.mark.asyncio
async def test_contacts_client_returns_typed_page_and_sends_bearer_token():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "results": [{"id": "1", "properties": {"email": "a@example.com"}}],
                "paging": {"next": {"after": "2"}},
            },
        )

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await HubSpotContactsClient(settings, client).list_contacts(
            context,
            "access-token",
            limit=10,
            after="1",
            properties=("email", "firstname"),
        )

    assert captured["authorization"] == "Bearer access-token"
    assert captured["params"] == {
        "limit": "10",
        "after": "1",
        "properties": "email,firstname",
    }
    assert page.results == [HubSpotContact(id="1", properties={"email": "a@example.com"})]
    assert page.next_after == "2"


@pytest.mark.asyncio
async def test_expired_tenant_token_is_refreshed_before_contacts_read():
    cipher = SecretCipher(Fernet.generate_key().decode())
    TokenRepository.token = StoredOAuthToken(
        tenant_id="tenant-a",
        hubspot_account_id="42",
        encrypted_access_token=cipher.encrypt("expired-access"),
        encrypted_refresh_token=cipher.encrypt("refresh-token"),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        scopes=["crm.objects.contacts.read"],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    provider = HubSpotAccessTokenProvider(
        lambda: Session(), RefreshClient(), cipher, TokenRepository
    )
    used: list[str] = []

    class FakeContactsClient:
        async def list_contacts(self, context, access_token, **kwargs):
            used.append(access_token)
            return HubSpotContactsPage(results=[], next_after=None)

    service = HubSpotContactsService(
        FakeContactsClient(), provider  # type: ignore[arg-type]
    )
    page = await service.list_contacts(
        TenantContext("tenant-a", "", "hubspot-oauth-token")
    )

    assert used == ["refreshed-access"]
    assert page.results == []
    assert TokenRepository.token is not None
    assert TokenRepository.token.encrypted_access_token != "refreshed-access"