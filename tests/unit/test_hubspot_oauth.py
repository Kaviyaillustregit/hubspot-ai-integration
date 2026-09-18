from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.hubspot_oauth import get_oauth_service
from app.core.config import Settings
from app.integrations.hubspot.oauth import HubSpotOAuthContract


def oauth_settings() -> Settings:
    return Settings(
        hubspot_client_id="test-client-id",
        hubspot_redirect_uri="http://testserver/api/v1/auth/hubspot/callback",
        hubspot_oauth_scopes="crm.objects.contacts.read crm.objects.contacts.write",
    )


class FakeRouteOAuthService:
    async def start(self, tenant_id: str) -> tuple[str, str]:
        return "https://app.hubspot.com/oauth/authorize?state=test-state", "test-state"

    async def exchange_code(self, code: str, nonce: str) -> None:
        return None


def test_oauth_start_builds_tenant_bound_authorization_contract():
    contract = HubSpotOAuthContract(oauth_settings())
    state = contract.create_state("tenant-a")
    parsed = urlparse(contract.authorization_url(state))
    query = parse_qs(parsed.query)

    assert parsed.netloc == "app.hubspot.com"
    assert query["client_id"] == ["test-client-id"]
    assert query["redirect_uri"] == [
        "http://testserver/api/v1/auth/hubspot/callback"
    ]
    assert query["scope"] == ["crm.objects.contacts.read crm.objects.contacts.write"]
    assert query["state"] == [state.nonce]
    assert state.tenant_id == "tenant-a"


def test_oauth_start_route_does_not_require_real_credentials():
    application = create_app(oauth_settings())
    application.dependency_overrides[get_oauth_service] = FakeRouteOAuthService
    with TestClient(application) as client:
        response = client.get("/api/v1/auth/hubspot/start?tenant_id=tenant-a")

    assert response.status_code == 200
    assert response.json()["state"]
    assert response.json()["authorization_url"].startswith("https://app.hubspot.com/oauth/authorize")


def test_oauth_start_returns_safe_configuration_error_when_unconfigured():
    unconfigured = Settings(
        hubspot_client_id=None,
        hubspot_client_secret=None,
        hubspot_redirect_uri=None,
        hubspot_token_encryption_key=None,
    )
    with TestClient(create_app(unconfigured)) as client:
        response = client.get("/api/v1/auth/hubspot/start?tenant_id=tenant-a")

    assert response.status_code == 503
    assert response.json()["code"] == "oauth_not_configured"
    assert "client" not in response.json()["message"]


def test_oauth_callback_denial_does_not_expose_provider_details():
    application = create_app(Settings())
    application.dependency_overrides[get_oauth_service] = FakeRouteOAuthService
    with TestClient(application) as client:
        response = client.get(
            "/api/v1/auth/hubspot/callback?error=access_denied&error_description=secret"
        )

    assert response.status_code == 400
    assert response.json() == {
        "status": "denied",
        "message": "HubSpot authorization was denied",
    }


def test_oauth_callback_completes_without_exposing_tokens():
    application = create_app(Settings())
    application.dependency_overrides[get_oauth_service] = FakeRouteOAuthService
    with TestClient(application) as client:
        response = client.get(
            "/api/v1/auth/hubspot/callback?code=test-code&state=test-state"
        )

    assert response.status_code == 200
    assert response.json()["status"] == "connected"
    assert "test-code" not in response.text