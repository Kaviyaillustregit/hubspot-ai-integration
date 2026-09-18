import httpx
import pytest

from app.core.config import Settings
from app.integrations.hubspot.companies import HubSpotCompaniesClient
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import HubSpotCompaniesPage
from app.integrations.hubspot.oauth import StoredOAuthToken
from app.services.hubspot_companies import HubSpotCompaniesService


def context() -> TenantContext:
    return TenantContext("tenant-a", "", "hubspot-oauth-token")


@pytest.mark.asyncio
async def test_companies_client_returns_typed_page_and_paging():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "results": [{"id": "company-1", "properties": {"name": "Acme"}}],
                "paging": {"next": {"after": "company-2"}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await HubSpotCompaniesClient(Settings(), client).list_companies(
            context(),
            "access-token",
            limit=25,
            after="company-0",
            properties=("name", "domain"),
        )

    assert captured["authorization"] == "Bearer access-token"
    assert captured["url"] == (
        "https://api.hubapi.com/crm/v3/objects/companies?limit=25"
        "&after=company-0&properties=name%2Cdomain"
    )
    assert page.results[0].id == "company-1"
    assert page.next_after == "company-2"


@pytest.mark.asyncio
async def test_association_client_uses_current_read_endpoint_and_maps_camel_case():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "toObjectId": 347402103497,
                    "associationTypes": [
                        {
                            "category": "HUBSPOT_DEFINED",
                            "typeId": 279,
                            "label": None,
                            "fromObjectTypeId": None,
                            "toObjectTypeId": None,
                        },
                        {
                            "category": "HUBSPOT_DEFINED",
                            "typeId": 1,
                            "label": "Primary",
                            "fromObjectTypeId": None,
                            "toObjectTypeId": None,
                        }
                    ],
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HubSpotCompaniesClient(Settings(), client).get_contact_company_associations(
            context(), "access-token", "contact-1"
        )

    assert captured == {
        "method": "GET",
        "url": "https://api.hubapi.com/crm/objects/2026-09/contacts/contact-1/associations/companies",
    }
    assert result.results[0].company_id == "347402103497"
    assert result.results[0].association_types[0].type_id == 279
    assert result.results[0].association_types[0].label is None
    assert result.results[0].association_types[1].type_id == 1
    assert result.results[0].association_types[1].label == "Primary"


@pytest.mark.asyncio
async def test_companies_service_uses_tenant_token_and_account_context():
    class TokenProvider:
        async def get_access_token(self, tenant_id: str):
            assert tenant_id == "tenant-a"
            return "tenant-access-token", StoredOAuthToken(
                tenant_id="tenant-a",
                hubspot_account_id="hubspot-42",
                encrypted_access_token="encrypted-access",
                encrypted_refresh_token="encrypted-refresh",
                expires_at=None,  # type: ignore[arg-type]
                scopes=[],
                created_at=None,  # type: ignore[arg-type]
                updated_at=None,  # type: ignore[arg-type]
            )

    class Client:
        async def list_companies(self, context, access_token, **kwargs):
            assert context.tenant_id == "tenant-a"
            assert context.hubspot_account_id == "hubspot-42"
            assert access_token == "tenant-access-token"
            return HubSpotCompaniesPage(results=[], next_after=None)

        async def get_contact_company_associations(self, context, access_token, contact_id):
            raise AssertionError("association lookup is not used")

    result = await HubSpotCompaniesService(Client(), TokenProvider()).list_companies(context())

    assert result.results == []