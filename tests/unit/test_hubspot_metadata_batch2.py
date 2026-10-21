from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import Settings
from app.integrations.errors import IntegrationError, IntegrationPermissionError
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.oauth import StoredOAuthToken
from app.integrations.hubspot.owners import HubSpotOwnersClient
from app.integrations.hubspot.properties import HubSpotPropertiesClient
from app.services.hubspot_owners import HubSpotOwnersService
from app.services.hubspot_properties import HubSpotPropertiesService

CONTEXT = TenantContext("tenant-a", "42", "hubspot-oauth-token")


class Recorder:
    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, payload = self.responses.pop(0)
        return httpx.Response(status, json=payload)


async def call(client_type, recorder: Recorder, method: str, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as http:
        client = client_type(Settings(), http)
        return await getattr(client, method)(CONTEXT, "access-token", **kwargs)


@pytest.mark.asyncio
async def test_property_catalog_follows_cursor_and_keeps_non_sensitive_filter():
    recorder = Recorder(
        [
            (
                200,
                {
                    "results": [{"name": "name", "label": "Name"}],
                    "paging": {"next": {"after": "2"}},
                },
            ),
            (200, {"results": [{"name": "industry", "label": "Industry"}]}),
        ]
    )

    result = await call(
        HubSpotPropertiesClient,
        recorder,
        "list_properties",
        object_type="companies",
    )

    assert [item.name for item in result] == ["name", "industry"]
    assert len(recorder.requests) == 2
    first_query = dict(recorder.requests[0].url.params)
    second_query = dict(recorder.requests[1].url.params)
    assert first_query["dataSensitivity"] == "non_sensitive"
    assert second_query["after"] == "2"


@pytest.mark.asyncio
async def test_property_catalog_rejects_invalid_page_and_repeated_cursor():
    invalid = Recorder([(200, {"results": [{"label": "Missing internal name"}]})])
    with pytest.raises(IntegrationError, match="catalog response was invalid"):
        await call(
            HubSpotPropertiesClient,
            invalid,
            "list_properties",
            object_type="contacts",
        )

    repeated = Recorder(
        [
            (200, {"results": [], "paging": {"next": {"after": "same"}}}),
            (200, {"results": [], "paging": {"next": {"after": "same"}}}),
        ]
    )
    with pytest.raises(IntegrationError, match="repeated a pagination cursor"):
        await call(
            HubSpotPropertiesClient,
            repeated,
            "list_properties",
            object_type="contacts",
        )


@pytest.mark.asyncio
async def test_owner_listing_follows_cursor_and_maps_names():
    recorder = Recorder(
        [
            (
                200,
                {
                    "results": [{"id": "7", "firstName": "Riley", "lastName": "Smith"}],
                    "paging": {"next": {"after": "2"}},
                },
            ),
            (200, {"results": [{"id": "8", "email": "owner@example.test"}]}),
        ]
    )

    result = await call(HubSpotOwnersClient, recorder, "list_owners")

    assert [owner.display_name for owner in result] == ["Riley Smith", "owner@example.test"]
    assert "after=2" in str(recorder.requests[1].url)


@pytest.mark.asyncio
async def test_owner_listing_surfaces_permission_and_invalid_response_errors():
    forbidden = Recorder([(403, {"message": "forbidden"})])
    with pytest.raises(IntegrationPermissionError):
        await call(HubSpotOwnersClient, forbidden, "list_owners")

    invalid = Recorder([(200, {"results": [{"firstName": "No ID"}]})])
    with pytest.raises(IntegrationError, match="owner response was invalid"):
        await call(HubSpotOwnersClient, invalid, "list_owners")


def stored_token(scopes: list[str]) -> StoredOAuthToken:
    now = datetime.now(UTC)
    return StoredOAuthToken(
        tenant_id="tenant-a",
        hubspot_account_id="42",
        encrypted_access_token="encrypted",
        encrypted_refresh_token="encrypted-refresh",
        expires_at=now + timedelta(hours=1),
        scopes=scopes,
        created_at=now,
        updated_at=now,
    )


class TokenProvider:
    def __init__(self, token: StoredOAuthToken) -> None:
        self.token = token
        self.tenant_ids: list[str] = []

    async def get_access_token(self, tenant_id: str):
        self.tenant_ids.append(tenant_id)
        return "access-token", self.token


@pytest.mark.asyncio
async def test_property_and_owner_services_keep_existing_scopes_and_tenant_context():
    class PropertyClient:
        async def list_properties(self, context, access_token, *, object_type):
            assert context.tenant_id == "tenant-a"
            assert context.hubspot_account_id == "42"
            assert object_type == "contacts"
            return []

    class OwnerClient:
        async def list_owners(self, context, access_token):
            assert context.tenant_id == "tenant-a"
            assert context.hubspot_account_id == "42"
            return []

    property_provider = TokenProvider(stored_token(["crm.schemas.contacts.read"]))
    owner_provider = TokenProvider(stored_token(["crm.objects.owners.read"]))
    await HubSpotPropertiesService(PropertyClient(), property_provider).list_properties(
        CONTEXT, "contacts"
    )
    await HubSpotOwnersService(OwnerClient(), owner_provider).list_owners(CONTEXT)
    assert property_provider.tenant_ids == ["tenant-a"]
    assert owner_provider.tenant_ids == ["tenant-a"]


@pytest.mark.asyncio
async def test_property_and_owner_services_do_not_expand_missing_scopes():
    provider = TokenProvider(stored_token(["crm.objects.contacts.read"]))
    with pytest.raises(IntegrationPermissionError) as property_error:
        await HubSpotPropertiesService(object(), provider).list_properties(CONTEXT, "contacts")
    assert property_error.value.scope == "crm.schemas.contacts.read"

    provider = TokenProvider(stored_token(["crm.objects.contacts.read"]))
    with pytest.raises(IntegrationPermissionError) as owner_error:
        await HubSpotOwnersService(object(), provider).list_owners(CONTEXT)
    assert owner_error.value.scope == "crm.objects.owners.read"
