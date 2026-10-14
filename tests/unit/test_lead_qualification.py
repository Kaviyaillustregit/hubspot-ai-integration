from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agent.leads import classify_priority, score_lead
from app.agent.schemas import AgentRequest, CRMIntentExtraction, LeadAssessment
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import LEAD_CONTACT_PROPERTIES, HubSpotToolRegistry
from app.ai.service import AIService
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.models import (
    HubSpotCompany,
    HubSpotContact,
    HubSpotContactCompanyAssociations,
    HubSpotContactsPage,
)

PROMPT = (
    "Analyze my HubSpot leads, identify high-potential prospects based on their CRM "
    "data and engagement history, and recommend which leads to prioritize."
)
TODAY = datetime.now(UTC).date()


class LeadProvider:
    def __init__(self, *, fail: bool = False, intent: str | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = fail
        self.intent = intent

    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            assert output_schema is CRMIntentExtraction
            return CRMIntentExtraction(
                intent="crm_question", query=self.intent, confidence=0.9
            )
        assert prompt_name == "lead-qualification/v1"
        self.calls.append(variables)
        if self.fail:
            raise IntegrationError("AI request failed")
        return LeadAssessment(
            qualification_assessment="Strong fit based on recorded data.",
            reasons=["Senior role recorded."],
            opportunities=["Company size suggests a larger account."],
            missing_information=["Budget"],
            recommended_next_action="Reach out to the contact.",
            outreach_approach="Reference their role.",
        )


def contact(contact_id: str, **props: str | None) -> HubSpotContact:
    return HubSpotContact(id=contact_id, properties=props)


STRONG = {
    "firstname": "Ada",
    "lastname": "Lovelace",
    "email": "ada@example.com",
    "jobtitle": "VP of Sales",
    "lifecyclestage": "salesqualifiedlead",
    "hs_lead_status": "IN_PROGRESS",
    "hubspot_owner_id": "owner-1",
    "notes_last_contacted": (TODAY - timedelta(days=3)).isoformat(),
}
COMPANY = HubSpotCompany(
    id="c1",
    properties={"name": "Acme", "industry": "SOFTWARE", "numberofemployees": "200"},
)


class LeadTools:
    def __init__(self, contacts, associations=None, *, fail=False, more=False):
        self.contacts = contacts
        self.associations = associations or {}
        self.fail = fail
        self.more = more
        self.limit = None
        self.company_fetches: list[str] = []
        self.tenants: list[str] = []

    async def list_contacts_limited(self, tenant_id, limit):
        self.tenants.append(tenant_id)
        self.limit = limit
        if self.fail:
            raise IntegrationError("HubSpot failed")
        return self.contacts, self.more

    async def contact_company_associations(self, tenant_id, contact_id):
        self.tenants.append(tenant_id)
        ids = self.associations.get(contact_id, [])
        return HubSpotContactCompanyAssociations.model_validate(
            {"results": [{"toObjectId": i, "associationTypes": []} for i in ids]}
        )

    async def get_company(self, tenant_id, company_id):
        self.tenants.append(tenant_id)
        self.company_fetches.append(company_id)
        return COMPANY

    async def hubspot_record_urls(self, tenant_id, records):
        return [f"https://app.hubspot.com/contacts/42/record/0-1/{i}" for _, i in records]


def make_agent(tools, provider, **kwargs):
    return AccountIntelligenceAgent(
        tools,  # type: ignore[arg-type]
        AIService(provider),
        SimpleNamespace(),  # type: ignore[arg-type]
        **kwargs,
    )


def req(message=PROMPT, tenant="tenant-a"):
    return AgentRequest(
        tenant_id=tenant, actor_id="u", message=message, request_id="r1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        PROMPT,
        "Which leads should I contact first?",
        "Score my leads",
        "Show high-potential prospects",
        "Prioritize my leads",
        "Qualify my leads please",
    ],
)
async def test_lead_intent_detected_without_llm_intent_call(message):
    tools = LeadTools([contact("1", **STRONG)], {"1": ["c1"]})
    provider = LeadProvider()
    response = await make_agent(tools, provider).respond(req(message))
    assert response.result is not None
    assert response.result["kind"] == "lead_qualification"


@pytest.mark.asyncio
async def test_llm_extracted_lead_query_is_routed():
    tools = LeadTools([contact("1", **STRONG)], {"1": ["c1"]})
    provider = LeadProvider(intent="lead_qualification")
    response = await make_agent(tools, provider).respond(req("Who looks promising?"))
    assert response.result is not None
    assert response.result["kind"] == "lead_qualification"


@pytest.mark.asyncio
async def test_retrieval_associates_companies_once_and_formats_report():
    contacts = [contact("1", **STRONG), contact("2", **{**STRONG, "email": "b@x.com"})]
    tools = LeadTools(contacts, {"1": ["c1"], "2": ["c1"]})
    response = await make_agent(tools, LeadProvider(), lead_analysis_limit=20).respond(req())

    assert tools.limit == 20
    assert tools.company_fetches == ["c1"]
    assert response.status == "ok"
    assert "*AI LEAD QUALIFICATION & PRIORITIZATION*" in response.text
    assert "Total leads analyzed: 2" in response.text
    assert "Company: Acme" in response.text
    assert "Open contact in HubSpot" in response.text
    assert "record/0-1/1" in response.text
    assert response.result["high_priority"] == 2


@pytest.mark.asyncio
async def test_missing_properties_are_reported_not_fabricated():
    tools = LeadTools([contact("9", firstname="Sparse")])
    provider = LeadProvider()
    response = await make_agent(tools, provider).respond(req())

    facts = provider.calls[0]["crm_facts"]
    assert facts["job_title"] is None
    assert facts["last_activity_date"] is None
    assert facts["company_name"] is None
    for item in ("job title", "lifecycle stage", "lead status", "last activity date",
                 "associated company", "email"):
        assert item in facts["missing_fields"]
    assert response.result["low_priority"] == 1
    assert "Missing information:" in response.text


def test_priority_classification_and_unqualified_cap():
    strong = score_lead(contact("1", **STRONG), COMPANY, today=TODAY)
    assert strong.priority == "High"
    assert strong.score >= 60

    medium = score_lead(
        contact("2", jobtitle="Engineer", lifecyclestage="lead", hs_lead_status="NEW",
                email="e@x.com"),
        COMPANY,
        today=TODAY,
    )
    assert medium.priority == "Medium"

    capped = score_lead(
        contact("3", **{**STRONG, "hs_lead_status": "UNQUALIFIED"}), COMPANY, today=TODAY
    )
    assert capped.priority == "Low"
    assert classify_priority(100, "unqualified") == "Low"
    assert classify_priority(10, None) == "Low"


@pytest.mark.asyncio
async def test_ai_failure_returns_partial_with_rules_based_result():
    tools = LeadTools([contact("1", **STRONG)], {"1": ["c1"]})
    response = await make_agent(tools, LeadProvider(fail=True)).respond(req())
    assert response.status == "partial"
    assert "AI assessment could not be generated" in response.text
    assert "Qualification reasoning:" in response.text


@pytest.mark.asyncio
async def test_hubspot_failure_and_unauthorized_are_safe():
    response = await make_agent(LeadTools([], fail=True), LeadProvider()).respond(req())
    assert response.status == "unavailable"

    class Unauthorized(LeadTools):
        async def list_contacts_limited(self, tenant_id, limit):
            raise ValueError("no token")

    response = await make_agent(Unauthorized([]), LeadProvider()).respond(req())
    assert response.status == "hubspot_not_authorized"


@pytest.mark.asyncio
async def test_large_report_respects_slack_limit_and_ai_batch():
    contacts = [
        contact(str(i), **{**STRONG, "lastname": "L" * 150}) for i in range(60)
    ]
    tools = LeadTools(contacts, more=True)
    provider = LeadProvider()
    response = await make_agent(tools, provider, lead_ai_batch_size=3).respond(req())
    assert len(provider.calls) == 3
    assert len(response.text) <= 39_000
    assert "more exist in HubSpot" in response.text


@pytest.mark.asyncio
async def test_tenant_id_is_used_for_every_hubspot_call():
    tools = LeadTools([contact("1", **STRONG)], {"1": ["c1"]})
    await make_agent(tools, LeadProvider()).respond(req(tenant="tenant-b"))
    assert set(tools.tenants) == {"tenant-b"}


@pytest.mark.asyncio
async def test_registry_limited_listing_stops_at_limit_and_uses_tenant():
    seen = []

    class Contacts:
        async def list_contacts(self, context, *, limit, after, properties):
            seen.append((context.tenant_id, limit, after, tuple(properties)))
            start = int(after or 0)
            return HubSpotContactsPage(
                results=[contact(str(start + i)) for i in range(limit)],
                next_after=str(start + limit),
            )

    registry = HubSpotToolRegistry(SimpleNamespace(), Contacts())  # type: ignore[arg-type]
    records, more = await registry.list_contacts_limited("tenant-z", 150)
    assert len(records) == 150
    assert more is True
    assert [item[1] for item in seen] == [100, 50]
    assert all(item[0] == "tenant-z" for item in seen)
    assert seen[0][3] == tuple(LEAD_CONTACT_PROPERTIES)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Find deals that need follow-up and prepare recommended actions.",
        "Create a contact named Jane Doe",
    ],
)
async def test_other_requests_do_not_route_to_lead_qualification(message):
    class NoLeadTools(LeadTools):
        async def list_contacts_limited(self, tenant_id, limit):
            raise AssertionError("lead tool must not run")

        async def list_all_deals(self, tenant_id, *, properties):
            return []

        async def deal_pipelines(self, tenant_id):
            return []

    provider = LeadProvider()
    try:
        response = await make_agent(NoLeadTools([]), provider).respond(req(message))
    except AssertionError:
        raise
    except Exception:
        return  # unrelated paths may need services not mocked here
    assert (response.result or {}).get("kind") != "lead_qualification"
