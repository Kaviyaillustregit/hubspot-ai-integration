from datetime import UTC, datetime
import pytest

from app.agent.crm_intelligence import plan_crm_query
from app.agent.schemas import AgentRequest, CRMIntentExtraction
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.provider import AIProvider
from app.ai.service import AIService
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotContact,
    HubSpotContactsPage,
    HubSpotDealsPage,
)
from app.integrations.hubspot.properties import HubSpotPropertyDefinition
from app.integrations.slack.home import render_response_blocks


class MockProvider(AIProvider):
    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(
                intent="crm_question",
                confidence=1.0,
                question=variables["message"],
            )
        if prompt_name == "crm-intelligence/v1":
            return plan_crm_query(variables["query"])
        raise AssertionError(prompt_name)


class MockCompanies:
    async def list_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=[])

    async def search_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=[])


class MockContacts:
    def __init__(self, contacts=None):
        self.contacts = contacts or []

    async def get_hubspot_account_id(self, context):
        return "portal-123"

    async def list_contacts(self, context, **kwargs):
        offset = int(kwargs.get("after") or 0)
        chunk = self.contacts[offset : offset + 100]
        more = offset + 100 < len(self.contacts)
        return HubSpotContactsPage(
            results=chunk, next_after=str(offset + 100) if more else None
        )

    async def search_contacts(self, context, **kwargs):
        return HubSpotContactsPage(results=self.contacts)


class MockPropertiesService:
    async def list_properties(self, context, object_type):
        if object_type == "contacts":
            return [
                HubSpotPropertyDefinition(name="firstname", label="First Name"),
                HubSpotPropertyDefinition(name="lastname", label="Last Name"),
                HubSpotPropertyDefinition(name="email", label="Email"),
                HubSpotPropertyDefinition(name="jobtitle", label="Job Title"),
                HubSpotPropertyDefinition(name="city", label="City"),
                HubSpotPropertyDefinition(name="country", label="Country"),
                HubSpotPropertyDefinition(name="createdate", label="Create Date", type="date"),
                HubSpotPropertyDefinition(name="lifecyclestage", label="Lifecycle Stage"),
                HubSpotPropertyDefinition(name="hs_lead_status", label="Lead Status"),
            ]
        return []


class MockDeals:
    async def list_deals(self, context, **kwargs):
        return HubSpotDealsPage(results=[])

    async def list_pipelines(self, context):
        return []


def make_test_agent(contacts=None):
    return AccountIntelligenceAgent(
        HubSpotToolRegistry(
            MockCompanies(),
            MockContacts(contacts),
            MockDeals(),
            properties=MockPropertiesService(),
        ),
        AIService(MockProvider()),
        object(),
    )


def ask(message: str) -> AgentRequest:
    return AgentRequest(
        tenant_id="tenant-test",
        actor_id="user-test",
        channel_id="C123",
        message=message,
        request_id="r-1",
    )


def test_plan_country_comparison_query():
    plan = plan_crm_query("How many contacts do we have in India vs the US?")
    assert plan.object == "contact"
    assert plan.group_by == "country"
    assert plan.chart_requested is True
    assert plan.chart_type == "bar"
    assert "country" in plan.properties
    assert any(f.field == "country" and f.operator == "in" for f in plan.filters)


@pytest.mark.asyncio
async def test_agent_country_comparison_generates_chart_and_accurate_footer():
    # 50 India contacts and 32 US contacts = 82 total contacts
    contacts = [
        HubSpotContact(
            id=f"in-{i}",
            properties={"firstname": f"IndiaUser{i}", "country": "India"},
        )
        for i in range(50)
    ] + [
        HubSpotContact(
            id=f"us-{i}",
            properties={"firstname": f"USUser{i}", "country": "US"},
        )
        for i in range(32)
    ]
    agent = make_test_agent(contacts=contacts)
    response = await agent.respond(ask("How many contacts do we have in India vs the US?"))

    assert response.status == "ok"
    result = response.result

    # 1. Chart title must be "Contacts by Country", NOT "Contacts by Lifecycle Stage"
    assert result.get("chart_title") == "Contacts by Country"
    assert result.get("chart_type") == "bar"
    assert result.get("chart_url") is not None

    # 2. Separate India and US contact counts
    chart_data = result["chart"]["data"]
    labels = chart_data["labels"]
    values = chart_data["datasets"][0]["data"]
    assert "India" in labels
    assert "US" in labels
    assert values[labels.index("India")] == 50.0
    assert values[labels.index("US")] == 32.0

    # 3. Slack App Home blocks:
    blocks = render_response_blocks(response)
    blocks_str = str(blocks)
    assert "Contacts by Country" in blocks_str
    assert "India" in blocks_str
    assert "*50* contacts" in blocks_str
    assert "*32* contacts" in blocks_str
    # Total count footer must be 82 contacts, NOT deals
    assert "*82* contacts" in blocks_str
    assert "82 deals" not in blocks_str
    assert "Count Id: 82" not in blocks_str


def test_plan_show_last_10_contacts_created():
    from app.agent.crm_intelligence import CRMIntelligenceSort
    plan = plan_crm_query("Show last 10 contacts created")
    assert plan.object == "contact"
    assert plan.limit == 10
    assert plan.sort == [CRMIntelligenceSort(field="createdate", direction="desc")]
    assert "createdate" in plan.properties


@pytest.mark.asyncio
async def test_show_last_10_contacts_created_preserves_timestamp_precision_and_selection():
    # 8 contacts created Nov 6
    nov6 = [
        HubSpotContact(id=f"c-nov6-{i}", properties={"firstname": f"UserNov6_{i}", "createdate": f"2026-11-06T10:0{i}:00Z"})
        for i in range(8)
    ]
    # 3 contacts created Nov 5 with different timestamps:
    # Naveen (18:00), Seenu (14:00), test1 (08:00)
    naveen = HubSpotContact(id="c-naveen", properties={"firstname": "Naveen", "createdate": "2026-11-05T18:00:00Z"})
    seenu = HubSpotContact(id="c-seenu", properties={"firstname": "Seenu", "createdate": "2026-11-05T14:00:00Z"})
    test1 = HubSpotContact(id="c-test1", properties={"firstname": "test1", "createdate": "2026-11-05T08:00:00Z"})
    # 5 older contacts created Nov 4
    nov4 = [
        HubSpotContact(id=f"c-nov4-{i}", properties={"firstname": f"UserNov4_{i}", "createdate": f"2026-11-04T10:0{i}:00Z"})
        for i in range(5)
    ]
    all_contacts = nov6 + [test1, seenu, naveen] + nov4  # purposely unsorted in repo
    agent = make_test_agent(contacts=all_contacts)

    response = await agent.respond(ask("Show last 10 contacts created"))
    assert response.status == "ok"
    result = response.result
    rows = result["table"]["rows"]

    # Must select exactly 10 records
    assert len(rows) == 10
    assert result["summary"]["matched_records"] == 16
    assert result["summary"]["returned_records"] == 10

    # Names in order: 8 Nov 6 records first, then Naveen (18:00), then Seenu (14:00). test1 (08:00) is 11th, excluded!
    names = [r.get("firstname") for r in rows]
    assert names[8] == "Naveen"
    assert names[9] == "Seenu"
    assert "test1" not in names


@pytest.mark.parametrize(
    ("phrase", "expected_enum"),
    [
        ("In progress", "IN_PROGRESS"),
        ("New", "NEW"),
        ("Open", "OPEN"),
        ("Open deal", "OPEN_DEAL"),
        ("Unqualified", "UNQUALIFIED"),
        ("Attempted to contact", "ATTEMPTED_TO_CONTACT"),
        ("Connected", "CONNECTED"),
        ("Bad timing", "BAD_TIMING"),
    ],
)
def test_plan_lead_status_queries_all_eight_statuses(phrase: str, expected_enum: str):
    plan = plan_crm_query(f"List all contacts who are lead status {phrase}")
    assert plan.object == "contact"
    assert "hs_lead_status" in plan.properties
    assert any(
        f.field == "hs_lead_status" and f.operator == "eq" and f.value == expected_enum
        for f in plan.filters
    )
    # Must not add redundant lifecycle stage lead filter
    assert not any(f.field == "lifecyclestage" for f in plan.filters)


def test_plan_all_contacts_across_lead_statuses():
    plan = plan_crm_query("List all contacts across lead statuses")
    assert plan.object == "contact"
    assert "hs_lead_status" in plan.properties
    assert not any(f.field == "hs_lead_status" for f in plan.filters)


@pytest.mark.asyncio
async def test_agent_filters_contacts_by_lead_status_in_progress():
    contacts = [
        HubSpotContact(id="c-1", properties={"firstname": "Alice", "hs_lead_status": "IN_PROGRESS"}),
        HubSpotContact(id="c-2", properties={"firstname": "Bob", "hs_lead_status": "NEW"}),
        HubSpotContact(id="c-3", properties={"firstname": "Charlie", "hs_lead_status": "OPEN_DEAL"}),
        HubSpotContact(id="c-4", properties={"firstname": "David", "hs_lead_status": "UNQUALIFIED"}),
        HubSpotContact(id="c-5", properties={"firstname": "Eve", "hs_lead_status": "ATTEMPTED_TO_CONTACT"}),
        HubSpotContact(id="c-6", properties={"firstname": "Frank", "hs_lead_status": "CONNECTED"}),
        HubSpotContact(id="c-7", properties={"firstname": "Grace", "hs_lead_status": "BAD_TIMING"}),
        HubSpotContact(id="c-8", properties={"firstname": "Heidi", "hs_lead_status": "IN_PROGRESS"}),
    ]
    agent = make_test_agent(contacts=contacts)
    response = await agent.respond(ask("List all contacts who are lead status In progress"))

    assert response.status == "ok"
    result = response.result
    rows = result["table"]["rows"]
    assert len(rows) == 2
    assert {r["firstname"] for r in rows} == {"Alice", "Heidi"}
    assert all(r["hs_lead_status"] == "IN_PROGRESS" for r in rows)


