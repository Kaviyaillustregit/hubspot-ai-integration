import pytest

from app.agent.schemas import AgentRequest
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.service import AIService
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotCompany,
    HubSpotContact,
    HubSpotContactCompanyAssociations,
    HubSpotContactsPage,
)


class Provider:
    async def generate_structured(self, *, prompt_name, variables, output_schema):
        assert prompt_name == "account-intelligence/v1"
        assert variables["crm"]["company"]["properties"]["name"] == "Test AI Company"
        return output_schema(
            crm_facts=["Company: Test AI Company", "Industry: Software"],
            observations=["Suggestion: confirm the next account touchpoint."],
        )


class Companies:
    async def list_companies(self, context, **kwargs):
        assert context.tenant_id == "tenant-a"
        return HubSpotCompaniesPage(
            results=[
                HubSpotCompany(
                    id="company-1",
                    properties={"name": "Test AI Company", "industry": "Software"},
                )
            ]
        )

    async def get_contact_company_associations(self, context, contact_id):
        assert context.tenant_id == "tenant-a"
        return HubSpotContactCompanyAssociations(results=[])


class Contacts:
    async def list_contacts(self, context, **kwargs):
        assert context.tenant_id == "tenant-a"
        return HubSpotContactsPage(
            results=[HubSpotContact(id="contact-1", properties={"email": "a@example.com"})]
        )


def request(message: str) -> AgentRequest:
    return AgentRequest(
        tenant_id="tenant-a", actor_id="user-a", message=message, request_id="req-1"
    )


@pytest.mark.asyncio
async def test_company_lookup_tool_uses_tenant_context():
    tools = HubSpotToolRegistry(Companies(), Contacts())

    company = await tools.find_company("tenant-a", "Test AI Company")

    assert company is not None
    assert company.id == "company-1"


@pytest.mark.asyncio
async def test_contact_and_association_tools_are_read_only_and_tenant_scoped():
    tools = HubSpotToolRegistry(Companies(), Contacts())

    contacts = await tools.find_contacts("tenant-a", "example.com")
    associations = await tools.contact_company_associations("tenant-a", "contact-1")

    assert contacts[0].id == "contact-1"
    assert associations.results == []


@pytest.mark.asyncio
async def test_agent_selects_company_tool_and_returns_labeled_grounded_summary():
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts()), AIService(Provider())
    )

    result = await agent.respond(request("Give me the latest information about Test AI Company."))

    assert result.status == "ok"
    assert result.tools_used == ["find_company"]
    assert "*CRM facts*" in result.text
    assert "*AI observations/suggestions*" in result.text
    assert "Test AI Company" in result.text


@pytest.mark.asyncio
async def test_agent_returns_safe_company_not_found_response():
    class MissingCompanies(Companies):
        async def list_companies(self, context, **kwargs):
            return HubSpotCompaniesPage(results=[])

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(MissingCompanies(), Contacts()), AIService(Provider())
    )

    result = await agent.respond(request("Tell me about Missing Company"))

    assert result.status == "not_found"
    assert "couldn't find" in result.text


@pytest.mark.asyncio
async def test_agent_hides_hubspot_and_llm_failures():
    class FailingCompanies(Companies):
        async def list_companies(self, context, **kwargs):
            raise IntegrationError("Bearer secret-access-token")

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(FailingCompanies(), Contacts()), AIService(Provider())
    )

    result = await agent.respond(request("Tell me about Test AI Company"))

    assert result.status == "unavailable"
    assert "secret-access-token" not in result.text
