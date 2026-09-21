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
                    properties={
                        "name": "Test AI Company",
                        "industry": "Software",
                    },
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
            results=[
                HubSpotContact(
                    id="contact-1",
                    properties={"email": "a@example.com"},
                )
            ]
        )


class ActionSafety:
    pass


def request(message: str) -> AgentRequest:
    return AgentRequest(
        tenant_id="tenant-a",
        actor_id="user-a",
        message=message,
        request_id="req-1",
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
    associations = await tools.contact_company_associations(
        "tenant-a",
        "contact-1",
    )

    assert contacts[0].id == "contact-1"
    assert associations.results == []


@pytest.mark.asyncio
async def test_agent_selects_company_tool_and_returns_labeled_grounded_summary():
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts()),
        AIService(Provider()),
        ActionSafety(),  # type: ignore[arg-type]
    )

    result = await agent.respond(
        request("Give me the latest information about Test AI Company.")
    )

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
        HubSpotToolRegistry(MissingCompanies(), Contacts()),
        AIService(Provider()),
        ActionSafety(),  # type: ignore[arg-type]
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
        HubSpotToolRegistry(FailingCompanies(), Contacts()),
        AIService(Provider()),
        ActionSafety(),  # type: ignore[arg-type]
    )

    result = await agent.respond(
        request("Tell me about Test AI Company")
    )

    assert result.status == "unavailable"
    assert "secret-access-token" not in result.text


@pytest.mark.asyncio
async def test_create_contact_tool_uses_tenant_context():
    expected_contact = HubSpotContact(
        id="contact-2",
        properties={
            "email": "new@example.com",
            "firstname": "New",
            "lastname": "User",
        },
    )

    class CreateContacts(Contacts):
        async def create_contact(
            self,
            context,
            *,
            properties: dict[str, str | None],
        ):
            assert context.tenant_id == "tenant-a"
            assert context.credential_reference == "hubspot-oauth-token"
            assert properties == {
                "email": "new@example.com",
                "firstname": "New",
                "lastname": "User",
            }
            return expected_contact

    tools = HubSpotToolRegistry(Companies(), CreateContacts())

    contact = await tools.create_contact(
        "tenant-a",
        {
            "email": "new@example.com",
            "firstname": "New",
            "lastname": "User",
        },
    )

    assert contact == expected_contact


def test_agent_detects_contact_create_intent():
    intent = AccountIntelligenceAgent._contact_create_intent(
        "Create a contact firstname Arun lastname Kumar email arun@test.com"
    )

    assert intent is not None
    assert intent.email == "arun@test.com"
    assert intent.firstname == "Arun"
    assert intent.lastname == "Kumar"


def test_agent_returns_no_contact_create_intent_without_email():
    intent = AccountIntelligenceAgent._contact_create_intent(
        "Create a contact firstname Arun lastname Kumar"
    )

    assert intent is None
@pytest.mark.asyncio
async def test_agent_creates_pending_action_for_contact_creation():
    created: dict[str, object] = {}

    class FakeActionSafety:
        async def create_pending_action(
            self,
            *,
            tenant_id: str,
            actor_id: str,
            action_type: str,
            resource_type: str,
            payload: dict[str, object],
        ) -> str:
            created["tenant_id"] = tenant_id
            created["actor_id"] = actor_id
            created["action_type"] = action_type
            created["resource_type"] = resource_type
            created["payload"] = payload
            return "action-123"

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts()),
        AIService(Provider()),
        FakeActionSafety(),  # type: ignore[arg-type]
    )

    result = await agent.respond(
        request(
            "Create a contact firstname Arun lastname Kumar "
            "email arun@test.com"
        )
    )

    assert result.status == "pending_confirmation"
    assert result.request_id == "req-1"
    assert "action-123" in result.text
    assert "arun@test.com" in result.text

    assert created == {
        "tenant_id": "tenant-a",
        "actor_id": "user-a",
        "action_type": "create_contact",
        "resource_type": "contact",
        "payload": {
            "email": "arun@test.com",
            "firstname": "Arun",
            "lastname": "Kumar",
        },
    }

def test_agent_extracts_confirmation_action_id():
    action_id = AccountIntelligenceAgent._confirmation_action_id(
        "confirm 0123456789abcdef0123456789abcdef"
    )

    assert action_id == "0123456789abcdef0123456789abcdef"

def test_agent_rejects_invalid_confirmation_action_id():
    assert (
        AccountIntelligenceAgent._confirmation_action_id(
            "confirm not-a-valid-action-id"
        )
        is None
    )