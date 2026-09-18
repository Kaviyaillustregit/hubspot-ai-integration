import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.secrets import SecretCipher
from app.integrations.hubspot.oauth import (
    HubSpotOAuthTokenClient,
    HubSpotTokenResponse,
    OAuthState,
    StoredOAuthToken,
)
from app.services.hubspot_oauth import HubSpotOAuthService


class FakeSession:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        pass


class FakeRepository:
    states: dict[str, OAuthState] = {}
    tokens: dict[str, StoredOAuthToken] = {}

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def save(self, state: OAuthState) -> None:
        self.states[state.nonce] = state

    async def consume(self, nonce: str) -> OAuthState | None:
        return self.states.pop(nonce, None)

    async def save_token(self, token: StoredOAuthToken) -> None:
        self.tokens[token.tenant_id] = token

    async def get_token(self, tenant_id: str) -> StoredOAuthToken | None:
        return self.tokens.get(tenant_id)


class FakeTokenClient:
    def __init__(self) -> None:
        self.refreshed: list[str] = []

    async def exchange_code(self, code: str) -> HubSpotTokenResponse:
        return HubSpotTokenResponse(
            access_token=f"access-{code}",
            refresh_token=f"refresh-{code}",
            expires_in=1800,
            hub_id=123,
            scopes=["oauth"],
        )

    async def refresh_token(self, refresh_token: str) -> HubSpotTokenResponse:
        self.refreshed.append(refresh_token)
        return HubSpotTokenResponse(
            access_token="refreshed-access",
            refresh_token="refreshed-refresh",
            expires_in=1800,
            hub_id=123,
            scopes=["oauth"],
        )


def service() -> tuple[HubSpotOAuthService, FakeTokenClient]:
    FakeRepository.states = {}
    FakeRepository.tokens = {}
    token_client = FakeTokenClient()
    settings = Settings(
        hubspot_client_id="test-client",
        hubspot_client_secret="test-secret",
        hubspot_redirect_uri="http://localhost/callback",
    )
    application = HubSpotOAuthService(
        settings,
        lambda: FakeSession(),
        token_client,
        SecretCipher(Fernet.generate_key().decode()),
        FakeRepository,
    )
    return application, token_client


@pytest.mark.asyncio
async def test_code_exchange_consumes_state_once_and_stores_only_encrypted_tokens():
    application, _ = service()
    _, state = await application.start("tenant-a")

    result = await application.exchange_code("code-a", state)

    assert result.tenant_id == "tenant-a"
    stored = FakeRepository.tokens["tenant-a"]
    assert "access-code-a" not in stored.encrypted_access_token
    assert "refresh-code-a" not in stored.encrypted_refresh_token
    with pytest.raises(ValueError, match="invalid or expired"):
        await application.exchange_code("code-a", state)


@pytest.mark.asyncio
async def test_refresh_is_tenant_scoped_and_decrypts_only_inside_service():
    application, token_client = service()
    _, state = await application.start("tenant-a")
    await application.exchange_code("code-a", state)

    result = await application.refresh("tenant-a")

    assert result.tenant_id == "tenant-a"
    assert token_client.refreshed == ["refresh-code-a"]
    with pytest.raises(ValueError, match="not found"):
        await application.refresh("tenant-b")


@pytest.mark.asyncio
async def test_hubspot_v3_client_posts_form_data_without_logging_or_returning_secrets():
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["form"] = request.content
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 1800,
                "hub_id": 123,
                "scopes": ["oauth"],
            },
        )

    settings = Settings(
        hubspot_client_id="test-client",
        hubspot_client_secret="test-secret",
        hubspot_redirect_uri="http://localhost/callback",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        token = await HubSpotOAuthTokenClient(settings, client).exchange_code("code")

    assert b"grant_type=authorization_code" in captured["form"]
    assert token.hub_id == 123