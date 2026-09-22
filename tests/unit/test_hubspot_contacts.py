import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.secrets import SecretCipher
from app.integrations.errors import (
    IntegrationAuthenticationError,
    IntegrationError,
    IntegrationRateLimitError,
    IntegrationTimeoutError,
)
from app.integrations.hubspot.contacts import HubSpotContactsClient
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import HubSpotContact, HubSpotContactsPage
from app.integrations.hubspot.oauth import HubSpotTokenResponse, StoredOAuthToken
from app.services.hubspot_contacts import (
    HubSpotAccessTokenProvider,
    HubSpotContactsService,
)


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

@pytest.mark.asyncio
async def test_contacts_client_creates_contact_and_sends_bearer_token():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["content_type"] = request.headers["Content-Type"]
        captured["json"] = request.read().decode()

        return httpx.Response(
            201,
            json={
                "id": "123",
                "properties": {
                    "email": "arun@test.com",
                    "firstname": "Arun",
                    "lastname": "Kumar",
                },
            },
        )

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contact = await HubSpotContactsClient(
            settings, client
        ).create_contact(
            context,
            "access-token",
            properties={
                "email": "arun@test.com",
                "firstname": "Arun",
                "lastname": "Kumar",
            },
        )

    assert captured["authorization"] == "Bearer access-token"
    assert captured["content_type"] == "application/json"
    payload = json.loads(str(captured["json"]))
    assert payload["properties"]["email"] == "arun@test.com"
    assert payload["properties"]["firstname"] == "Arun"
    assert payload["properties"]["lastname"] == "Kumar"
    assert contact == HubSpotContact(
        id="123",
        properties={
            "email": "arun@test.com",
            "firstname": "Arun",
            "lastname": "Kumar",
        },
    )

@pytest.mark.asyncio
async def test_contacts_client_updates_contact_and_sends_bearer_token():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["content_type"] = request.headers["Content-Type"]
        captured["json"] = json.loads(request.read().decode())

        return httpx.Response(
            200,
            json={
                "id": "123",
                "properties": {
                    "email": "arun@test.com",
                    "firstname": "Arun",
                    "lastname": "Kumar",
                    "jobtitle": "Senior AI Engineer",
                },
            },
        )

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contact = await HubSpotContactsClient(
            settings,
            client,
        ).update_contact(
            context,
            "access-token",
            contact_id="123",
            properties={
                "firstname": "Arun",
                "lastname": "Kumar",
                "jobtitle": "Senior AI Engineer",
            },
        )

    assert captured["method"] == "PATCH"
    assert captured["url"] == (
        "https://api.hubapi.com/crm/v3/objects/contacts/123"
    )
    assert captured["authorization"] == "Bearer access-token"
    assert captured["content_type"] == "application/json"
    assert captured["json"] == {
        "properties": {
            "firstname": "Arun",
            "lastname": "Kumar",
            "jobtitle": "Senior AI Engineer",
        }
    }

    assert contact == HubSpotContact(
        id="123",
        properties={
            "email": "arun@test.com",
            "firstname": "Arun",
            "lastname": "Kumar",
            "jobtitle": "Senior AI Engineer",
        },
    )

@pytest.mark.asyncio
async def test_contacts_client_update_handles_authentication_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": "Unauthorized"},
        )

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contacts_client = HubSpotContactsClient(settings, client)

        with pytest.raises(IntegrationAuthenticationError):
            await contacts_client.update_contact(
                context,
                "access-token",
                contact_id="123",
                properties={"firstname": "Arun"},
            )

@pytest.mark.asyncio
async def test_contacts_client_update_handles_rate_limit_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"message": "Rate limit exceeded"},
        )

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contacts_client = HubSpotContactsClient(settings, client)

        with pytest.raises(IntegrationRateLimitError):
            await contacts_client.update_contact(
                context,
                "access-token",
                contact_id="123",
                properties={"firstname": "Arun"},
            )

@pytest.mark.asyncio
async def test_contacts_client_update_handles_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request timed out")

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contacts_client = HubSpotContactsClient(settings, client)

        with pytest.raises(IntegrationTimeoutError):
            await contacts_client.update_contact(
                context,
                "access-token",
                contact_id="123",
                properties={"firstname": "Arun"},
            )

@pytest.mark.asyncio
async def test_contacts_client_update_handles_generic_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"message": "Internal Server Error"},
        )

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contacts_client = HubSpotContactsClient(settings, client)

        with pytest.raises(IntegrationError):
            await contacts_client.update_contact(
                context,
                "access-token",
                contact_id="123",
                properties={"firstname": "Arun"},
            )

@pytest.mark.asyncio
async def test_contacts_client_finds_contact_by_email():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]

        payload = json.loads(request.read().decode())
        captured["json"] = payload

        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "123",
                        "properties": {
                            "email": "arun@test.com",
                            "firstname": "Arun",
                            "lastname": "Kumar",
                        },
                    }
                ]
            },
        )

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contact = await HubSpotContactsClient(
            settings,
            client,
        ).find_contact_by_email(
            context,
            "access-token",
            email="arun@test.com",
        )

    assert captured["method"] == "POST"
    assert captured["url"] == (
        "https://api.hubapi.com/crm/v3/objects/contacts/search"
    )
    assert captured["authorization"] == "Bearer access-token"

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["limit"] == 1
    assert payload["properties"] == [
        "email",
        "firstname",
        "lastname",
    ]
    assert payload["filterGroups"][0]["filters"][0] == {
        "propertyName": "email",
        "operator": "EQ",
        "value": "arun@test.com",
    }

    assert contact == HubSpotContact(
        id="123",
        properties={
            "email": "arun@test.com",
            "firstname": "Arun",
            "lastname": "Kumar",
        },
    )

@pytest.mark.asyncio
async def test_contacts_client_returns_none_when_contact_does_not_exist():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [],
            },
        )

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contact = await HubSpotContactsClient(
            settings,
            client,
        ).find_contact_by_email(
            context,
            "access-token",
            email="missing@test.com",
        )

    assert contact is None

@pytest.mark.asyncio
async def test_contacts_client_find_by_email_handles_authentication_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contacts_client = HubSpotContactsClient(settings, client)

        with pytest.raises(IntegrationAuthenticationError):
            await contacts_client.find_contact_by_email(
                context,
                "access-token",
                email="arun@test.com",
            )

@pytest.mark.asyncio
async def test_contacts_client_find_by_email_handles_rate_limit_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "Rate limit exceeded"})

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contacts_client = HubSpotContactsClient(settings, client)

        with pytest.raises(IntegrationRateLimitError):
            await contacts_client.find_contact_by_email(
                context,
                "access-token",
                email="arun@test.com",
            )

@pytest.mark.asyncio
async def test_contacts_client_find_by_email_handles_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request timed out")

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contacts_client = HubSpotContactsClient(settings, client)

        with pytest.raises(IntegrationTimeoutError):
            await contacts_client.find_contact_by_email(
                context,
                "access-token",
                email="arun@test.com",
            )

@pytest.mark.asyncio
async def test_contacts_client_find_by_email_handles_generic_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"message": "Internal Server Error"},
        )

    settings = Settings()
    context = TenantContext("tenant-a", "42", "hubspot-oauth-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        contacts_client = HubSpotContactsClient(settings, client)

        with pytest.raises(IntegrationError):
            await contacts_client.find_contact_by_email(
                context,
                "access-token",
                email="arun@test.com",
            )

@pytest.mark.asyncio
async def test_contacts_service_finds_contact_by_email():
    expected_contact = HubSpotContact(
        id="123",
        properties={
            "email": "arun@test.com",
            "firstname": "Arun",
            "lastname": "Kumar",
        },
    )

    class FakeTokenProvider:
        async def get_access_token(
            self,
            tenant_id: str,
        ) -> tuple[str, StoredOAuthToken]:
            assert tenant_id == "tenant-a"

            token = StoredOAuthToken(
                tenant_id="tenant-a",
                hubspot_account_id="42",
                encrypted_access_token="encrypted-access",
                encrypted_refresh_token="encrypted-refresh",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=["crm.objects.contacts.read"],
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

            return "access-token", token

    class FakeContactsClient:
        async def find_contact_by_email(
            self,
            context,
            access_token: str,
            *,
            email: str,
        ):
            assert context.tenant_id == "tenant-a"
            assert context.hubspot_account_id == "42"
            assert context.credential_reference == "hubspot-oauth-token"
            assert access_token == "access-token"
            assert email == "arun@test.com"

            return expected_contact

    service = HubSpotContactsService(
        FakeContactsClient(),  # type: ignore[arg-type]
        FakeTokenProvider(),  # type: ignore[arg-type]
    )

    contact = await service.find_contact_by_email(
        "tenant-a",
        "arun@test.com",
    )

    assert contact == expected_contact

@pytest.mark.asyncio
async def test_contacts_service_blocks_duplicate_contact_creation():
    existing_contact = HubSpotContact(
        id="123",
        properties={
            "email": "arun@test.com",
            "firstname": "Arun",
            "lastname": "Kumar",
        },
    )

    class FakeTokenProvider:
        async def get_access_token(
            self,
            tenant_id: str,
        ) -> tuple[str, StoredOAuthToken]:
            assert tenant_id == "tenant-a"

            token = StoredOAuthToken(
                tenant_id="tenant-a",
                hubspot_account_id="42",
                encrypted_access_token="encrypted-access",
                encrypted_refresh_token="encrypted-refresh",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=["crm.objects.contacts.read"],
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

            return "access-token", token

    class FakeContactsClient:
        async def find_contact_by_email(
            self,
            context,
            access_token: str,
            *,
            email: str,
        ):
            assert context.tenant_id == "tenant-a"
            assert access_token == "access-token"
            assert email == "arun@test.com"
            return existing_contact

        async def create_contact(
            self,
            context,
            access_token: str,
            *,
            properties: dict[str, str | None],
        ):
            raise AssertionError(
                "create_contact must not be called for a duplicate"
            )

    service = HubSpotContactsService(
        FakeContactsClient(),  # type: ignore[arg-type]
        FakeTokenProvider(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="already exists"):
        await service.create_contact(
            TenantContext("tenant-a", "", "hubspot-oauth-token"),
            properties={
                "email": "arun@test.com",
                "firstname": "Arun",
                "lastname": "Kumar",
            },
        )
@pytest.mark.asyncio
async def test_contacts_service_creates_contact_when_email_is_not_duplicate():
    created_contact = HubSpotContact(
        id="456",
        properties={
            "email": "new@test.com",
            "firstname": "New",
            "lastname": "User",
        },
    )

    class FakeTokenProvider:
        async def get_access_token(
            self,
            tenant_id: str,
        ) -> tuple[str, StoredOAuthToken]:
            assert tenant_id == "tenant-a"

            token = StoredOAuthToken(
                tenant_id="tenant-a",
                hubspot_account_id="42",
                encrypted_access_token="encrypted-access",
                encrypted_refresh_token="encrypted-refresh",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=["crm.objects.contacts.write"],
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

            return "access-token", token

    class FakeContactsClient:
        async def find_contact_by_email(
            self,
            context,
            access_token: str,
            *,
            email: str,
        ):
            assert context.tenant_id == "tenant-a"
            assert access_token == "access-token"
            assert email == "new@test.com"
            return None

        async def create_contact(
            self,
            context,
            access_token: str,
            *,
            properties: dict[str, str | None],
        ):
            assert context.tenant_id == "tenant-a"
            assert context.hubspot_account_id == "42"
            assert access_token == "access-token"
            assert properties == {
                "email": "new@test.com",
                "firstname": "New",
                "lastname": "User",
            }
            return created_contact

    service = HubSpotContactsService(
        FakeContactsClient(),  # type: ignore[arg-type]
        FakeTokenProvider(),  # type: ignore[arg-type]
    )

    contact = await service.create_contact(
        TenantContext("tenant-a", "", "hubspot-oauth-token"),
        properties={
            "email": "new@test.com",
            "firstname": "New",
            "lastname": "User",
        },
    )

    assert contact == created_contact

@pytest.mark.asyncio
async def test_contacts_service_updates_contact():
    expected_contact = HubSpotContact(
        id="123",
        properties={
            "email": "arun@test.com",
            "firstname": "Arun",
            "lastname": "Kumar",
            "jobtitle": "Senior AI Engineer",
        },
    )

    class FakeTokenProvider:
        async def get_access_token(
            self,
            tenant_id: str,
        ) -> tuple[str, StoredOAuthToken]:
            assert tenant_id == "tenant-a"

            token = StoredOAuthToken(
                tenant_id="tenant-a",
                hubspot_account_id="42",
                encrypted_access_token="encrypted-access",
                encrypted_refresh_token="encrypted-refresh",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=["crm.objects.contacts.write"],
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

            return "access-token", token

    class FakeContactsClient:
        async def update_contact(
            self,
            context,
            access_token: str,
            *,
            contact_id: str,
            properties: dict[str, str | None],
        ):
            assert context.tenant_id == "tenant-a"
            assert context.hubspot_account_id == "42"
            assert context.credential_reference == "hubspot-oauth-token"
            assert access_token == "access-token"
            assert contact_id == "123"
            assert properties == {
                "firstname": "Arun",
                "lastname": "Kumar",
                "jobtitle": "Senior AI Engineer",
            }

            return expected_contact

    service = HubSpotContactsService(
        FakeContactsClient(),  # type: ignore[arg-type]
        FakeTokenProvider(),  # type: ignore[arg-type]
    )

    contact = await service.update_contact(
        TenantContext("tenant-a", "", "hubspot-oauth-token"),
        contact_id="123",
        properties={
            "firstname": "Arun",
            "lastname": "Kumar",
            "jobtitle": "Senior AI Engineer",
        },
    )

    assert contact == expected_contact