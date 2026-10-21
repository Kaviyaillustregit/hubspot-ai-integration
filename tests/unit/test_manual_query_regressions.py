import pytest

from app.agent.crm_intelligence import (
    CRMIntelligenceAggregation,
    CRMQueryPlan,
    analyze_deals,
    plan_crm_query,
)
from app.agent.schemas import AgentRequest, CRMIntentExtraction
from app.agent.service import AccountIntelligenceAgent
from app.ai.provider import AIProvider
from app.ai.service import AIService
from app.integrations.hubspot.models import HubSpotContact, HubSpotDeal
from app.integrations.hubspot.properties import HubSpotPropertyDefinition


def test_lead_retrieval_phrases_target_contacts_by_lifecycle_stage():
    for prompt in ("Can you show me our leads?", "Who are our potential customers?"):
        plan = plan_crm_query(prompt)
        assert plan.object == "contact"
        assert any(
            item.field == "lifecyclestage"
            and item.operator == "in"
            and {"lead", "marketingqualifiedlead", "salesqualifiedlead"}.issubset(
                set(item.value.split(","))
            )
            for item in plan.filters
        )


def test_overdue_query_has_open_stage_and_half_open_close_date_bounds():
    plan = plan_crm_query("Which of my deals are overdue?")
    assert plan.object == "deal"
    assert any(item.field == "dealstage" and item.value == "open" for item in plan.filters)
    assert plan.date_range is not None
    assert plan.date_range.field == "closedate"
    assert plan.date_range.start is None
    assert plan.date_range.end is not None


def test_company_plural_and_contact_city_grouping_are_recognized_as_charts():
    companies = plan_crm_query("Show companies grouped by industry")
    assert companies.object == "company"
    assert companies.group_by == "industry"
    assert companies.chart_requested is True

    contacts = plan_crm_query("Show the number of contacts by city")
    assert contacts.object == "contact"
    assert contacts.group_by == "city"
    assert contacts.chart_requested is True


def test_latest_all_deals_grouped_by_stage_preserves_order_and_grouping():
    plan = plan_crm_query("Show me all latest deals grouped by stage name")
    assert plan.object == "deal"
    assert plan.group_by == "dealstage"
    assert plan.sort[0].field == "closedate"
    assert plan.sort[0].direction == "desc"


def test_quarterly_pipeline_prompt_requests_counts_and_amounts_without_and_filters():
    plan = plan_crm_query(
        "Give me a summary of our sales pipeline for this quarter, including "
        "how many deals are open, won, and lost."
    )
    assert plan.date_range is not None
    assert plan.date_range.relative == "this quarter"
    assert {item.operation for item in plan.aggregations} >= {"count", "sum"}
    assert not plan.filters
    assert plan.group_by == "dealstage"


def test_explicit_status_comparison_is_grouped_and_chartable():
    plan = plan_crm_query("Compare open, won, and lost deals in a chart")
    assert plan.group_by == "dealstatus"
    assert plan.chart_requested is True
    assert plan.chart_type == "bar"


def test_top_five_amount_chart_has_correct_sort_limit_and_type():
    plan = plan_crm_query("Show the top five deals by amount in a bar chart")
    assert plan.group_by is None
    assert plan.sort[0].field == "amount"
    assert plan.sort[0].direction == "desc"
    assert plan.limit == 5
    assert plan.chart_requested is True
    assert plan.chart_type == "bar"


def test_active_pipeline_value_by_stage_is_open_only_and_chartable():
    plan = plan_crm_query("Visualize our active pipeline value by stage")
    assert plan.group_by == "dealstage"
    assert any(item.field == "dealstage" and item.value == "open" for item in plan.filters)
    assert plan.aggregations[0].operation == "sum"
    assert plan.chart_requested is True


class _GroupedContactProvider(AIProvider):
    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(intent="crm_question", query="contact_list", confidence=1.0)
        if prompt_name == "crm-intelligence/v1":
            return plan_crm_query(variables["query"])
        raise AssertionError(prompt_name)


class _GroupedContactTools:
    property_discovery_available = True

    async def discover_properties(self, tenant_id, object_type):
        return [
            HubSpotPropertyDefinition(name="firstname", label="First name"),
            HubSpotPropertyDefinition(name="lastname", label="Last name"),
            HubSpotPropertyDefinition(name="email", label="Email"),
            HubSpotPropertyDefinition(name="jobtitle", label="Job title"),
            HubSpotPropertyDefinition(name="city", label="City"),
            HubSpotPropertyDefinition(name="lifecyclestage", label="Lifecycle stage"),
        ]

    async def list_all_contacts(self, tenant_id, *, properties):
        return [
            HubSpotContact(id="c1", properties={"firstname": "Ava", "lifecyclestage": "lead"}),
            HubSpotContact(id="c2", properties={"firstname": "Bo", "lifecyclestage": "lead"}),
            HubSpotContact(id="c3", properties={"firstname": "Cal", "lifecyclestage": "customer"}),
        ]

    async def hubspot_record_urls(self, tenant_id, records):
        return [f"https://hubspot.test/{record_id}" for _, record_id in records]

    async def list_owners(self, tenant_id):
        return []


@pytest.mark.asyncio
async def test_contact_group_query_returns_matching_chart_and_truthful_slack_text():
    agent = AccountIntelligenceAgent(
        _GroupedContactTools(), AIService(_GroupedContactProvider()), object()
    )
    response = await agent.respond(
        AgentRequest(
            tenant_id="tenant-chart",
            actor_id="user-chart",
            message="Show contacts grouped by lifecycle stage",
            request_id="contact-chart",
        )
    )
    assert response.status == "ok"
    assert "chart" in response.result, response.model_dump()
    assert response.result["chart"]["data"]["labels"] == ["customer", "lead"]
    assert response.result["chart"]["data"]["datasets"][0]["data"] == [1, 2]
    assert response.result["chart_url"].startswith("https://quickchart.io/chart?")
    assert response.text.count("Chart shown below") == 1


@pytest.mark.asyncio
async def test_lead_retrieval_prompts_return_contacts_without_qualification():
    for prompt in ("Can you show me our leads?", "Who are our potential customers?"):
        agent = AccountIntelligenceAgent(
            _GroupedContactTools(), AIService(_GroupedContactProvider()), object()
        )
        response = await agent.respond(
            AgentRequest(
                tenant_id="tenant-leads", actor_id="user-leads", message=prompt,
                request_id=f"leads-{len(prompt)}",
            )
        )
        assert response.status == "ok"
        assert response.result["plan"]["object"] == "contact"
        assert [row["id"] for row in response.result["rows"]] == ["c1", "c2"]
        assert response.result["kind"] == "crm_intelligence"


def test_stage_insight_calls_equal_counts_a_tie_and_uses_precise_value_share():
    plan = CRMQueryPlan(
        object="deal", group_by="dealstage",
        aggregations=[CRMIntelligenceAggregation(operation="sum", field="amount")],
    )
    records = [
        HubSpotDeal(id="w1", properties={"dealname": "Won 1", "dealstage": "won", "amount": "1"}),
        HubSpotDeal(id="l1", properties={"dealname": "Lost 1", "dealstage": "lost", "amount": "2"}),
    ]
    analysis, text = analyze_deals(plan, records, None)
    assert "tied for the most deals (1 of 2 each)" in text
    assert "66.7% of the total" in text
    assert analysis["summary"]["matched_records"] == 2
