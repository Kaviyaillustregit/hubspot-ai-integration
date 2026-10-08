"""Natural-language CRM agent: LLM interpretation + deterministic validation and policy.

The LLM is replaced by a scripted provider returning the extraction a model would give
for each message; everything after that (grounding, validation, company resolution,
direct-create safety, confirmation policy) is the real application code.
"""

import pytest

from app.agent.schemas import AgentRequest, CRMIntentExtraction, GroundedSummary
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.service import AIService
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotCompany,
    HubSpotContact,
    HubSpotContactCompanyAssociation,
    HubSpotContactCompanyAssociations,
    HubSpotContactsPage,
)
from app.services.action_safety import ActionSafetyService, DirectActionClaim
from app.services.hubspot_contacts import HubSpotDuplicateContactError


def extraction(intent: str = "create_contact", **fields) -> CRMIntentExtraction:
    return CRMIntentExtraction(intent=intent, confidence=fields.pop("confidence", 0.95), **fields)


class ScriptedProvider:
    def __init__(self, intent: CRMIntentExtraction | Exception) -> None:
        self.intent = intent
        self.intent_calls = 0
        self.summary_variables: dict[str, object] | None = None

    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            assert output_schema is CRMIntentExtraction
            assert variables == {"message": variables["message"]}
            self.intent_calls += 1
            if isinstance(self.intent, Exception):
                raise self.intent
            return self.intent
        assert prompt_name == "account-intelligence/v1"
        assert output_schema is GroundedSummary
        self.summary_variables = variables
        return GroundedSummary(
            crm_facts=[f"Company: {variables['crm']['company']['properties']['name']}"],
            observations=[],
        )


class FakeCompanies:
    def __init__(self, companies: list[HubSpotCompany] | None = None) -> None:
        self.companies = companies or []
        self.queries: list[str] = []
        self.associations: dict[str, list[str]] = {}

    async def search_companies(self, context, **kwargs):
        assert context.tenant_id == "tenant-a"
        self.queries.append(kwargs["query"])
        return HubSpotCompaniesPage(results=self.companies)

    async def list_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=self.companies)

    async def get_contact_company_associations(self, context, contact_id):
        return HubSpotContactCompanyAssociations(
            results=[
                HubSpotContactCompanyAssociation(
                    toObjectId=company_id,
                    associationTypes=[],
                )
                for company_id in self.associations.get(contact_id, [])
            ]
        )


class FakeContacts:
    def __init__(self, *, duplicate_of: str | None = None) -> None:
        self.duplicate_of = duplicate_of
        self.created: list[dict[str, object]] = []
        self.existing: list[HubSpotContact] = []

    async def get_hubspot_account_id(self, context):
        return "42"

    async def create_contact(self, context, *, properties, company_id=None):
        assert context.tenant_id == "tenant-a"
        if self.duplicate_of is not None:
            raise HubSpotDuplicateContactError(str(properties["email"]), self.duplicate_of)
        self.created.append({"properties": properties, "company_id": company_id})
        return HubSpotContact(id=f"contact-{len(self.created)}", properties=properties)

    async def list_contacts(self, context, **kwargs):
        return HubSpotContactsPage(results=self.existing)

    async def search_contacts(self, context, *, query, **kwargs):
        matches = [
            contact
            for contact in self.existing
            if query.casefold() in " ".join(
                value or "" for value in contact.properties.values()
            ).casefold()
        ]
        return HubSpotContactsPage(results=matches)

    async def find_contact_by_email(self, tenant_id, email):
        return next(
            (contact for contact in self.existing if contact.properties.get("email") == email),
            None,
        )

    async def update_contact(self, context, **kwargs):
        raise AssertionError("update must wait for confirmation")

    async def delete_contact(self, context, **kwargs):
        raise AssertionError("delete must wait for confirmation")


class FakeActionSafety:
    """In-memory stand-in with the same claim semantics as ActionSafetyService."""

    def __init__(self) -> None:
        self.claimed: dict[str, dict[str, object]] = {}
        self.completed: list[dict[str, object]] = []
        self.failed: list[dict[str, object]] = []
        self.pending: list[dict[str, object]] = []

    async def start_direct_action(self, *, idempotency_key, tenant_id, **kwargs):
        action_id = ActionSafetyService.direct_action_id(
            tenant_id=tenant_id,
            action_type=kwargs["action_type"],
            idempotency_key=idempotency_key,
        )
        if action_id in self.claimed:
            return DirectActionClaim(action_id, False, "succeeded")
        self.claimed[action_id] = {"tenant_id": tenant_id, **kwargs}
        return DirectActionClaim(action_id, True)

    async def complete_action(self, **kwargs) -> None:
        self.completed.append(kwargs)

    async def fail_action(self, **kwargs) -> None:
        self.failed.append(kwargs)

    async def create_pending_action(self, **kwargs) -> str:
        self.pending.append(kwargs)
        return "0123456789abcdef0123456789abcdef"


ABC = HubSpotCompany(id="company-abc", properties={"name": "ABC Company"})


def request(message: str, ts: str = "1712345678.000100") -> AgentRequest:
    return AgentRequest(
        tenant_id="tenant-a",
        actor_id="U1",
        message=message,
        request_id="req-1",
        channel_id="C1",
        message_ts=ts,
        event_id="Ev1",
    )


def build(
    intent: CRMIntentExtraction | Exception,
    *,
    companies: list[HubSpotCompany] | None = None,
    contacts: FakeContacts | None = None,
):
    provider = ScriptedProvider(intent)
    company_service = FakeCompanies(companies)
    contact_service = contacts or FakeContacts()
    safety = FakeActionSafety()
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(company_service, contact_service),  # type: ignore[arg-type]
        AIService(provider),
        safety,  # type: ignore[arg-type]
    )
    return agent, provider, company_service, contact_service, safety


@pytest.mark.parametrize(
    ("message", "intent", "expected_properties", "expected_company_id"),
    [
        (
            "Create a contact named Victor Hall",
            extraction(first_name="Victor", last_name="Hall"),
            {"firstname": "Victor", "lastname": "Hall"},
            None,
        ),
        (
            "Create Victor Hall",
            extraction(first_name="Victor", last_name="Hall"),
            {"firstname": "Victor", "lastname": "Hall"},
            None,
        ),
        (
            "Add Victor Hall to contacts",
            extraction(first_name="Victor", last_name="Hall"),
            {"firstname": "Victor", "lastname": "Hall"},
            None,
        ),
        (
            "Please create a new contact, first name Victor, last name Hall",
            extraction(first_name="Victor", last_name="Hall"),
            {"firstname": "Victor", "lastname": "Hall"},
            None,
        ),
        (
            "Create Victor Hall, email victor@abc.com",
            extraction(first_name="Victor", last_name="Hall", email="victor@abc.com"),
            {"firstname": "Victor", "lastname": "Hall", "email": "victor@abc.com"},
            None,
        ),
        (
            "Under ABC company add Victor Hall",
            extraction(first_name="Victor", last_name="Hall", company_name="ABC"),
            {"firstname": "Victor", "lastname": "Hall"},
            "company-abc",
        ),
        (
            "Create Victor Hall under ABC company with email victor@abc.com",
            extraction(
                first_name="Victor",
                last_name="Hall",
                email="victor@abc.com",
                company_name="ABC company",
            ),
            {"firstname": "Victor", "lastname": "Hall", "email": "victor@abc.com"},
            "company-abc",
        ),
        (
            "create contat Victor Hall",
            extraction(first_name="Victor", last_name="Hall"),
            {"firstname": "Victor", "lastname": "Hall"},
            None,
        ),
        (
            "First name is Victor and last name is Hall, create him as a contact.",
            extraction(first_name="Victor", last_name="Hall"),
            {"firstname": "Victor", "lastname": "Hall"},
            None,
        ),
    ],
)
async def test_natural_language_contact_creation_runs_without_confirmation(
    message, intent, expected_properties, expected_company_id
):
    agent, _, _, contacts, safety = build(intent, companies=[ABC])

    result = await agent.respond(request(message))

    assert result.status == "ok"
    assert contacts.created == [
        {"properties": expected_properties, "company_id": expected_company_id}
    ]
    assert safety.pending == []
    assert len(safety.completed) == 1
    assert safety.completed[0]["resource_id"] == "contact-1"
    assert safety.failed == []
    assert "Contact Victor Hall was created successfully" in result.text


async def test_full_example_creates_and_associates_contact():
    agent, _, companies, contacts, safety = build(
        extraction(
            first_name="Victor",
            last_name="Hall",
            email="victor@abc.com",
            company_name="ABC company",
        ),
        companies=[ABC],
    )

    result = await agent.respond(
        request("Create a contact named Victor Hall, email victor@abc.com, under ABC company.")
    )

    assert result.text.startswith(
        "Contact Victor Hall was created successfully and associated with ABC Company."
    )
    assert result.tools_used == ["resolve_company", "create_contact"]
    assert result.result == {
        "kind": "contact_created",
        "contact_id": "contact-1",
        "name": "Victor Hall",
        "hubspot_url": "https://app.hubspot.com/contacts/42/record/0-1/contact-1",
        "email": "victor@abc.com",
        "company_name": "ABC Company",
    }
    assert companies.queries == ["ABC"]
    assert contacts.created[0]["company_id"] == "company-abc"
    assert safety.completed[0]["result"] == {
        "contact_id": "contact-1",
        "company_id": "company-abc",
    }


async def test_values_are_taken_from_the_users_text_not_the_models_rewrite():
    agent, _, _, contacts, _ = build(extraction(first_name="Victor", last_name="Hall"))

    await agent.respond(request("create contat victor hall"))

    assert contacts.created[0]["properties"] == {"firstname": "victor", "lastname": "hall"}


async def test_silently_corrected_email_is_rejected_and_nothing_is_written():
    agent, _, _, contacts, safety = build(
        extraction(first_name="Victor", email="victor@abc.com")
    )

    result = await agent.respond(request("Create Victor, email victor@abcc.com"))

    assert result.status == "invalid_request"
    assert "victor@abc.com" in result.text
    assert contacts.created == []
    assert safety.claimed == {}


async def test_invented_company_is_rejected():
    agent, _, companies, contacts, _ = build(
        extraction(first_name="Victor", last_name="Hall", company_name="ABC")
    )

    result = await agent.respond(request("Create Victor Hall, email victor@abc.com"))

    assert result.status == "invalid_request"
    assert companies.queries == []
    assert contacts.created == []


async def test_malformed_email_is_rejected():
    agent, _, _, contacts, _ = build(extraction(first_name="Victor", email="victor@abc"))

    result = await agent.respond(request("Create Victor with email victor@abc"))

    assert result.status == "invalid_request"
    assert "valid email address" in result.text
    assert contacts.created == []


async def test_missing_required_fields_does_not_create():
    agent, _, _, contacts, safety = build(extraction(company_name="ABC"))

    result = await agent.respond(request("Create a contact under ABC"))

    assert result.status == "missing_fields"
    assert "name or email" in result.text
    assert contacts.created == []
    assert safety.claimed == {}


async def test_company_not_found_does_not_create_contact():
    agent, _, _, contacts, safety = build(
        extraction(first_name="Victor", last_name="Hall", company_name="ABC"),
        companies=[HubSpotCompany(id="company-x", properties={"name": "ABCD Labs"})],
    )

    result = await agent.respond(request("Add Victor Hall to ABC"))

    assert result.status == "company_not_found"
    assert result.text == "I couldn't find a company named ABC, so I didn't create the contact."
    assert contacts.created == []
    assert safety.failed[0]["error_code"] == "company_not_found"
    assert safety.completed == []


async def test_ambiguous_company_does_not_create_contact():
    agent, _, _, contacts, safety = build(
        extraction(first_name="Victor", last_name="Hall", company_name="ABC"),
        companies=[
            HubSpotCompany(id="company-1", properties={"name": "ABC"}),
            HubSpotCompany(id="company-2", properties={"name": "ABC Inc."}),
        ],
    )

    result = await agent.respond(request("Associate Victor Hall with ABC"))

    assert result.status == "company_ambiguous"
    assert result.text == (
        "I found multiple companies matching ABC, so I didn't create the contact."
    )
    assert contacts.created == []
    assert safety.failed[0]["error_code"] == "company_ambiguous"


async def test_duplicate_contact_is_reported_and_recorded():
    agent, _, _, _, safety = build(
        extraction(first_name="Victor", email="victor@abc.com"),
        contacts=FakeContacts(duplicate_of="contact-9"),
    )

    result = await agent.respond(request("Create Victor, victor@abc.com"))

    assert result.status == "duplicate"
    assert "already exists" in result.text
    assert "contact-9" in result.text
    assert safety.failed[0]["error_code"] == "duplicate"
    assert safety.completed == []


async def test_redelivered_slack_message_creates_contact_once():
    agent, _, _, contacts, safety = build(extraction(first_name="Victor", last_name="Hall"))

    first = await agent.respond(request("Create Victor Hall"))
    second = await agent.respond(request("Create Victor Hall"))

    assert first.status == "ok"
    assert second.status == "duplicate_request"
    assert len(contacts.created) == 1
    assert len(safety.completed) == 1


async def test_a_new_message_with_the_same_text_is_a_new_request():
    agent, _, _, contacts, _ = build(extraction(first_name="Victor", last_name="Hall"))

    await agent.respond(request("Create Victor Hall", ts="1.0001"))
    await agent.respond(request("Create Victor Hall", ts="2.0002"))

    assert len(contacts.created) == 2


async def test_hubspot_not_connected_is_reported_and_recorded():
    class DisconnectedCompanies(FakeCompanies):
        async def search_companies(self, context, **kwargs):
            raise ValueError("HubSpot OAuth connection was not found")

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(DisconnectedCompanies(), FakeContacts()),  # type: ignore[arg-type]
        AIService(
            ScriptedProvider(extraction(first_name="Victor", company_name="ABC"))
        ),
        (safety := FakeActionSafety()),  # type: ignore[arg-type]
    )

    result = await agent.respond(request("Add Victor to ABC"))

    assert result.status == "hubspot_not_authorized"
    assert safety.failed[0]["error_code"] == "hubspot_not_authorized"


async def test_update_still_requires_confirmation():
    agent, _, _, _, safety = build(
        extraction("update_contact", contact_id="123", job_title="Head of Sales")
    )

    result = await agent.respond(request("Change contact 123's job title to Head of Sales"))

    assert result.status == "pending_confirmation"
    assert "confirm 0123456789abcdef0123456789abcdef" in result.text
    assert safety.pending == [
        {
            "tenant_id": "tenant-a",
            "actor_id": "U1",
            "action_type": "update_contact",
            "resource_type": "contact",
            "payload": {"contact_id": "123", "jobtitle": "Head of Sales"},
        }
    ]
    assert safety.claimed == {}


async def test_delete_still_requires_confirmation():
    agent, _, _, _, safety = build(extraction("delete_contact", contact_id="123"))

    result = await agent.respond(request("please remove contact 123"))

    assert result.status == "pending_confirmation"
    assert result.result == {
        "kind": "pending_confirmation",
        "action_id": "0123456789abcdef0123456789abcdef",
        "action_type": "delete_contact",
        "contact_id": "123",
    }
    assert safety.pending[0]["action_type"] == "delete_contact"
    assert safety.pending[0]["payload"] == {"contact_id": "123"}
    assert safety.claimed == {}


async def test_delete_without_contact_id_asks_for_it():
    agent, _, _, _, safety = build(
        extraction("delete_contact", first_name="Victor", last_name="Hall")
    )

    result = await agent.respond(request("delete Victor Hall"))

    assert result.status == "missing_fields"
    assert safety.pending == []


async def test_low_confidence_write_asks_for_clarification():
    agent, _, _, contacts, safety = build(
        extraction(first_name="Victor", confidence=0.3)
    )

    result = await agent.respond(request("Victor?"))

    assert result.status == "needs_clarification"
    assert contacts.created == []
    assert safety.claimed == {}


@pytest.mark.parametrize(
    "message",
    [
        "Who are the contacts in ABC?",
        "What contacts are associated with ABC?",
    ],
)
async def test_crm_contact_question_uses_read_only_tools_and_grounded_summary(message):
    contacts = FakeContacts()
    contacts.existing = [
        HubSpotContact(id="contact-7", properties={"firstname": "Angel", "lastname": "John"})
    ]
    agent, provider, companies, _, safety = build(
        extraction("crm_question", company_name="ABC", question="Contacts at ABC"),
        companies=[ABC],
        contacts=contacts,
    )
    companies.associations["contact-7"] = ["company-abc"]

    result = await agent.respond(request(message))

    assert result.status == "ok"
    assert result.tools_used == [
        "resolve_company",
        "find_contacts",
        "contact_company_associations",
    ]
    assert "*CRM facts*" in result.text
    assert provider.summary_variables is not None
    assert provider.summary_variables["question"] == "Contacts at ABC"
    assert provider.summary_variables["crm"]["contacts"][0]["id"] == "contact-7"
    assert safety.claimed == {} and safety.pending == []


@pytest.mark.parametrize(
    "message",
    ["Tell me about ABC.", "Give me information about ABC company."],
)
async def test_crm_company_question(message):
    company_name = "ABC company" if "company" in message else "ABC"
    agent, provider, _, _, _ = build(
        extraction("crm_question", company_name=company_name), companies=[ABC]
    )

    result = await agent.respond(request(message))

    assert result.status == "ok"
    assert result.tools_used == ["resolve_company"]
    assert provider.summary_variables["crm"]["contacts"] == []  # type: ignore[index]


async def test_crm_question_for_unknown_company():
    agent, _, _, _, _ = build(extraction("crm_question", company_name="Zeta"))

    result = await agent.respond(request("Tell me about Zeta"))

    assert result.status == "not_found"
    assert "Zeta" in result.text


async def test_unsupported_request_lists_real_capabilities():
    agent, _, _, _, _ = build(extraction("unsupported", confidence=0.9))

    result = await agent.respond(request("What's the weather?"))

    assert result.status == "unsupported"
    assert "contacts" in result.text
    assert "deal" not in result.text.lower()


async def test_llm_failure_falls_back_to_rule_parser_for_company_summary():
    agent, provider, _, _, _ = build(
        IntegrationError("LLM request failed"),
        companies=[HubSpotCompany(id="company-1", properties={"name": "Test AI Company"})],
    )

    result = await agent.respond(request("Tell me about Test AI Company"))

    assert result.status == "ok"
    assert result.tools_used == ["find_company"]
    assert provider.summary_variables is not None


async def test_llm_failure_falls_back_to_rule_parser_for_contact_creation():
    agent, _, _, contacts, safety = build(IntegrationError("LLM returned an invalid response"))

    result = await agent.respond(
        request("Create a contact firstname Arun lastname Kumar email arun@test.com")
    )

    assert result.status == "ok"
    assert contacts.created == [
        {
            "properties": {"email": "arun@test.com", "firstname": "Arun", "lastname": "Kumar"},
            "company_id": None,
        }
    ]
    assert len(safety.completed) == 1


async def test_confirmation_messages_never_reach_the_llm():
    class Safety(FakeActionSafety):
        async def confirm_and_claim_action(self, **kwargs):
            return None

    provider = ScriptedProvider(extraction(first_name="Victor"))
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(FakeCompanies(), FakeContacts()),  # type: ignore[arg-type]
        AIService(provider),
        Safety(),  # type: ignore[arg-type]
    )

    result = await agent.respond(request("confirm 0123456789abcdef0123456789abcdef"))

    assert result.status == "invalid_confirmation"
    assert provider.intent_calls == 0


async def test_company_mentioned_only_inside_an_email_is_not_grounded():
    agent, _, companies, contacts, _ = build(
        extraction(first_name="Victor", email="victor@abc.com", company_name="abc")
    )

    result = await agent.respond(request("Create Victor <mailto:victor@abc.com|victor@abc.com>"))

    assert result.status == "invalid_request"
    assert companies.queries == []
    assert contacts.created == []
