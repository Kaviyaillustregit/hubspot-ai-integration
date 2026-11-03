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
    HubSpotDeal,
    HubSpotDealsPage,
    HubSpotPipeline,
    HubSpotPipelineStage,
)
from app.integrations.hubspot.properties import HubSpotPropertyDefinition

PIPELINE = HubSpotPipeline(
    id="default",
    label="Sales",
    stages=[
        HubSpotPipelineStage(
            id="s-new", label="Qualification", displayOrder=0, metadata={"isClosed": "false"}
        ),
        HubSpotPipelineStage(
            id="s-prop", label="Proposal", displayOrder=1, metadata={"isClosed": "false"}
        ),
        HubSpotPipelineStage(
            id="s-won",
            label="Closed Won",
            displayOrder=2,
            metadata={"isClosed": "true", "probability": "1.0"},
        ),
        HubSpotPipelineStage(
            id="s-lost",
            label="Closed Lost",
            displayOrder=3,
            metadata={"isClosed": "true", "probability": "0.0"},
        ),
    ],
)


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
        if object_type == "deals":
            return [
                HubSpotPropertyDefinition(name="dealname", label="Deal Name"),
                HubSpotPropertyDefinition(name="amount", label="Amount", type="number"),
                HubSpotPropertyDefinition(name="dealstage", label="Deal Stage"),
                HubSpotPropertyDefinition(name="closedate", label="Close Date", type="date"),
                HubSpotPropertyDefinition(name="pipeline", label="Pipeline"),
            ]
        if object_type == "companies":
            return [
                HubSpotPropertyDefinition(name="name", label="Company Name"),
                HubSpotPropertyDefinition(name="industry", label="Industry"),
                HubSpotPropertyDefinition(name="city", label="City"),
            ]
        return [
            HubSpotPropertyDefinition(name="firstname", label="First Name"),
            HubSpotPropertyDefinition(name="lastname", label="Last Name"),
            HubSpotPropertyDefinition(name="email", label="Email"),
            HubSpotPropertyDefinition(name="jobtitle", label="Job Title"),
            HubSpotPropertyDefinition(name="city", label="City"),
            HubSpotPropertyDefinition(name="lifecyclestage", label="Lifecycle Stage"),
            HubSpotPropertyDefinition(name="hs_lead_status", label="Lead Status"),
        ]


class MockDeals:
    def __init__(self, deals):
        self.deals = deals

    async def list_deals(self, context, **kwargs):
        offset = int(kwargs.get("after") or 0)
        chunk = self.deals[offset : offset + 20]
        more = offset + 20 < len(self.deals)
        return HubSpotDealsPage(
            results=chunk, next_after=str(offset + 20) if more else None
        )

    async def list_pipelines(self, context):
        return [PIPELINE]

    async def get_deal_type_options(self, context):
        return []


def _make_deal(deal_id: int, name: str, stage: str, amount: int, owner: str = "user-1"):
    return HubSpotDeal(
        id=str(deal_id),
        properties={
            "dealname": name,
            "dealstage": stage,
            "pipeline": "default",
            "amount": str(amount),
            "hubspot_owner_id": owner,
            "closedate": "2026-11-15T00:00:00Z",
        },
    )


def sample_deals():
    return [
        _make_deal(1, "Alpha Tech", "s-new", 850000, "Alice"),
        _make_deal(2, "Beta Corp", "s-prop", 420000, "Bob"),
        _make_deal(3, "Gamma LLC", "s-won", 1200000, "Alice"),
        _make_deal(4, "Delta Inc", "s-prop", 310000, "Charlie"),
        _make_deal(5, "Epsilon Group", "s-lost", 150000, "Bob"),
        _make_deal(6, "Zeta Co", "s-new", 950000, "Alice"),
        _make_deal(7, "Eta Systems", "s-won", 670000, "Charlie"),
    ]


def make_test_agent(deals=None, contacts=None):
    deals = deals or sample_deals()
    repo = MockDeals(deals)
    contact_repo = MockContacts(contacts)
    return AccountIntelligenceAgent(
        HubSpotToolRegistry(
            MockCompanies(),
            contact_repo,
            repo,
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


# ---------------------------------------------------------------------------
# 1. Core user examples without explicit chart keywords
# ---------------------------------------------------------------------------


def test_core_example_top_5_opportunities():
    plan = plan_crm_query("Show top 5 opportunities")
    assert plan.chart_requested is True
    assert plan.chart_type == "bar"


def test_core_example_compare_deals_by_stage():
    plan = plan_crm_query("Compare deals by stage")
    assert plan.chart_requested is True
    assert plan.chart_type == "bar"


def test_core_example_deal_amount_by_month():
    plan = plan_crm_query("Show total deal amount by month")
    assert plan.chart_requested is True
    assert plan.chart_type == "line"


def test_core_example_company_distribution_by_industry():
    plan = plan_crm_query("Show company distribution by industry")
    assert plan.chart_requested is True
    assert plan.chart_type == "doughnut"


def test_core_example_compare_sales_performance_by_owner():
    plan = plan_crm_query("Compare sales performance by owner")
    assert plan.chart_requested is True
    assert plan.chart_type == "bar"


# ---------------------------------------------------------------------------
# 2. Phrasing and synonym variations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected_chart_type"),
    [
        ("Top 3 deals by amount", "bar"),
        ("Show highest 5 opportunities", "bar"),
        ("Biggest deals this month", "bar"),
        ("Compare deal stages", "bar"),
        ("Deal breakdown by stage", "bar"),
        ("Monthly breakdown of deal revenue", "line"),
        ("Deals over time by month", "line"),
        ("Quarterly trend of closed deals", "line"),
        ("Company share by industry", "doughnut"),
        ("Industry proportion breakdown for companies", "doughnut"),
        ("Performance comparison across reps", "bar"),
    ],
)
def test_analytical_query_variations(message: str, expected_chart_type: str):
    plan = plan_crm_query(message)
    assert plan.chart_requested is True
    assert plan.chart_type == expected_chart_type


# ---------------------------------------------------------------------------
# 3. Explicit chart requests (preserve user-specified chart type)
# ---------------------------------------------------------------------------


def test_explicit_pie_chart_requested():
    plan = plan_crm_query("Compare deals by stage as a pie chart")
    assert plan.chart_requested is True
    assert plan.chart_type == "pie"


def test_explicit_bar_chart_requested():
    plan = plan_crm_query("Show company distribution by industry as a bar chart")
    assert plan.chart_requested is True
    assert plan.chart_type == "bar"


# ---------------------------------------------------------------------------
# 4. Explicit negative chart requests ("no chart", "without chart", etc.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Show top 5 opportunities without chart",
        "Show top 5 opportunities without a chart",
        "Compare deals by stage (no chart)",
        "Show total deal amount by month, omit chart",
        "Show company distribution by industry skip chart",
        "Compare sales performance by owner do not show a chart",
        "Show top 5 opportunities chart: none",
    ],
)
def test_negative_chart_requests_suppress_chart(message: str):
    plan = plan_crm_query(message)
    assert plan.chart_requested is False
    assert plan.chart_type is None


# ---------------------------------------------------------------------------
# 5. Non-analytical queries and plain lookups remain chart-free
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Tell me about Acme Corp",
        "Find contact Ava Kumar",
        "Show all open deals",
        "List contacts",
    ],
)
def test_non_analytical_queries_produce_no_chart(message: str):
    plan = plan_crm_query(message)
    assert plan.chart_requested is False
    assert plan.chart_type is None


# ---------------------------------------------------------------------------
# 7. Preserved funnel default for deal pipeline / general deal charts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Show deals as a chart",
        "Deals chart",
        "Pipeline summary with chart",
        "Show pipeline as a chart",
        "Show me our deal pipeline with a chart",
    ],
)
def test_deal_pipeline_charts_default_to_funnel(message: str):
    plan = plan_crm_query(message)
    assert plan.chart_requested is True
    assert plan.chart_type == "funnel"


# ---------------------------------------------------------------------------
# 8. Preserved company and contact grouped chart behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected_group"),
    [
        ("Count companies by industry", "industry"),
        ("Group contacts by lifecyclestage", "lifecyclestage"),
        ("Number of contacts by city", "city"),
        ("Count contacts by owner", "hubspot_owner_id"),
    ],
)
def test_existing_supported_company_contact_groupings_generate_charts(
    message: str, expected_group: str
):
    plan = plan_crm_query(message)
    assert plan.group_by == expected_group
    assert plan.chart_requested is True
    assert plan.chart_type == "bar"


@pytest.mark.parametrize(
    "message",
    [
        "Count companies by industry without chart",
        "Group contacts by lifecyclestage (no chart)",
        "Number of contacts by city omit chart",
    ],
)
def test_company_contact_groupings_respect_negative_chart(message: str):
    plan = plan_crm_query(message)
    assert plan.chart_requested is False
    assert plan.chart_type is None



@pytest.mark.asyncio
async def test_agent_auto_charts_top_5_opportunities():
    agent = make_test_agent()
    response = await agent.respond(ask("Show top 5 opportunities"))

    assert response.status == "ok"
    assert "result" in response.__dict__
    result = response.result
    assert result.get("chart_url") is not None
    assert result["chart_url"].startswith("https://quickchart.io/chart?")
    assert result.get("chart_type") == "bar"
    assert "table" in result
    assert len(result["table"]["rows"]) > 0
    assert "_Chart shown below._" in response.text


@pytest.mark.asyncio
async def test_agent_auto_charts_compare_deals_by_stage():
    agent = make_test_agent()
    response = await agent.respond(ask("Compare deals by stage"))

    assert response.status == "ok"
    result = response.result
    assert result.get("chart_url") is not None
    assert result["chart_url"].startswith("https://quickchart.io/chart?")
    assert result.get("chart_type") == "bar"
    assert "_Chart shown below._" in response.text


@pytest.mark.asyncio
async def test_agent_respects_negative_chart_request():
    agent = make_test_agent()
    response = await agent.respond(ask("Show top 5 opportunities without chart"))

    assert response.status == "ok"
    result = response.result
    assert result.get("chart_url") is None
    assert "chart" not in result
    assert "table" in result
    assert len(result["table"]["rows"]) > 0
    assert "_Chart shown below._" not in response.text


@pytest.mark.asyncio
async def test_agent_stage_distribution_query_generates_chart():
    agent = make_test_agent()
    response = await agent.respond(ask("How many deals are in each stage?"))

    assert response.status == "ok"
    result = response.result
    assert result.get("chart_url") is not None
    assert result["chart_url"].startswith("https://quickchart.io/chart?")
    assert result.get("chart_type") == "bar"
    assert "_Chart shown below._" in response.text


@pytest.mark.asyncio
async def test_agent_open_deals_by_stage_generates_grouped_chart():
    agent = make_test_agent()
    response = await agent.respond(ask("Show open deals by stage"))

    assert response.status == "ok"
    result = response.result
    assert result.get("chart_url") is not None
    assert result["chart_url"].startswith("https://quickchart.io/chart?")
    assert result.get("chart_type") == "bar"
    assert "_Chart shown below._" in response.text


@pytest.mark.asyncio
async def test_agent_lead_distribution_by_status_routes_to_contact_analytics_and_charts():
    contacts = [
        HubSpotContact(
            id="lead-1",
            properties={
                "firstname": "Lead",
                "lastname": "One",
                "email": "lead1@example.com",
                "lifecyclestage": "lead",
                "hs_lead_status": "NEW",
            },
        ),
        HubSpotContact(
            id="lead-2",
            properties={
                "firstname": "Lead",
                "lastname": "Two",
                "email": "lead2@example.com",
                "lifecyclestage": "lead",
                "hs_lead_status": "OPEN",
            },
        ),
        HubSpotContact(
            id="lead-3",
            properties={
                "firstname": "Lead",
                "lastname": "Three",
                "email": "lead3@example.com",
                "lifecyclestage": "lead",
            },
        ),
    ]
    agent = make_test_agent(contacts=contacts)
    response = await agent.respond(ask("How are our leads distributed by status?"))

    assert response.status == "ok"
    result = response.result
    assert result.get("chart_url") is not None
    assert result["chart_url"].startswith("https://quickchart.io/chart?")
    assert "Missing lead status" in result["chart"]["data"]["labels"]


# ---------------------------------------------------------------------------
# 9. Regression tests for lead lifecycle filtering, limits, and chart rendering
# ---------------------------------------------------------------------------


def test_plan_20_leads_with_bar_chart():
    plan = plan_crm_query("Give me 20 leads with a bar chart")
    assert plan.object == "contact"
    assert plan.limit == 20
    assert plan.chart_requested is True
    assert plan.chart_type == "bar"
    assert any(
        f.field == "lifecyclestage" and f.operator == "eq" and f.value == "lead"
        for f in plan.filters
    )


@pytest.mark.asyncio
async def test_agent_20_leads_with_bar_chart_returns_strictly_leads_and_grouped_bar_chart():
    # 25 total contacts: 22 leads with various statuses, 2 customers, 1 SQL
    contacts = [
        HubSpotContact(
            id=f"lead-{i}",
            properties={
                "firstname": f"LeadFirst{i}",
                "lastname": f"LeadLast{i}",
                "email": f"lead{i}@example.com",
                "lifecyclestage": "lead",
                "hs_lead_status": "NEW" if i < 10 else ("OPEN" if i < 18 else "IN_PROGRESS"),
            },
        )
        for i in range(22)
    ] + [
        HubSpotContact(
            id=f"cust-{i}",
            properties={
                "firstname": f"Cust{i}",
                "lastname": "Customer",
                "email": f"cust{i}@example.com",
                "lifecyclestage": "customer",
            },
        )
        for i in range(2)
    ] + [
        HubSpotContact(
            id="sql-1",
            properties={
                "firstname": "SQL1",
                "lastname": "User",
                "email": "sql1@example.com",
                "lifecyclestage": "salesqualifiedlead",
            },
        )
    ]

    agent = make_test_agent(contacts=contacts)
    response = await agent.respond(ask("Give me 20 leads with a bar chart"))

    assert response.status == "ok"
    result = response.result
    # 1. Returned records are at most 20 and all are strictly leads
    rows = result["rows"]
    assert len(rows) == 20
    assert all("Cust" not in row["firstname"] and "SQL" not in row["firstname"] for row in rows)

    # 2. Chart is generated and present
    assert result.get("chart_url") is not None
    assert result["chart_url"].startswith("https://quickchart.io/chart?")
    assert result.get("chart_type") == "bar"
    assert result.get("chart_title") == "Leads by Status"

    # 3. Chart values exactly match the 20 returned records
    chart_data = result["chart"]["data"]
    assert sum(chart_data["datasets"][0]["data"]) == 20
    assert "_Chart shown below._" in response.text


@pytest.mark.asyncio
async def test_agent_empty_results_with_chart_requested_explains_no_records():
    # Agent with 0 contacts
    agent = make_test_agent(contacts=[])
    response = await agent.respond(ask("Give me 20 leads with a bar chart"))

    assert response.status == "ok"
    result = response.result
    assert result.get("chart_url") is None
    assert "_Chart could not be generated: no matching records found._" in response.text


@pytest.mark.asyncio
async def test_agent_deal_listing_with_chart_generates_bar_chart():
    agent = make_test_agent()
    response = await agent.respond(ask("Show 5 deals as a bar chart"))

    assert response.status == "ok"
    result = response.result
    assert result.get("chart_url") is not None
    assert result["chart_url"].startswith("https://quickchart.io/chart?")
    assert result.get("chart_type") == "bar"
    assert "_Chart shown below._" in response.text
