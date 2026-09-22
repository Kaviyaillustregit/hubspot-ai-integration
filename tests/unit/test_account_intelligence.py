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
    async def generate_structured(
        self,
        *,
        prompt_name,
        variables,
        output_schema,
    ):
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

    async def get_contact_company_associations(
        self,
        context,
        contact_id,
    ):
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

    company = await tools.find_company(
        "tenant-a",
        "Test AI Company",
    )

    assert company is not None
    assert company.id == "company-1"


@pytest.mark.asyncio
async def test_contact_and_association_tools_are_read_only_and_tenant_scoped():
    tools = HubSpotToolRegistry(Companies(), Contacts())

    contacts = await tools.find_contacts(
        "tenant-a",
        "example.com",
    )
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

    result = await agent.respond(request("Tell me about Test AI Company"))

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

    tools = HubSpotToolRegistry(
        Companies(),
        CreateContacts(),
    )

    contact = await tools.create_contact(
        "tenant-a",
        {
            "email": "new@example.com",
            "firstname": "New",
            "lastname": "User",
        },
    )

    assert contact == expected_contact


@pytest.mark.asyncio
async def test_update_contact_tool_uses_tenant_context():
    expected_contact = HubSpotContact(
        id="contact-2",
        properties={
            "firstname": "Arun",
            "jobtitle": "Senior AI Engineer",
        },
    )

    class UpdateContacts(Contacts):
        async def update_contact(
            self,
            context,
            *,
            contact_id: str,
            properties: dict[str, str | None],
        ):
            assert context.tenant_id == "tenant-a"
            assert context.credential_reference == "hubspot-oauth-token"
            assert contact_id == "123"
            assert properties == {
                "firstname": "Arun",
                "jobtitle": "Senior AI Engineer",
            }
            return expected_contact

    tools = HubSpotToolRegistry(
        Companies(),
        UpdateContacts(),
    )

    contact = await tools.update_contact(
        "tenant-a",
        "123",
        {
            "firstname": "Arun",
            "jobtitle": "Senior AI Engineer",
        },
    )

    assert contact == expected_contact

@pytest.mark.asyncio
async def test_delete_contact_tool_uses_tenant_context():
    class DeleteContacts(Contacts):
        async def delete_contact(
            self,
            context,
            *,
            contact_id: str,
        ) -> None:
            assert context.tenant_id == "tenant-a"
            assert (
                context.credential_reference
                == "hubspot-oauth-token"
            )
            assert contact_id == "123"

    tools = HubSpotToolRegistry(
        Companies(),
        DeleteContacts(),
    )

    result = await tools.delete_contact(
        "tenant-a",
        "123",
    )

    assert result is None

def test_agent_detects_contact_create_intent():
    intent = AccountIntelligenceAgent._contact_create_intent(
        "Create a contact firstname Arun lastname Kumar email arun@test.com"
    )

    assert intent is not None
    assert intent.email == "arun@test.com"
    assert intent.firstname == "Arun"
    assert intent.lastname == "Kumar"


def test_agent_detects_contact_update_intent():
    intent = AccountIntelligenceAgent._contact_update_intent(
        "Update contact id 123 firstname Arun lastname Kumar jobtitle Senior AI Engineer"
    )

    assert intent is not None
    assert intent.contact_id == "123"
    assert intent.properties == {
        "firstname": "Arun",
        "lastname": "Kumar",
        "jobtitle": "Senior AI Engineer",
    }

def test_agent_detects_contact_delete_intent():
    intent = AccountIntelligenceAgent._contact_delete_intent(
        "Delete contact id 123"
    )

    assert intent is not None
    assert intent.contact_id == "123"

def test_agent_returns_no_contact_delete_intent_without_contact_id():
    intent = AccountIntelligenceAgent._contact_delete_intent(
        "Delete contact"
    )

    assert intent is None

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
        request("Create a contact firstname Arun lastname Kumar email arun@test.com")
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


@pytest.mark.asyncio
async def test_agent_creates_pending_action_for_contact_update():
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
            return "action-456"

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts()),
        AIService(Provider()),
        FakeActionSafety(),  # type: ignore[arg-type]
    )

    result = await agent.respond(
        request("Update contact id 123 firstname Arun lastname Kumar jobtitle Senior AI Engineer")
    )

    assert result.status == "pending_confirmation"
    assert result.request_id == "req-1"
    assert "action-456" in result.text
    assert "123" in result.text

    assert created == {
        "tenant_id": "tenant-a",
        "actor_id": "user-a",
        "action_type": "update_contact",
        "resource_type": "contact",
        "payload": {
            "contact_id": "123",
            "firstname": "Arun",
            "lastname": "Kumar",
            "jobtitle": "Senior AI Engineer",
        },
    }

@pytest.mark.asyncio
async def test_agent_creates_pending_action_for_contact_delete():
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
            return "action-789"

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts()),
        AIService(Provider()),
        FakeActionSafety(),  # type: ignore[arg-type]
    )

    result = await agent.respond(
        request("Delete contact id 123")
    )

    assert result.status == "pending_confirmation"
    assert result.request_id == "req-1"
    assert "action-789" in result.text
    assert "123" in result.text

    assert created == {
        "tenant_id": "tenant-a",
        "actor_id": "user-a",
        "action_type": "delete_contact",
        "resource_type": "contact",
        "payload": {
            "contact_id": "123",
        },
    }

def test_agent_extracts_confirmation_action_id():
    action_id = AccountIntelligenceAgent._confirmation_action_id(
        "confirm 0123456789abcdef0123456789abcdef"
    )

    assert action_id == "0123456789abcdef0123456789abcdef"


def test_agent_rejects_invalid_confirmation_action_id():
    assert AccountIntelligenceAgent._confirmation_action_id("confirm not-a-valid-action-id") is None


@pytest.mark.asyncio
async def test_agent_confirms_pending_contact_creation():
    class FakeActionSafety:
        def __init__(self):
            self.completed: dict[str, object] | None = None
            self.failed: dict[str, object] | None = None

        async def confirm_and_claim_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_fingerprint: str,
        ):
            assert action_id == "0123456789abcdef0123456789abcdef"
            assert tenant_id == "tenant-a"
            assert actor_id == "user-a"
            assert request_fingerprint == ("confirm 0123456789abcdef0123456789abcdef")

            return type(
                "ConfirmedAction",
                (),
                {
                    "id": action_id,
                    "action_type": "create_contact",
                    "payload": {
                        "email": "arun@test.com",
                        "firstname": "Arun",
                        "lastname": "Kumar",
                    },
                },
            )()

        async def complete_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            resource_id: str | None,
            result: dict[str, object],
        ) -> None:
            self.completed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "result": result,
            }

        async def fail_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            error_code: str,
        ) -> None:
            self.failed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "error_code": error_code,
            }

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
                "email": "arun@test.com",
                "firstname": "Arun",
                "lastname": "Kumar",
            }

            return HubSpotContact(
                id="contact-2",
                properties=properties,
            )

    action_safety = FakeActionSafety()

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(),
            CreateContacts(),
        ),
        AIService(Provider()),
        action_safety,  # type: ignore[arg-type]
    )

    result = await agent.respond(request("confirm 0123456789abcdef0123456789abcdef"))

    assert result.status == "ok"
    assert result.request_id == "req-1"
    assert "Contact created successfully in HubSpot." in result.text
    assert "contact-2" in result.text
    assert result.tools_used == ["create_contact"]

    assert action_safety.completed == {
        "action_id": "0123456789abcdef0123456789abcdef",
        "tenant_id": "tenant-a",
        "actor_id": "user-a",
        "request_id": "req-1",
        "resource_type": "contact",
        "resource_id": "contact-2",
        "result": {
            "contact_id": "contact-2",
        },
    }
    assert action_safety.failed is None


@pytest.mark.asyncio
async def test_agent_confirms_pending_contact_update():
    class FakeActionSafety:
        def __init__(self):
            self.completed: dict[str, object] | None = None
            self.failed: dict[str, object] | None = None

        async def confirm_and_claim_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_fingerprint: str,
        ):
            assert action_id == "0123456789abcdef0123456789abcdef"
            assert tenant_id == "tenant-a"
            assert actor_id == "user-a"

            return type(
                "ConfirmedAction",
                (),
                {
                    "id": action_id,
                    "action_type": "update_contact",
                    "payload": {
                        "contact_id": "123",
                        "firstname": "Arun",
                        "lastname": "Kumar",
                        "jobtitle": "Senior AI Engineer",
                    },
                },
            )()

        async def complete_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            resource_id: str | None,
            result: dict[str, object],
        ) -> None:
            self.completed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "result": result,
            }

        async def fail_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            error_code: str,
        ) -> None:
            self.failed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "error_code": error_code,
            }

    class UpdateContacts(Contacts):
        async def update_contact(
            self,
            context,
            contact_id: str,
            properties: dict[str, str | None],
        ):
            assert context.tenant_id == "tenant-a"
            assert context.credential_reference == "hubspot-oauth-token"
            assert contact_id == "123"
            assert properties == {
                "firstname": "Arun",
                "lastname": "Kumar",
                "jobtitle": "Senior AI Engineer",
            }

            return HubSpotContact(
                id="123",
                properties=properties,
            )

    action_safety = FakeActionSafety()

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(),
            UpdateContacts(),
        ),
        AIService(Provider()),
        action_safety,  # type: ignore[arg-type]
    )

    result = await agent.respond(request("confirm 0123456789abcdef0123456789abcdef"))

    assert result.status == "ok"
    assert "Contact updated successfully" in result.text
    assert "123" in result.text
    assert result.tools_used == ["update_contact"]

    assert action_safety.completed is not None
    assert action_safety.completed["resource_id"] == "123"

@pytest.mark.asyncio
async def test_agent_confirms_pending_contact_delete():
    class FakeActionSafety:
        def __init__(self):
            self.completed: dict[str, object] | None = None
            self.failed: dict[str, object] | None = None

        async def confirm_and_claim_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_fingerprint: str,
        ):
            return type(
                "ConfirmedAction",
                (),
                {
                    "id": action_id,
                    "action_type": "delete_contact",
                    "payload": {
                        "contact_id": "123",
                    },
                },
            )()

        async def complete_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            resource_id: str | None,
            result: dict[str, object],
        ) -> None:
            self.completed = {
                "action_id": action_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "result": result,
            }

        async def fail_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            error_code: str,
        ) -> None:
            self.failed = {
                "action_id": action_id,
                "error_code": error_code,
            }

    class DeleteContacts(Contacts):
        async def delete_contact(
            self,
            context,
            *,
            contact_id: str,
        ) -> None:
            assert context.tenant_id == "tenant-a"
            assert context.credential_reference == "hubspot-oauth-token"
            assert contact_id == "123"

    action_safety = FakeActionSafety()

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(),
            DeleteContacts(),
        ),
        AIService(Provider()),
        action_safety,  # type: ignore[arg-type]
    )

    result = await agent.respond(
        request("confirm 0123456789abcdef0123456789abcdef")
    )

    assert result.status == "ok"
    assert "Contact deleted successfully" in result.text
    assert "123" in result.text
    assert result.tools_used == ["delete_contact"]

    assert action_safety.completed is not None
    assert action_safety.completed["resource_id"] == "123"

@pytest.mark.asyncio
async def test_agent_marks_contact_delete_failed_on_hubspot_error():
    class FakeActionSafety:
        def __init__(self):
            self.completed = None
            self.failed = None

        async def confirm_and_claim_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_fingerprint: str,
        ):
            return type(
                "ConfirmedAction",
                (),
                {
                    "id": action_id,
                    "action_type": "delete_contact",
                    "payload": {
                        "contact_id": "123",
                    },
                },
            )()

        async def complete_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            resource_id: str | None,
            result: dict[str, object],
        ) -> None:
            self.completed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "result": result,
            }

        async def fail_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            error_code: str,
        ) -> None:
            self.failed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "error_code": error_code,
            }

    class FailingContacts(Contacts):
        async def delete_contact(
            self,
            context,
            *,
            contact_id: str,
        ) -> None:
            raise IntegrationError("HubSpot delete failed")

    action_safety = FakeActionSafety()

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(),
            FailingContacts(),
        ),
        AIService(Provider()),
        action_safety,  # type: ignore[arg-type]
    )

    result = await agent.respond(
        request("confirm 0123456789abcdef0123456789abcdef")
    )

    assert result.status == "unavailable"
    assert "couldn't delete" in result.text.lower()
    assert result.tools_used == ["delete_contact"]

    assert action_safety.completed is None
    assert action_safety.failed == {
        "action_id": "0123456789abcdef0123456789abcdef",
        "tenant_id": "tenant-a",
        "actor_id": "user-a",
        "request_id": "req-1",
        "resource_type": "contact",
        "error_code": "integration_error",
    }

@pytest.mark.asyncio
async def test_agent_marks_contact_update_failed_on_hubspot_error():
    class FakeActionSafety:
        def __init__(self):
            self.completed = None
            self.failed = None

        async def confirm_and_claim_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_fingerprint: str,
        ):
            return type(
                "ConfirmedAction",
                (),
                {
                    "id": action_id,
                    "action_type": "update_contact",
                    "payload": {
                        "contact_id": "123",
                        "firstname": "Arun",
                    },
                },
            )()

        async def complete_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            resource_id: str | None,
            result: dict[str, object],
        ) -> None:
            self.completed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "result": result,
            }

        async def fail_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            error_code: str,
        ) -> None:
            self.failed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "error_code": error_code,
            }

    class FailingContacts(Contacts):
        async def update_contact(
            self,
            context,
            contact_id: str,
            properties: dict[str, str | None],
        ):
            raise IntegrationError("HubSpot update failed")

    action_safety = FakeActionSafety()

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(),
            FailingContacts(),
        ),
        AIService(Provider()),
        action_safety,  # type: ignore[arg-type]
    )

    result = await agent.respond(request("confirm 0123456789abcdef0123456789abcdef"))

    assert result.status == "unavailable"
    assert "couldn't update" in result.text.lower()
    assert result.tools_used == ["update_contact"]

    assert action_safety.completed is None
    assert action_safety.failed == {
        "action_id": "0123456789abcdef0123456789abcdef",
        "tenant_id": "tenant-a",
        "actor_id": "user-a",
        "request_id": "req-1",
        "resource_type": "contact",
        "error_code": "integration_error",
    }


@pytest.mark.asyncio
async def test_agent_marks_contact_creation_failed_on_hubspot_error():
    class FakeActionSafety:
        def __init__(self):
            self.completed = None
            self.failed = None

        async def confirm_and_claim_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_fingerprint: str,
        ):
            return type(
                "ConfirmedAction",
                (),
                {
                    "id": action_id,
                    "action_type": "create_contact",
                    "payload": {
                        "email": "arun@test.com",
                        "firstname": "Arun",
                        "lastname": "Kumar",
                    },
                },
            )()

        async def complete_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            error_code: str,
        ) -> None:
            self.completed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "error_code": error_code,
            }

        async def fail_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            error_code: str,
        ) -> None:
            self.failed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "error_code": error_code,
            }

    class FailingContacts(Contacts):
        async def create_contact(
            self,
            context,
            *,
            properties: dict[str, str | None],
        ):
            raise IntegrationError("HubSpot create failed")

    action_safety = FakeActionSafety()

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(),
            FailingContacts(),
        ),
        AIService(Provider()),
        action_safety,  # type: ignore[arg-type]
    )

    result = await agent.respond(request("confirm 0123456789abcdef0123456789abcdef"))

    assert result.status == "unavailable"
    assert "couldn't create the HubSpot contact" in result.text
    assert result.tools_used == ["create_contact"]

    assert action_safety.completed is None
    assert action_safety.failed == {
        "action_id": "0123456789abcdef0123456789abcdef",
        "tenant_id": "tenant-a",
        "actor_id": "user-a",
        "request_id": "req-1",
        "resource_type": "contact",
        "error_code": "integration_error",
    }


@pytest.mark.asyncio
async def test_agent_marks_contact_creation_duplicate_as_failed():
    class FakeActionSafety:
        def __init__(self):
            self.completed = None
            self.failed = None

        async def confirm_and_claim_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_fingerprint: str,
        ):
            return type(
                "ConfirmedAction",
                (),
                {
                    "id": action_id,
                    "action_type": "create_contact",
                    "payload": {
                        "email": "arun@test.com",
                        "firstname": "Arun",
                        "lastname": "Kumar",
                    },
                },
            )()

        async def complete_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            resource_id: str | None,
            result: dict[str, object],
        ) -> None:
            self.completed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "result": result,
            }

        async def fail_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            error_code: str,
        ) -> None:
            self.failed = {
                "action_id": action_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "resource_type": resource_type,
                "error_code": error_code,
            }

    class DuplicateContacts(Contacts):
        async def create_contact(
            self,
            context,
            *,
            properties: dict[str, str | None],
        ):
            raise ValueError("HubSpot contact with email 'arun@test.com' already exists")

    action_safety = FakeActionSafety()

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(),
            DuplicateContacts(),
        ),
        AIService(Provider()),
        action_safety,  # type: ignore[arg-type]
    )

    result = await agent.respond(request("confirm 0123456789abcdef0123456789abcdef"))

    assert result.status == "duplicate"
    assert "already exists" in result.text
    assert result.tools_used == ["create_contact"]

    assert action_safety.completed is None
    assert action_safety.failed == {
        "action_id": "0123456789abcdef0123456789abcdef",
        "tenant_id": "tenant-a",
        "actor_id": "user-a",
        "request_id": "req-1",
        "resource_type": "contact",
        "error_code": "duplicate",
    }
