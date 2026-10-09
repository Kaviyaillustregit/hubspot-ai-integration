import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import Settings
from app.integrations.errors import (
    IntegrationAuthenticationError,
    IntegrationNotFoundError,
    IntegrationPermissionError,
)
from app.integrations.hubspot.associations import HubSpotAssociationsClient
from app.integrations.hubspot.companies import HubSpotCompaniesClient
from app.integrations.hubspot.contacts import HubSpotContactsClient
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.deals import HubSpotDealsClient
from app.integrations.hubspot.models import HubSpotProperty
from app.integrations.hubspot.oauth import StoredOAuthToken
from app.services.hubspot_associations import HubSpotAssociationsService
from app.services.hubspot_deals import HubSpotDealsService

CONTEXT = TenantContext("tenant-a", "42", "hubspot-oauth-token")


class Recorder:
    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        content = request.read()
        self.calls.append(
            (request.method, str(request.url), json.loads(content) if content else None)
        )
        assert request.headers["Authorization"] == "Bearer access-token"
        return httpx.Response(self.status, json=self.body)


async def call(client_class, recorder: Recorder, method: str, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as http:
        client = client_class(Settings(), http)
        return await getattr(client, method)(CONTEXT, "access-token", **kwargs)


async def test_create_company_posts_properties():
    recorder = Recorder(201, {"id": "9", "properties": {"name": "TechNova"}})

    company = await call(
        HubSpotCompaniesClient, recorder, "create_company", properties={"name": "TechNova"}
    )

    assert recorder.calls == [
        ("POST", "https://api.hubapi.com/crm/v3/objects/companies", {
            "properties": {"name": "TechNova"}
        })
    ]
    assert company.id == "9"


async def test_update_company_patches_record():
    recorder = Recorder(200, {"id": "9", "properties": {"website": "technova.io"}})

    await call(
        HubSpotCompaniesClient,
        recorder,
        "update_company",
        company_id="9",
        properties={"website": "technova.io"},
    )

    assert recorder.calls[0][:2] == ("PATCH", "https://api.hubapi.com/crm/v3/objects/companies/9")


async def test_search_contacts_uses_full_text_query():
    recorder = Recorder(200, {"results": [{"id": "1", "properties": {"firstname": "Victor"}}]})

    page = await call(
        HubSpotContactsClient, recorder, "search_contacts", query="Victor Hall",
        properties=("firstname",),
    )

    assert recorder.calls[0] == (
        "POST",
        "https://api.hubapi.com/crm/v3/objects/contacts/search",
        {"query": "Victor Hall", "limit": 100, "properties": ["firstname"]},
    )
    assert page.results[0].id == "1"


async def test_search_and_get_contact_support_cursor_and_property_selection():
    search = Recorder(
        200,
        {
            "results": [{"id": "1", "properties": {"email": "kaviya@example.com"}}],
            "paging": {"next": {"after": "100"}},
        },
    )
    page = await call(
        HubSpotContactsClient,
        search,
        "search_contacts",
        query="Kaviya",
        after="50",
        properties=("email", "city"),
    )
    assert search.calls[0][2] == {
        "query": "Kaviya",
        "limit": 100,
        "after": "50",
        "properties": ["email", "city"],
    }
    assert page.next_after == "100"

    get = Recorder(200, {"id": "1", "properties": {"email": "kaviya@example.com"}})
    contact = await call(
        HubSpotContactsClient,
        get,
        "get_contact",
        contact_id="1",
        properties=("email", "city"),
    )
    assert contact.id == "1"
    assert get.calls[0][1].endswith("/crm/v3/objects/contacts/1?properties=email%2Ccity")


async def test_deal_create_search_update_and_pipelines():
    recorder = Recorder(200, {"id": "5", "properties": {"dealname": "Renewal"}})
    await call(HubSpotDealsClient, recorder, "create_deal", properties={"dealname": "Renewal"})
    await call(
        HubSpotDealsClient, recorder, "update_deal", deal_id="5", properties={"amount": "10"}
    )
    assert recorder.calls == [
        ("POST", "https://api.hubapi.com/crm/v3/objects/deals", {
            "properties": {"dealname": "Renewal"}
        }),
        ("PATCH", "https://api.hubapi.com/crm/v3/objects/deals/5", {
            "properties": {"amount": "10"}
        }),
    ]

    search = Recorder(200, {"results": [{"id": "5", "properties": {"dealname": "Renewal"}}]})
    page = await call(HubSpotDealsClient, search, "search_deals", query="Renewal")
    assert search.calls[0][1] == "https://api.hubapi.com/crm/v3/objects/deals/search"
    assert page.results[0].properties["dealname"] == "Renewal"

    pipelines = Recorder(
        200,
        {
            "results": [
                {
                    "id": "default",
                    "label": "Sales Pipeline",
                    "displayOrder": 0,
                    "stages": [
                        {
                            "id": "closedwon",
                            "label": "Closed Won",
                            "displayOrder": 5,
                            "metadata": {"isClosed": "true", "probability": "1.0"},
                        }
                    ],
                }
            ]
        },
    )
    result = await call(HubSpotDealsClient, pipelines, "list_pipelines")
    assert pipelines.calls[0][:2] == ("GET", "https://api.hubapi.com/crm/v3/pipelines/deals")
    assert result[0].stages[0].label == "Closed Won"
    assert result[0].stages[0].metadata == {"isClosed": "true", "probability": "1.0"}


async def test_deal_type_property_options_are_read_from_hubspot():
    recorder = Recorder(
        200,
        {
            "name": "dealtype",
            "options": [
                {"label": "New Business", "value": "newbusiness", "displayOrder": 0},
                {"label": "Existing Business", "value": "existingbusiness", "displayOrder": 1},
            ],
        },
    )

    prop = await call(HubSpotDealsClient, recorder, "get_deal_type_property")

    assert recorder.calls[0][:2] == (
        "GET",
        "https://api.hubapi.com/crm/v3/properties/deals/dealtype",
    )
    assert [(option.label, option.value) for option in prop.options] == [
        ("New Business", "newbusiness"),
        ("Existing Business", "existingbusiness"),
    ]


async def test_deal_listing_preserves_pagination_cursor_and_pipeline_metadata():
    recorder = Recorder(
        200,
        {
            "results": [{"id": "5", "properties": {"dealname": "Renewal"}}],
            "paging": {"next": {"after": "100"}},
        },
    )

    page = await call(
        HubSpotDealsClient,
        recorder,
        "list_deals",
        limit=100,
        after="50",
        properties=("dealname", "hs_probability"),
    )

    assert recorder.calls[0][1].endswith(
        "/crm/v3/objects/deals?limit=100&after=50&properties=dealname%2Chs_probability"
    )
    assert page.results[0].properties["dealname"] == "Renewal"
    assert page.next_after == "100"


async def test_associate_uses_v4_default_association():
    recorder = Recorder(200, {"status": "COMPLETE"})

    await call(
        HubSpotAssociationsClient,
        recorder,
        "associate",
        from_type="deals",
        from_id="5",
        to_type="companies",
        to_id="9",
    )

    assert recorder.calls == [
        ("PUT", "https://api.hubapi.com/crm/v4/objects/deals/5/associations/default/companies/9",
         None)
    ]


async def test_associated_record_ids_follow_hubspot_paging_cursor():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.params.get("after") == "100":
            return httpx.Response(
                200,
                json={"results": [{"toObjectId": "company-101"}]},
            )
        return httpx.Response(
            200,
            json={
                "results": [{"toObjectId": "company-1"}],
                "paging": {"next": {"after": "100"}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HubSpotAssociationsClient(Settings(), http)
        ids = await client.list_associated_ids(
            CONTEXT,
            "access-token",
            from_type="contacts",
            from_id="contact-1",
            to_type="companies",
        )

    assert ids == ["company-1", "company-101"]
    assert len(calls) == 2
    assert "after=100" in calls[1]


async def test_associated_ids_and_batch_read():
    listing = Recorder(
        200,
        {"results": [{"toObjectId": 11, "associationTypes": []}, {"toObjectId": 11}]},
    )
    ids = await call(
        HubSpotAssociationsClient,
        listing,
        "list_associated_ids",
        from_type="companies",
        from_id="9",
        to_type="contacts",
    )
    assert ids == ["11"]
    assert listing.calls[0][1] == (
        "https://api.hubapi.com/crm/v4/objects/companies/9/associations/contacts?limit=100"
    )

    batch = Recorder(200, {"results": [{"id": "11", "properties": {"firstname": "Victor"}}]})
    records = await call(
        HubSpotAssociationsClient,
        batch,
        "read_records",
        object_type="contacts",
        record_ids=["11"],
        properties=["firstname"],
    )
    assert batch.calls[0][2] == {"inputs": [{"id": "11"}], "properties": ["firstname"]}
    assert records[0].properties == {"firstname": "Victor"}


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, IntegrationAuthenticationError),
        (403, IntegrationPermissionError),
        (404, IntegrationNotFoundError),
    ],
)
async def test_http_errors_are_normalized(status, error):
    with pytest.raises(error):
        await call(HubSpotDealsClient, Recorder(status), "get_deal", deal_id="5")


def token(scopes: list[str]) -> StoredOAuthToken:
    now = datetime.now(UTC)
    return StoredOAuthToken(
        tenant_id="tenant-a",
        hubspot_account_id="42",
        encrypted_access_token="x",
        encrypted_refresh_token="y",
        expires_at=now + timedelta(hours=1),
        scopes=scopes,
        created_at=now,
        updated_at=now,
    )


class Provider:
    def __init__(self, scopes: list[str]) -> None:
        self.scopes = scopes

    async def get_access_token(self, tenant_id: str):
        assert tenant_id == "tenant-a"
        return "access-token", token(self.scopes)


class ForbiddenClient:
    def __getattr__(self, name):
        raise AssertionError("HubSpot must not be called without the required scope")


async def test_missing_deal_scope_fails_before_calling_hubspot():
    service = HubSpotDealsService(
        ForbiddenClient(),  # type: ignore[arg-type]
        Provider(["crm.objects.contacts.read", "crm.objects.companies.read"]),
    )

    with pytest.raises(IntegrationPermissionError) as raised:
        await service.create_deal(
            TenantContext("tenant-a", "", "hubspot-oauth-token"), properties={"dealname": "x"}
        )

    assert raised.value.scope == "crm.objects.deals.write"


async def test_deal_type_property_requires_existing_schema_read_scope():
    service = HubSpotDealsService(
        ForbiddenClient(),  # type: ignore[arg-type]
        Provider(["crm.objects.deals.read", "crm.objects.deals.write"]),
    )

    with pytest.raises(IntegrationPermissionError) as raised:
        await service.get_deal_type_options(
            TenantContext("tenant-a", "", "hubspot-oauth-token")
        )

    assert raised.value.scope == "crm.schemas.deals.read"


async def test_deal_type_property_uses_an_already_granted_schema_scope():
    class PropertyClient:
        async def get_deal_type_property(self, context, access_token):
            assert context.hubspot_account_id == "42"
            assert access_token == "access-token"
            return HubSpotProperty(
                name="dealtype",
                options=[
                    {"label": "Existing Business", "value": "existingbusiness"},
                ],
            )

    service = HubSpotDealsService(
        PropertyClient(),  # type: ignore[arg-type]
        Provider(["crm.schemas.deals.read"]),
    )

    options = await service.get_deal_type_options(
        TenantContext("tenant-a", "", "hubspot-oauth-token")
    )

    assert [(option.label, option.value) for option in options] == [
        ("Existing Business", "existingbusiness")
    ]


async def test_association_reads_require_both_object_read_scopes():
    service = HubSpotAssociationsService(
        ForbiddenClient(),  # type: ignore[arg-type]
        Provider(["crm.objects.companies.read"]),
    )

    with pytest.raises(IntegrationPermissionError) as raised:
        await service.associated_records(
            TenantContext("tenant-a", "", "hubspot-oauth-token"),
            from_type="companies",
            from_id="9",
            to_type="deals",
            properties=["dealname"],
        )

    assert raised.value.scope == "crm.objects.deals.read"
