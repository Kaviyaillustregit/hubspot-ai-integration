"""Companies, deals and associations through the natural-language agent.

The LLM is replaced by a scripted extraction (what a model returns for the message). The
agent, validation, tool registry, operations and safety bookkeeping are the real code;
HubSpot services are an in-memory CRM so created records can be found again.
"""

import pytest

from app.agent.schemas import AgentRequest, CRMIntentExtraction, GroundedSummary
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.service import AIService
from app.integrations.errors import IntegrationError, IntegrationPermissionError
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotCompany,
    HubSpotContact,
    HubSpotContactCompanyAssociations,
    HubSpotContactsPage,
    HubSpotDeal,
    HubSpotDealsPage,
    HubSpotPipeline,
    HubSpotPipelineStage,
    HubSpotPropertyOption,
    HubSpotRecord,
)
from app.integrations.slack.home import render_response_blocks
from app.services.action_safety import ActionSafetyService, ConfirmedAction, DirectActionClaim
from app.services.hubspot_contacts import HubSpotDuplicateContactError

PIPELINE = HubSpotPipeline(
    id="default",
    label="Sales Pipeline",
    stages=[
        HubSpotPipelineStage(id="appointmentscheduled", label="Appointment Scheduled"),
        HubSpotPipelineStage(id="contractsent", label="Contract Sent", display_order=4),
        HubSpotPipelineStage(id="closedwon", label="Closed Won", display_order=5),
        HubSpotPipelineStage(id="qualifiedtobuy", label="Qualified to Buy", display_order=6),
    ],
)


class FakeCRM:
    """Tenant-checked in-memory HubSpot shared by the fake services."""

    def __init__(self) -> None:
        self.companies: dict[str, dict[str, str | None]] = {}
        self.contacts: dict[str, dict[str, str | None]] = {}
        self.deals: dict[str, dict[str, str | None]] = {}
        self.links: list[tuple[str, str, str, str]] = []
        self.writes: list[str] = []
        self.fail_on: str | None = None
        self.denied_scope: str | None = None
        self.deal_type_options: list[HubSpotPropertyOption] | None = [
            HubSpotPropertyOption(label="New Business", value="newbusiness"),
            HubSpotPropertyOption(label="Existing Business", value="existingbusiness"),
        ]

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{len(self.companies) + len(self.contacts) + len(self.deals) + 1}"

    def check(self, context, operation: str) -> None:
        assert context.tenant_id == "tenant-a", "tenant context must be preserved"
        if self.denied_scope and operation.endswith(self.denied_scope):
            raise IntegrationPermissionError("denied", scope=f"crm.objects.{self.denied_scope}")
        if self.fail_on == operation:
            raise IntegrationError("HubSpot request failed")


class FakeCompanies:
    def __init__(self, crm: FakeCRM) -> None:
        self.crm = crm

    async def search_companies(self, context, *, query, limit, properties):
        self.crm.check(context, "search:companies")
        return HubSpotCompaniesPage(
            results=[
                HubSpotCompany(id=key, properties=value)
                for key, value in self.crm.companies.items()
                if query.casefold() in (value.get("name") or "").casefold()
            ]
        )

    async def list_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(
            results=[HubSpotCompany(id=k, properties=v) for k, v in self.crm.companies.items()]
        )

    async def get_company(self, context, *, company_id, properties):
        self.crm.check(context, "read:companies")
        return HubSpotCompany(id=company_id, properties=self.crm.companies[company_id])

    async def create_company(self, context, *, properties):
        self.crm.check(context, "write:companies")
        company_id = self.crm.new_id("company")
        self.crm.companies[company_id] = dict(properties)
        self.crm.writes.append(f"create company {properties['name']}")
        return HubSpotCompany(id=company_id, properties=dict(properties))

    async def update_company(self, context, *, company_id, properties):
        self.crm.check(context, "write:companies")
        self.crm.companies[company_id].update(properties)
        self.crm.writes.append(f"update company {company_id} {properties}")
        return HubSpotCompany(id=company_id, properties=self.crm.companies[company_id])

    async def get_contact_company_associations(self, context, contact_id):
        return HubSpotContactCompanyAssociations(results=[])


class FakeContacts:
    def __init__(self, crm: FakeCRM) -> None:
        self.crm = crm

    async def get_hubspot_account_id(self, context):
        return "42"

    async def find_contact_by_email(self, tenant_id, email):
        assert tenant_id == "tenant-a"
        return next(
            (
                HubSpotContact(id=key, properties=value)
                for key, value in self.crm.contacts.items()
                if value.get("email") == email
            ),
            None,
        )

    async def search_contacts(self, context, *, query, limit, properties):
        self.crm.check(context, "search:contacts")
        words = query.casefold().split()
        return HubSpotContactsPage(
            results=[
                HubSpotContact(id=key, properties=value)
                for key, value in self.crm.contacts.items()
                if all(
                    word in " ".join(v or "" for v in value.values()).casefold() for word in words
                )
            ]
        )

    async def create_contact(self, context, *, properties, company_id=None):
        self.crm.check(context, "write:contacts")
        email = properties.get("email")
        if email and await self.find_contact_by_email("tenant-a", email):
            raise HubSpotDuplicateContactError(email, "existing")
        contact_id = self.crm.new_id("contact")
        self.crm.contacts[contact_id] = dict(properties)
        self.crm.writes.append(f"create contact {properties}")
        if company_id is not None:
            self.crm.links.append(("contacts", contact_id, "companies", company_id))
        return HubSpotContact(id=contact_id, properties=dict(properties))

    async def list_contacts(self, context, **kwargs):
        return HubSpotContactsPage(
            results=[HubSpotContact(id=k, properties=v) for k, v in self.crm.contacts.items()]
        )


class FakeDeals:
    def __init__(self, crm: FakeCRM) -> None:
        self.crm = crm

    async def search_deals(self, context, *, query, limit, properties):
        self.crm.check(context, "search:deals")
        return HubSpotDealsPage(
            results=[
                HubSpotDeal(id=key, properties=value)
                for key, value in self.crm.deals.items()
                if query.casefold() in (value.get("dealname") or "").casefold()
            ]
        )

    async def get_deal(self, context, *, deal_id, properties):
        return HubSpotDeal(id=deal_id, properties=self.crm.deals[deal_id])

    async def create_deal(self, context, *, properties):
        self.crm.check(context, "write:deals")
        deal_id = self.crm.new_id("deal")
        self.crm.deals[deal_id] = dict(properties)
        self.crm.writes.append(f"create deal {properties}")
        return HubSpotDeal(id=deal_id, properties=dict(properties))

    async def update_deal(self, context, *, deal_id, properties):
        self.crm.check(context, "write:deals")
        self.crm.deals[deal_id].update(properties)
        self.crm.writes.append(f"update deal {deal_id} {properties}")
        return HubSpotDeal(id=deal_id, properties=self.crm.deals[deal_id])

    async def list_pipelines(self, context):
        self.crm.check(context, "read:deals")
        return [PIPELINE]

    async def get_deal_type_options(self, context):
        self.crm.check(context, "read:deal_type_options")
        if self.crm.deal_type_options is None:
            raise IntegrationError("Deal type options unavailable")
        return self.crm.deal_type_options

    async def list_deals(self, context, **kwargs):
        self.crm.check(context, "read:deals")
        return HubSpotDealsPage(
            results=[
                HubSpotDeal(id=key, properties=value)
                for key, value in self.crm.deals.items()
            ]
        )


class FakeAssociations:
    def __init__(self, crm: FakeCRM) -> None:
        self.crm = crm

    async def associate(self, context, *, from_type, from_id, to_type, to_id):
        self.crm.check(context, f"associate:{from_type}")
        self.crm.links.append((from_type, from_id, to_type, to_id))
        self.crm.writes.append(f"link {from_type}:{from_id} -> {to_type}:{to_id}")

    async def associated_records(self, context, *, from_type, from_id, to_type, properties):
        self.crm.check(context, f"read:{to_type}")
        store = {"contacts": self.crm.contacts, "companies": self.crm.companies}.get(
            to_type, self.crm.deals
        )
        ids = [
            link[3] if link[0] == from_type else link[1]
            for link in self.crm.links
            if (link[0], link[1], link[2]) == (from_type, from_id, to_type)
            or (link[2], link[3], link[0]) == (from_type, from_id, to_type)
        ]
        return [HubSpotRecord(id=i, properties=store[i]) for i in dict.fromkeys(ids)]


class FakeActionSafety:
    def __init__(self) -> None:
        self.claimed: dict[str, dict[str, object]] = {}
        self.completed: list[dict[str, object]] = []
        self.failed: list[dict[str, object]] = []
        self.partial: list[dict[str, object]] = []
        self.pending: dict[str, dict[str, object]] = {}

    async def start_direct_action(self, *, idempotency_key, tenant_id, **kwargs):
        action_id = ActionSafetyService.direct_action_id(
            tenant_id=tenant_id, action_type=kwargs["action_type"], idempotency_key=idempotency_key
        )
        if action_id in self.claimed:
            return DirectActionClaim(action_id, False, "succeeded")
        self.claimed[action_id] = {"tenant_id": tenant_id, **kwargs}
        return DirectActionClaim(action_id, True)

    async def complete_action(self, **kwargs):
        self.completed.append(kwargs)

    async def fail_action(self, **kwargs):
        self.failed.append(kwargs)

    async def record_partial_action(self, **kwargs):
        self.partial.append(kwargs)

    async def create_pending_action(self, **kwargs):
        action_id = f"{len(self.pending):032x}"
        self.pending[action_id] = kwargs
        return action_id

    async def confirm_and_claim_action(self, *, action_id, tenant_id, actor_id, **kwargs):
        stored = self.pending.pop(action_id, None)
        if stored is None or stored["tenant_id"] != tenant_id or stored["actor_id"] != actor_id:
            return None
        return ConfirmedAction(
            id=action_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action_type=str(stored["action_type"]),
            resource_type=str(stored["resource_type"]),
            payload=dict(stored["payload"]),  # type: ignore[call-overload]
        )


class ScriptedProvider:
    def __init__(self) -> None:
        self.intent: CRMIntentExtraction | Exception | None = None

    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            if isinstance(self.intent, Exception):
                raise self.intent
            assert self.intent is not None
            return self.intent
        return GroundedSummary(crm_facts=["fact"], observations=[])


class Harness:
    def __init__(self) -> None:
        self.crm = FakeCRM()
        self.provider = ScriptedProvider()
        self.safety = FakeActionSafety()
        self.agent = AccountIntelligenceAgent(
            HubSpotToolRegistry(
                FakeCompanies(self.crm),  # type: ignore[arg-type]
                FakeContacts(self.crm),  # type: ignore[arg-type]
                FakeDeals(self.crm),  # type: ignore[arg-type]
                FakeAssociations(self.crm),  # type: ignore[arg-type]
            ),
            AIService(self.provider),
            self.safety,  # type: ignore[arg-type]
        )
        self._ts = 0

    def company(self, name: str, **properties: str) -> str:
        company_id = self.crm.new_id("company")
        self.crm.companies[company_id] = {"name": name, **properties}
        return company_id

    def contact(self, first: str, last: str, email: str | None = None) -> str:
        contact_id = self.crm.new_id("contact")
        self.crm.contacts[contact_id] = {"firstname": first, "lastname": last, "email": email}
        return contact_id

    def deal(self, name: str, **properties: str) -> str:
        deal_id = self.crm.new_id("deal")
        self.crm.deals[deal_id] = {
            "dealname": name,
            "pipeline": "default",
            "dealstage": "appointmentscheduled",
            **properties,
        }
        return deal_id

    async def say(self, message: str, intent: str, *, ts: str | None = None, **fields):
        self._ts += 1
        self.provider.intent = CRMIntentExtraction(
            intent=intent, confidence=fields.pop("confidence", 0.95), **fields
        )
        return await self.agent.respond(
            AgentRequest(
                tenant_id="tenant-a",
                actor_id="U1",
                message=message,
                request_id=f"req-{self._ts}",
                channel_id="C1",
                message_ts=ts or f"{self._ts}.000",
            )
        )

    async def confirm(self, action_id: str):
        return await self.agent.respond(
            AgentRequest(
                tenant_id="tenant-a",
                actor_id="U1",
                message=f"confirm {action_id}",
                request_id="req-confirm",
            )
        )


@pytest.fixture
def h() -> Harness:
    return Harness()


# ------------------------------------------------------------------------- companies


@pytest.mark.parametrize(
    "message",
    [
        "Create a company called ABC Technologies.",
        "Add ABC Technologies as a company.",
        "creat a new compnay named ABC Technologies",
    ],
)
async def test_create_company_from_natural_language(h, message):
    result = await h.say(message, "create_company", company_name="ABC Technologies")

    assert result.status == "ok"
    assert "✅ Company created: ABC Technologies" in result.text
    assert result.result == {
        "kind": "crm_records",
        "title": "Company created",
        "message": "ABC Technologies was successfully added to HubSpot.",
    }
    assert h.crm.writes == ["create company ABC Technologies"]
    assert len(h.safety.completed) == 1
    assert h.safety.completed[0]["result"] == {"company_id": "company-1"}


@pytest.mark.parametrize("failed_extraction", [True, False])
@pytest.mark.parametrize(
    ("message", "misclassified_intent", "expected_write"),
    [
        ("create company named abcd", "crm_question", "create company abcd"),
        ("create contact name ayal", "unsupported", "create contact"),
    ],
)
async def test_explicit_create_recovers_failed_or_misclassified_intent(
    h, failed_extraction, message, misclassified_intent, expected_write
):
    h.provider.intent = (
        RuntimeError("intent extraction unavailable")
        if failed_extraction
        else CRMIntentExtraction(
            intent=misclassified_intent,
            confidence=0.95,
            query="company_details" if misclassified_intent == "crm_question" else None,
        )
    )
    result = await h.agent.respond(
        AgentRequest(
            tenant_id="tenant-a",
            actor_id="U1",
            message=message,
            request_id="create-recovery",
        )
    )

    assert result.status == "ok"
    if expected_write == "create company abcd":
        assert h.crm.writes == [expected_write]
        assert "Company created: abcd" in result.text
    else:
        assert h.crm.writes == ["create contact {'firstname': 'ayal'}"]
        assert "Contact ayal was created successfully" in result.text


@pytest.mark.parametrize(
    "name_phrase",
    [
        "named Daniel and last name Joseph",
        "name Daniel and last name Joseph",
        "first name Daniel and last name Joseph",
        "Daniel and last name Joseph",
    ],
)
async def test_create_contact_parses_last_name_phrase_and_preserves_other_fields(h, name_phrase):
    h.provider.intent = CRMIntentExtraction(
        intent="create_contact",
        confidence=0.95,
        first_name="Daniel",
        last_name="and last name Joseph",
        email="daniel.joseph@example.com",
        phone="9876543211",
    )

    result = await h.agent.respond(
        AgentRequest(
            tenant_id="tenant-a",
            actor_id="U1",
            message=(
                f"Create contact {name_phrase} with email daniel.joseph@example.com "
                "and phone 9876543211"
            ),
            request_id="create-contact-name",
        )
    )

    assert result.status == "ok"
    assert h.crm.writes == [
        "create contact {'firstname': 'Daniel', 'lastname': 'Joseph', "
        "'email': 'daniel.joseph@example.com', 'phone': '9876543211'}"
    ]
    assert result.result is not None
    assert result.result["hubspot_url"] == (
        "https://app.hubspot.com/contacts/42/record/0-1/contact-1"
    )


async def test_create_company_with_grounded_details(h):
    result = await h.say(
        "Create a company called TechNova, website technova.io, phone +1 415 555 0100",
        "create_company",
        company_name="TechNova",
        company_website="technova.io",
        company_phone="+1 415 555 0100",
    )

    assert result.status == "ok"
    assert h.crm.companies["company-1"] == {
        "name": "TechNova",
        "website": "technova.io",
        "phone": "+1 415 555 0100",
    }


async def test_existing_company_is_not_duplicated(h):
    h.company("ABC Technologies")

    result = await h.say(
        "Create a company called ABC Technologies", "create_company",
        company_name="ABC Technologies",
    )

    assert result.status == "already_exists"
    assert result.result == {"kind": "existing_record", "title": "Company already exists"}
    assert h.crm.writes == []
    assert h.safety.failed == []
    assert h.safety.completed[0]["result"] == {"existing_company_id": "company-1"}


@pytest.mark.parametrize(
    "message", ["Find ABC Technologies.", "Get the company information for ABC Technologies."]
)
async def test_find_company_returns_details_card(h, message):
    h.company("ABC Technologies", domain="abctech.com", phone="+1 555 0100")

    result = await h.say(
        message, "crm_question", query="company_details", company_name="ABC Technologies"
    )

    assert result.status == "ok"
    assert result.result == {"kind": "crm_records", "title": "Company Details"}
    assert "*ABC Technologies*" in result.text
    assert "Domain: abctech.com" in result.text
    assert "HubSpot ID: `company-1`" in result.text
    assert h.safety.claimed == {}


async def test_update_company_requires_confirmation_then_updates(h):
    company_id = h.company("ABC Technologies")

    proposed = await h.say(
        "Update the website of ABC Technologies to abctech.io",
        "update_company",
        company_name="ABC Technologies",
        company_website="abctech.io",
    )

    assert proposed.status == "pending_confirmation"
    assert h.crm.writes == []
    action_id = proposed.result["action_id"]  # type: ignore[index]
    assert proposed.result["record_label"] == "company ABC Technologies"  # type: ignore[index]

    confirmed = await h.confirm(action_id)

    assert confirmed.status == "ok"
    assert h.crm.companies[company_id]["website"] == "abctech.io"
    assert h.safety.completed[-1]["resource_type"] == "company"


async def test_update_company_without_a_new_value_asks_for_it(h):
    h.company("ABC Technologies")

    result = await h.say(
        "Update ABC Technologies' phone number", "update_company",
        company_name="ABC Technologies",
    )

    assert result.status == "missing_fields"
    assert h.safety.pending == {}


# -------------------------------------------------------- contact <-> company links


@pytest.mark.parametrize(
    "message",
    [
        "Add Victor Hall to ABC Technologies.",
        "Associate Victor Hall with ABC Technologies.",
        "Under ABC Technologies add Victor Hall",
    ],
)
async def test_existing_contact_is_associated_with_company(h, message):
    company_id = h.company("ABC Technologies")
    contact_id = h.contact("Victor", "Hall")

    result = await h.say(
        message,
        "associate_records",
        first_name="Victor",
        last_name="Hall",
        company_name="ABC Technologies",
        contact_action="reference",
        company_action="reference",
    )

    assert result.status == "ok"
    assert "✅ Contact Victor Hall associated with ABC Technologies" in result.text
    assert h.crm.links == [("contacts", contact_id, "companies", company_id)]


async def test_associating_an_unknown_contact_asks_instead_of_creating(h):
    h.company("ABC Technologies")

    result = await h.say(
        "Add Victor Hall to ABC Technologies",
        "associate_records",
        first_name="Victor",
        last_name="Hall",
        company_name="ABC Technologies",
    )

    assert result.status == "contact_not_found"
    assert "create" in result.text
    assert h.crm.writes == []
    assert h.safety.failed[0]["error_code"] == "contact_not_found"


async def test_ambiguous_contact_asks_for_email(h):
    h.company("ABC Technologies")
    h.contact("Victor", "Hall", "v1@abc.com")
    h.contact("Victor", "Hall", "v2@abc.com")

    result = await h.say(
        "Add Victor Hall to ABC Technologies",
        "associate_records",
        first_name="Victor",
        last_name="Hall",
        company_name="ABC Technologies",
    )

    assert result.status == "contact_ambiguous"
    assert "email" in result.text
    assert h.crm.links == []


async def test_ambiguous_company_asks_which_one(h):
    h.company("ABC Technologies")
    h.company("ABC Technologies Inc.")
    h.contact("Victor", "Hall")

    result = await h.say(
        "Add Victor Hall to ABC Technologies",
        "associate_records",
        first_name="Victor",
        last_name="Hall",
        company_name="ABC Technologies",
    )

    assert result.status == "company_ambiguous"
    assert "Which one should I use?" in result.text
    assert h.crm.links == []


async def test_new_contact_under_company_keeps_the_original_contact_flow(h):
    company_id = h.company("ABC Technologies")

    result = await h.say(
        "Create Victor Hall as a contact under ABC Technologies",
        "create_contact",
        first_name="Victor",
        last_name="Hall",
        company_name="ABC Technologies",
    )

    assert result.status == "ok"
    assert result.result["kind"] == "contact_created"  # type: ignore[index]
    assert h.crm.links == [("contacts", "contact-2", "companies", company_id)]


@pytest.mark.parametrize(
    "message",
    [
        "Who are the contacts at ABC Technologies?",
        "Show me the contacts associated with ABC Technologies.",
    ],
)
async def test_company_contacts_query_uses_associations(h, message):
    company_id = h.company("ABC Technologies")
    contact_id = h.contact("Victor", "Hall", "victor@abc.com")
    h.contact("Other", "Person")
    h.crm.links.append(("contacts", contact_id, "companies", company_id))

    result = await h.say(
        message, "crm_question", query="company_contacts", company_name="ABC Technologies"
    )

    assert result.result == {"kind": "crm_records", "title": "Contacts at ABC Technologies"}
    assert result.text == "• Victor Hall — victor@abc.com"


async def test_which_company_is_contact_associated_with(h):
    company_id = h.company("ABC Technologies")
    contact_id = h.contact("Victor", "Hall")
    h.crm.links.append(("contacts", contact_id, "companies", company_id))

    result = await h.say(
        "Which company is Victor Hall associated with?",
        "crm_question",
        query="contact_company",
        first_name="Victor",
        last_name="Hall",
    )

    assert result.result == {"kind": "crm_records", "title": "Companies for Victor Hall"}
    assert result.text == "• ABC Technologies"


# ----------------------------------------------------------------------------- deals


async def test_create_deal_for_company_with_amount(h):
    h.company("ABC Technologies")

    result = await h.say(
        "Create a $50,000 deal for ABC Technologies.",
        "create_deal",
        deal_amount="$50,000",
        company_name="ABC Technologies",
    )

    assert result.status == "missing_fields"
    assert "What should the new deal be called?" in result.text
    assert h.crm.writes == []
    assert h.crm.deals == {}
    assert h.crm.links == []


@pytest.mark.parametrize(
    "message",
    [
        "Create a deal named Enterprise Upgrade with a close date of 2026-10-10.",
        "Close date 2026-10-10. Create a deal named Enterprise Upgrade.",
        "Creat a deal calld Enterprise Upgrade, close date 2026-10-10.",
    ],
)
async def test_create_named_deal_with_close_date(h, message):
    result = await h.say(
        message,
        "create_deal",
        deal_name="Enterprise Upgrade",
        deal_close_date="2026-10-10",
    )

    assert result.status == "ok"
    assert h.crm.deals["deal-1"] == {
        "dealname": "Enterprise Upgrade",
        "pipeline": "default",
        "dealstage": "appointmentscheduled",
        "closedate": "2026-10-10T00:00:00Z",
    }
    assert "✅ Deal created: Enterprise Upgrade" in result.text
    assert "2026-10-10" in result.text


async def test_deal_type_phrase_without_record_name_asks_for_clarification(h):
    result = await h.say(
        "Create a deal type with close date Oct 10.",
        "create_deal",
        deal_close_date="Oct 10",
    )

    assert result.status == "needs_clarification"
    assert "Did you mean to create a deal record?" in result.text
    assert h.crm.writes == []
    assert h.safety.claimed == {}


async def test_missing_extracted_close_date_does_not_create_incomplete_deal(h):
    result = await h.say(
        "Create a deal called Renewal closing October 10.",
        "create_deal",
        deal_name="Renewal",
    )

    assert result.status == "needs_clarification"
    assert "couldn't identify the close date" in result.text
    assert h.crm.writes == []
    assert h.crm.deals == {}
    assert h.safety.claimed == {}


async def test_updating_deal_close_date_remains_confirmation_gated(h):
    deal_id = h.deal("Renewal")
    proposed = await h.say(
        "Update the Renewal close date to 2026-10-10.",
        "update_deal",
        deal_name="Renewal",
        deal_close_date="2026-10-10",
    )

    assert proposed.status == "pending_confirmation"
    assert h.crm.deals[deal_id].get("closedate") is None

    await h.confirm(str(proposed.result["action_id"]))  # type: ignore[index]

    assert h.crm.deals[deal_id]["closedate"] == "2026-10-10T00:00:00Z"


async def test_deal_type_and_owner_requests_do_not_guess_account_values(h):
    typed = await h.say(
        "Create a deal called Renewal with type new business.",
        "create_deal",
        deal_name="Renewal",
        deal_type="New Business",
    )
    assert typed.status == "ok"
    assert h.crm.deals["deal-1"]["dealtype"] == "newbusiness"

    owned = await h.say(
        "Create a deal called Renewal and assign it to me.",
        "create_deal",
        deal_name="Renewal",
        deal_owner="me",
    )
    assert owned.status == "needs_clarification"
    assert "resolve a HubSpot owner" in owned.text
    assert len(h.crm.writes) == 1

    currency = await h.say(
        "Create a deal called Renewal for 10000 in currency USD.",
        "create_deal",
        deal_name="Renewal",
        deal_amount="10000",
        deal_currency="USD",
    )
    assert currency.status == "needs_clarification"
    assert "supported deal currencies" in currency.text
    assert len(h.crm.writes) == 1


async def test_deal_type_label_maps_to_hubspot_account_value(h):
    company_id = h.company("Test ABC")
    contact_id = h.contact("John", "Cena")
    result = await h.say(
        "Create a deal called Renewal with type Existing Business for Test ABC and "
        "associate it with John Cena, amount $1000, stage Qualified to Buy, "
        "close date December 10,2026.",
        "create_deal",
        deal_name="Renewal",
        company_name="Test ABC",
        first_name="John",
        last_name="Cena",
        deal_amount="$1000",
        deal_stage="Qualified to Buy",
        deal_close_date="December 10,2026",
        deal_type="Existing Business",
        associations=["deal_contact", "deal_company"],
    )

    assert result.status == "ok"
    deal_id = next(iter(h.crm.deals))
    assert h.crm.deals[deal_id] == {
        "dealname": "Renewal",
        "pipeline": "default",
        "dealstage": "qualifiedtobuy",
        "amount": "1000",
        "closedate": "2026-12-10T00:00:00Z",
        "dealtype": "existingbusiness",
    }
    assert h.crm.links == [
        ("deals", deal_id, "companies", company_id),
        ("deals", deal_id, "contacts", contact_id),
    ]


async def test_invalid_deal_type_is_rejected_without_creation(h):
    result = await h.say(
        "Create a deal called Renewal with type Channel Partner.",
        "create_deal",
        deal_name="Renewal",
        deal_type="Channel Partner",
    )

    assert result.status == "invalid_request"
    assert "New Business" in result.text
    assert "Existing Business" in result.text
    assert h.crm.writes == []
    assert h.crm.deals == {}


async def test_unavailable_deal_type_options_prevent_creation(h):
    h.crm.deal_type_options = None
    result = await h.say(
        "Create a deal called Renewal with type Existing Business.",
        "create_deal",
        deal_name="Renewal",
        deal_type="Existing Business",
    )

    assert result.status == "unavailable"
    assert h.crm.writes == []
    assert h.crm.deals == {}


async def test_unrecognized_deal_name_from_llm_is_rejected_without_creation(h):
    result = await h.say(
        "Create a deal called Renewal.",
        "create_deal",
        deal_name="Invented Renewal",
    )

    assert result.status == "invalid_request"
    assert "deal name" in result.text
    assert h.crm.writes == []
    assert h.crm.deals == {}


async def test_create_deal_with_close_date_and_associations(h):
    company_id = h.company("TechNova")
    contact_id = h.contact("John", "Smith")

    result = await h.say(
        "Create a deal named Product Renewal for TechNova and associate it with John Smith, "
        "closing 2026-10-16.",
        "create_deal",
        deal_name="Product Renewal",
        company_name="TechNova",
        first_name="John",
        last_name="Smith",
        deal_close_date="2026-10-16",
        associations=["deal_company", "deal_contact"],
    )

    assert result.status == "ok"
    deal_id = next(iter(h.crm.deals))
    assert h.crm.deals[deal_id]["closedate"] == "2026-10-16T00:00:00Z"
    assert h.crm.links == [
        ("deals", deal_id, "companies", company_id),
        ("deals", deal_id, "contacts", contact_id),
    ]


async def test_create_deal_with_comma_adjacent_close_date_and_requested_fields(h):
    company_id = h.company("Test ABC")
    contact_id = h.contact("John", "Cena")

    result = await h.say(
        "Create a new deal named 'Deal From John Cena', associate it with contact "
        "'John Cena' and company 'Test ABC', set amount to $1000, stage to "
        "'Qualified to Buy', and close date to December 10,2026.",
        "create_deal",
        deal_name="Deal From John Cena",
        company_name="Test ABC",
        first_name="John",
        last_name="Cena",
        deal_amount="$1000",
        deal_stage="Qualified to Buy",
        deal_close_date="December 10,2026",
        associations=["deal_contact", "deal_company"],
    )

    assert result.status == "ok"
    deal_id = next(iter(h.crm.deals))
    assert h.crm.deals[deal_id] == {
        "dealname": "Deal From John Cena",
        "pipeline": "default",
        "dealstage": "qualifiedtobuy",
        "amount": "1000",
        "closedate": "2026-12-10T00:00:00Z",
    }
    assert h.crm.links == [
        ("deals", deal_id, "companies", company_id),
        ("deals", deal_id, "contacts", contact_id),
    ]
    assert len(h.crm.writes) == 3


async def test_hubspot_deal_creation_failure_does_not_report_success(h):
    h.crm.fail_on = "write:deals"
    result = await h.say(
        "Create a deal called Renewal with close date 2026-10-10.",
        "create_deal",
        deal_name="Renewal",
        deal_close_date="2026-10-10",
    )

    assert result.status == "unavailable"
    assert "✅ Deal created" not in result.text
    assert h.crm.deals == {}
    assert h.crm.writes == []


async def test_provider_failure_does_not_create_a_deal(h):
    h.provider.intent = RuntimeError("provider unavailable")

    result = await h.agent.respond(
        AgentRequest(
            tenant_id="tenant-a",
            actor_id="U1",
            message="Create a deal called Renewal with close date 2026-10-10.",
            request_id="provider-failure",
        )
    )

    assert result.status == "unsupported"
    assert h.crm.writes == []
    assert h.crm.deals == {}


async def test_create_named_deal_with_amount_and_stage(h):
    result = await h.say(
        "Create a deal called Enterprise Renewal worth 50000 in contract sent",
        "create_deal",
        deal_name="Enterprise Renewal",
        deal_amount="50000",
        deal_stage="contract sent",
    )

    assert result.status == "ok"
    assert h.crm.deals["deal-1"]["dealstage"] == "contractsent"
    assert h.crm.links == []


async def test_duplicate_deal_name_is_not_created_again(h):
    h.deal("ABC Enterprise Deal")

    result = await h.say(
        "Create a deal called ABC Enterprise Deal",
        "create_deal",
        deal_name="ABC Enterprise Deal",
    )

    assert result.status == "duplicate"
    assert h.crm.writes == []


async def test_invalid_amount_is_rejected_before_anything_happens(h):
    result = await h.say(
        "Create a deal called Big One worth lots",
        "create_deal",
        deal_name="Big One",
        deal_amount="lots",
    )

    assert result.status == "invalid_request"
    assert "deal amount" in result.text
    assert h.safety.claimed == {}


async def test_deal_without_name_or_company_asks_for_a_name(h):
    result = await h.say("Create a deal worth 5000", "create_deal", deal_amount="5000")

    assert result.status == "missing_fields"
    assert h.crm.writes == []


async def test_find_deal_shows_stage_label(h):
    h.deal("ABC Enterprise Deal", amount="75000", dealstage="contractsent")

    result = await h.say(
        "Find the ABC Enterprise Deal.",
        "crm_question",
        query="deal_details",
        deal_name="ABC Enterprise Deal",
    )

    assert result.result == {"kind": "crm_records", "title": "Deal Details"}
    assert "• Amount: 75,000" in result.text
    assert "• Stage: Contract Sent" in result.text


async def test_update_deal_amount_requires_confirmation(h):
    deal_id = h.deal("ABC Enterprise Deal", amount="50000")

    proposed = await h.say(
        "Update the ABC Enterprise Deal amount to 75000",
        "update_deal",
        deal_name="ABC Enterprise Deal",
        deal_amount="75000",
    )

    assert proposed.status == "pending_confirmation"
    assert "amount → 75,000" in proposed.text
    assert h.crm.deals[deal_id]["amount"] == "50000"

    confirmed = await h.confirm(proposed.result["action_id"])  # type: ignore[index]

    assert confirmed.status == "ok"
    assert h.crm.deals[deal_id]["amount"] == "75000"


async def test_move_deal_to_a_real_stage(h):
    deal_id = h.deal("ABC Enterprise Deal")

    proposed = await h.say(
        "Move the ABC Enterprise Deal to closed won",
        "update_deal",
        deal_name="ABC Enterprise Deal",
        deal_stage="closed won",
    )
    await h.confirm(proposed.result["action_id"])  # type: ignore[index]

    assert h.crm.deals[deal_id]["dealstage"] == "closedwon"


async def test_unknown_stage_lists_the_real_stages(h):
    h.deal("ABC Enterprise Deal")

    result = await h.say(
        "Move the ABC Enterprise Deal to Negotiation",
        "update_deal",
        deal_name="ABC Enterprise Deal",
        deal_stage="Negotiation",
    )

    assert result.status == "invalid_request"
    assert "“Negotiation” isn't a stage in the Sales Pipeline pipeline" in result.text
    assert "Contract Sent" in result.text
    assert h.safety.pending == {}


async def test_company_deals_query(h):
    company_id = h.company("ABC Technologies")
    deal_id = h.deal("ABC Enterprise Deal", amount="50000", dealstage="closedwon")
    h.crm.links.append(("deals", deal_id, "companies", company_id))

    result = await h.say(
        "Show me the deals for ABC Technologies.",
        "crm_question",
        query="company_deals",
        company_name="ABC Technologies",
    )

    assert result.text == "• ABC Enterprise Deal — 50,000 — Closed Won"


# ----------------------------------------------------------------- deal associations


async def test_associate_existing_deal_with_company(h):
    company_id = h.company("ABC Technologies")
    deal_id = h.deal("ABC Enterprise Deal")

    result = await h.say(
        "Associate the ABC Enterprise Deal with ABC Technologies.",
        "associate_records",
        deal_name="ABC Enterprise Deal",
        company_name="ABC Technologies",
    )

    assert result.status == "ok"
    assert h.crm.links == [("deals", deal_id, "companies", company_id)]


async def test_associate_existing_deal_with_contact(h):
    contact_id = h.contact("Victor", "Hall")
    deal_id = h.deal("ABC Enterprise Deal")

    result = await h.say(
        "Add Victor Hall to the ABC Enterprise Deal.",
        "associate_records",
        deal_name="ABC Enterprise Deal",
        first_name="Victor",
        last_name="Hall",
    )

    assert result.status == "ok"
    assert h.crm.links == [("deals", deal_id, "contacts", contact_id)]


async def test_unknown_deal_is_reported(h):
    h.company("ABC Technologies")

    result = await h.say(
        "Associate the Mystery Deal with ABC Technologies",
        "associate_records",
        deal_name="Mystery Deal",
        company_name="ABC Technologies",
    )

    assert result.status == "deal_not_found"
    assert h.crm.links == []


# ------------------------------------------------------------------ combined requests


async def test_create_company_and_contact_and_link_them(h):
    result = await h.say(
        "Create a company called TechNova and add John Smith to it.",
        "multi_step",
        company_name="TechNova",
        first_name="John",
        last_name="Smith",
        company_action="create",
        contact_action="create",
        associations=["contact_company"],
    )

    assert result.status == "ok"
    assert result.text.splitlines() == [
        "✅ Company created: TechNova",
        "✅ Contact created: John Smith",
        "✅ Contact John Smith associated with TechNova",
    ]
    assert len(h.safety.claimed) == 1
    assert h.safety.completed[0]["result"] == {
        "company_id": "company-1",
        "contact_id": "contact-2",
        "contact_company_association": "contact-2->company-1",
    }


async def test_create_deal_for_company_and_associate_with_contact(h):
    company_id = h.company("TechNova")
    contact_id = h.contact("John", "Smith")

    result = await h.say(
        "Create a deal called Product Renewal worth $50,000 for TechNova and associate it "
        "with John Smith.",
        "create_deal",
        deal_name="Product Renewal",
        deal_amount="$50,000",
        company_name="TechNova",
        first_name="John",
        last_name="Smith",
        associations=["deal_company", "deal_contact"],
    )

    assert result.status == "ok"
    deal_id = next(iter(h.crm.deals))
    assert h.crm.links == [
        ("deals", deal_id, "companies", company_id),
        ("deals", deal_id, "contacts", contact_id),
    ]

async def test_create_company_contact_and_deal_in_one_message(h):
    result = await h.say(
        "Create ABC Technologies, add John Smith as a contact, and create a $50,000 deal "
        "called Product Renewal for them.",
        "multi_step",
        company_name="ABC Technologies",
        first_name="John",
        last_name="Smith",
        deal_name="Product Renewal",
        deal_amount="$50,000",
        company_action="create",
        contact_action="create",
        deal_action="create",
        associations=["contact_company", "deal_company", "deal_contact"],
    )

    assert result.status == "ok"
    assert [line.split(":")[0] for line in result.text.splitlines()] == [
        "✅ Company created",
        "✅ Contact created",
        "✅ Deal created",
        "✅ Contact John Smith associated with ABC Technologies",
        "✅ Deal Product Renewal associated with ABC Technologies",
        "✅ Deal Product Renewal associated with Contact John Smith",
    ]
    assert len(h.crm.links) == 3


async def test_existing_company_is_reused_in_combined_request(h):
    company_id = h.company("TechNova")

    result = await h.say(
        "Create a company called TechNova and add John Smith to it.",
        "multi_step",
        company_name="TechNova",
        first_name="John",
        last_name="Smith",
        company_action="create",
        contact_action="create",
    )

    assert result.status == "ok"
    assert "ℹ️ Company TechNova already exists — used the existing record" in result.text
    assert len(h.crm.companies) == 1
    assert ("contacts", "contact-2", "companies", company_id) in h.crm.links


async def test_partial_failure_reports_what_was_saved(h):
    h.crm.fail_on = "associate:contacts"

    result = await h.say(
        "Create a company called TechNova and add John Smith to it.",
        "multi_step",
        company_name="TechNova",
        first_name="John",
        last_name="Smith",
        company_action="create",
        contact_action="create",
    )

    assert result.status == "partial"
    assert "✅ Company created: TechNova" in result.text
    assert "❌ Associating Contact John Smith with TechNova failed" in result.text
    assert "nothing was rolled back" in result.text
    assert h.safety.partial[0]["result"]["failed_step"].startswith("Associating")
    assert h.safety.completed == []


async def test_missing_deal_scope_is_reported_clearly(h):
    h.crm.denied_scope = "deals"

    result = await h.say(
        "Create a deal called Enterprise Renewal",
        "create_deal",
        deal_name="Enterprise Renewal",
    )

    assert result.status == "insufficient_scope"
    assert "crm.objects.deals" in result.text
    assert "reconnect HubSpot" in result.text
    assert h.crm.writes == []
    assert h.safety.failed[0]["error_code"] == "insufficient_scope"


async def test_redelivered_combined_request_runs_once(h):
    fields = {
        "company_name": "TechNova",
        "first_name": "John",
        "last_name": "Smith",
        "company_action": "create",
        "contact_action": "create",
    }
    first = await h.say("Create TechNova and add John Smith", "multi_step", ts="9.1", **fields)
    second = await h.say("Create TechNova and add John Smith", "multi_step", ts="9.1", **fields)

    assert first.status == "ok"
    assert second.status == "duplicate_request"
    assert len(h.crm.companies) == 1
    assert len(h.crm.contacts) == 1


async def test_update_mixed_with_create_is_refused_without_writing(h):
    h.company("ABC Technologies")

    result = await h.say(
        "Create John Smith and change ABC Technologies' website to abc.io",
        "multi_step",
        first_name="John",
        last_name="Smith",
        company_name="ABC Technologies",
        company_website="abc.io",
        contact_action="create",
        company_action="update",
    )

    assert result.status == "needs_clarification"
    assert h.crm.writes == []
    assert h.safety.pending == {}


# ------------------------------------------------------------- fallback and isolation


async def test_llm_failure_falls_back_to_existing_rule_parser(h):
    h.provider.intent = IntegrationError("LLM request failed")
    h.company("ABC Technologies")

    result = await h.agent.respond(
        AgentRequest(
            tenant_id="tenant-a",
            actor_id="U1",
            message="Tell me about ABC Technologies",
            request_id="req-x",
        )
    )

    assert result.status == "ok"
    assert result.tools_used == ["find_company"]
    assert h.crm.writes == []


@pytest.mark.parametrize(
    ("message", "title", "first_tool"),
    [
        ("Show all contacts.", "Contacts", "list_all_contacts"),
        ("show all company", "Companies", "list_all_companies"),
        ("show all companies", "Companies", "list_all_companies"),
        ("show all deals", "Total Deals", "deal_pipelines"),
        ("show all open deals", "Open Deals", "deal_pipelines"),
    ],
)
async def test_llm_failure_routes_list_request_to_existing_list_flow(h, message, title, first_tool):
    h.provider.intent = IntegrationError("LLM request failed")
    h.contact("Ada", "Lovelace", "ada@example.com")

    result = await h.agent.respond(
        AgentRequest(
            tenant_id="tenant-a",
            actor_id="U1",
            message=message,
            request_id="req-contacts",
        )
    )

    assert result.status == "ok"
    assert result.result is not None
    assert result.result["title"] == title
    assert result.tools_used[0] == first_tool
    if message.casefold().endswith("contacts."):
        assert result.text == "Found 1 contacts."
        assert result.result["table"]["rows"][0]["name"] == "Ada Lovelace"  # type: ignore[index]
    assert h.crm.writes == []


async def test_llm_failure_routes_open_deals_to_open_deals_filter(h):
    h.provider.intent = IntegrationError("LLM request failed")

    result = await h.agent.respond(
        AgentRequest(
            tenant_id="tenant-a",
            actor_id="U1",
            message="show all open deals",
            request_id="req-open-deals",
        )
    )

    assert result.status == "ok"
    assert result.result is not None
    assert result.result["title"] == "Open Deals"
    assert result.text == "No open deals were found."


async def test_confirmation_from_another_user_is_rejected(h):
    h.company("ABC Technologies")
    proposed = await h.say(
        "Update the website of ABC Technologies to abctech.io",
        "update_company",
        company_name="ABC Technologies",
        company_website="abctech.io",
    )

    result = await h.agent.respond(
        AgentRequest(
            tenant_id="tenant-a",
            actor_id="U-other",
            message=f"confirm {proposed.result['action_id']}",  # type: ignore[index]
            request_id="req-y",
        )
    )

    assert result.status == "invalid_confirmation"
    assert h.crm.writes == []


async def test_confirming_an_unknown_action_type_never_creates_a_contact(h):
    class StrangeSafety(FakeActionSafety):
        async def confirm_and_claim_action(self, *, action_id, tenant_id, actor_id, **kwargs):
            return ConfirmedAction(
                id=action_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action_type="archive_everything",
                resource_type="crm",
                payload={"email": "x@example.com"},
            )

    h.agent._action_safety = StrangeSafety()  # type: ignore[assignment]

    result = await h.confirm("0123456789abcdef0123456789abcdef")

    assert result.status == "unsupported"
    assert h.crm.writes == []


async def test_deal_found_when_the_model_drops_the_word_deal(h):
    contact_id = h.contact("Victor", "Hall")
    deal_id = h.deal("ABC Enterprise Deal")

    result = await h.say(
        "Associate the ABC Enterprise Deal with Victor Hall.",
        "associate_records",
        deal_name="ABC Enterprise",
        first_name="Victor",
        last_name="Hall",
    )

    assert result.status == "ok"
    assert h.crm.links == [("deals", deal_id, "contacts", contact_id)]


async def test_deal_names_differing_only_by_suffix_are_ambiguous(h):
    h.contact("Victor", "Hall")
    h.deal("ABC Enterprise Deal")
    h.deal("ABC Enterprise")

    result = await h.say(
        "Associate the ABC Enterprise deal with Victor Hall.",
        "associate_records",
        deal_name="ABC Enterprise",
        first_name="Victor",
        last_name="Hall",
    )

    assert result.status == "deal_ambiguous"
    assert h.crm.links == []


# Extractions recorded from the real Gemini model (crm-intent/v2, 7 Oct 2026).
@pytest.mark.parametrize(
    "message",
    [
        "Create a company called Test AI Company.",
        "Create Test AI Company as a company.",
        "Add Test AI Company as a company.",
        "Create a new company named Test AI Company.",
        "Can you add Test AI Company to our CRM?",
    ],
)
async def test_regression_create_company_called_test_ai_company(h, message):
    result = await h.say(
        message, "create_company", company_name="Test AI Company", company_action="create"
    )

    assert result.status == "ok"
    assert result.text == "✅ Company created: Test AI Company"
    assert h.crm.companies == {"company-1": {"name": "Test AI Company"}}
    assert h.safety.completed[0]["result"] == {"company_id": "company-1"}


async def test_regression_existing_test_ai_company_is_reported_not_duplicated(h):
    h.company("Test AI Company", domain="testaicompany.com")

    result = await h.say(
        "Create a company called Test AI Company.",
        "create_company",
        company_name="Test AI Company",
        company_action="create",
    )

    assert result.status == "already_exists"
    assert result.result == {"kind": "existing_record", "title": "Company already exists"}
    assert result.text == (
        "Test AI Company already exists in HubSpot, so I used the existing company record. "
        "No duplicate was created."
    )
    assert len(h.crm.companies) == 1
    assert h.crm.writes == []
    assert h.safety.failed == []
    assert len(h.safety.completed) == 1


async def test_regression_existing_test_ai_company_renders_as_information_in_app_home(h):
    h.company("Test AI Company")

    result = await h.say(
        "Create a company called Test AI Company.",
        "create_company",
        company_name="Test AI Company",
        company_action="create",
    )
    blocks = render_response_blocks(result)

    assert blocks[0]["text"]["text"] == "ⓘ  *Company already exists*"
    assert blocks[1]["text"]["text"] == (
        "Test AI Company already exists in HubSpot, so I used the existing company record. "
        "No duplicate was created."
    )
    assert "Unable to complete request" not in str(blocks)


async def test_all_requested_records_existing_is_summarized(h):
    h.company("TechNova")
    h.contact("John", "Smith", "john@technova.io")
    h.crm.links.append(("contacts", "contact-2", "companies", "company-1"))

    result = await h.say(
        "Create a company called TechNova and add John Smith, john@technova.io, to it.",
        "multi_step",
        company_name="TechNova",
        first_name="John",
        last_name="Smith",
        email="john@technova.io",
        company_action="create",
        contact_action="create",
    )

    # The two existing records are re-linked (an idempotent HubSpot call), so the plan
    # reports success with the reuse notes rather than an error.
    assert result.status == "ok"
    assert "ℹ️ Company TechNova already exists — used the existing record" in result.text
    assert "ℹ️ Contact John Smith already exists — used the existing record" in result.text
    assert len(h.crm.companies) == 1 and len(h.crm.contacts) == 1


async def test_actual_failures_are_still_errors(h):
    h.crm.denied_scope = "companies"

    result = await h.say(
        "Create a company called Brand New Co",
        "create_company",
        company_name="Brand New Co",
        company_action="create",
    )

    assert result.status == "insufficient_scope"
    assert render_response_blocks(result)[0]["text"]["text"] == (
        "⚠  *Unable to complete request*"
    )


async def test_company_created_renders_as_assistant_success_in_app_home(h):
    result = await h.say(
        "Create a company called Demo AI Company 001.",
        "create_company",
        company_name="Demo AI Company 001",
        company_action="create",
    )
    blocks = render_response_blocks(result)

    assert blocks[0]["text"]["text"] == "✓  *Company created*"
    assert blocks[1]["text"]["text"] == "Demo AI Company 001 was successfully added to HubSpot."
    # The channel reply text is unchanged.
    assert result.text == "✅ Company created: Demo AI Company 001"


async def test_multi_step_success_keeps_the_step_list(h):
    result = await h.say(
        "Create a company called TechNova and add John Smith to it.",
        "multi_step",
        company_name="TechNova",
        first_name="John",
        last_name="Smith",
        company_action="create",
        contact_action="create",
    )
    blocks = render_response_blocks(result)

    assert blocks[0]["text"]["text"] == "✓  *CRM Updated*"
    assert "✅ Contact John Smith associated with TechNova" in blocks[1]["text"]["text"]


async def test_created_records_produce_result_cards(h):
    result = await h.say(
        "Create a $50,000 deal called Enterprise AI Platform for TechNova",
        "multi_step",
        company_name="TechNova",
        deal_name="Enterprise AI Platform",
        deal_amount="$50,000",
        company_action="create",
        deal_action="create",
    )

    assert result.status == "ok"
    assert result.cards == [
        {
            "kind": "company",
            "title": "Company created",
            "name": "TechNova",
            "hubspot_id": "company-1",
        },
        {
            "kind": "deal",
            "title": "Deal created",
            "name": "Enterprise AI Platform",
            "hubspot_id": "deal-2",
            "detail": "Amount 50,000 · Appointment Scheduled",
        },
        {"kind": "link", "title": "Linked", "name": "Deal Enterprise AI Platform ↔ TechNova"},
    ]


async def test_partial_failure_keeps_cards_for_saved_records(h):
    h.crm.fail_on = "associate:contacts"

    result = await h.say(
        "Create a company called TechNova and add John Smith to it.",
        "multi_step",
        company_name="TechNova",
        first_name="John",
        last_name="Smith",
        company_action="create",
        contact_action="create",
    )

    assert result.status == "partial"
    assert [card["kind"] for card in result.cards] == ["company", "contact"]
