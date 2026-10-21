import pytest

from app.agent.crm_intelligence import (
    CRMIntelligenceAggregation,
    CRMIntelligencePlanner,
    CRMIntelligenceSort,
    CRMQueryPlan,
    build_chart_payload,
    execute_plan,
    plan_crm_query,
    summarize_pipeline,
)
from app.ai.provider import AIProvider
from app.ai.service import AIService
from app.integrations.hubspot.models import HubSpotDeal, HubSpotPipeline, HubSpotPipelineStage


class StubProvider(AIProvider):
    async def generate_structured(self, *, prompt_name: str, variables: dict, output_schema):
        assert prompt_name == "crm-intelligence/v1"
        return CRMQueryPlan(
            object="deal",
            group_by="dealstage",
            aggregations=[CRMIntelligenceAggregation(operation="sum", field="amount")],
            sort=[CRMIntelligenceSort(field="closedate", direction="desc")],
            chart_requested=True,
            chart_type="funnel",
        )


@pytest.mark.asyncio
async def test_planner_uses_existing_ai_provider_abstraction():
    planner = CRMIntelligencePlanner(ai_service=AIService(StubProvider()))
    plan = await planner.plan("show pipeline by stage")

    assert plan.object == "deal"
    assert plan.group_by == "dealstage"
    assert plan.chart_type == "funnel"
    assert plan.sort[0].field == "closedate"


def test_rule_based_query_planner_handles_groupby_and_quarter_variations():
    plan = plan_crm_query("show me all the latest deal groupby stagename")
    assert plan.object == "deal"
    assert plan.group_by == "dealstage"
    assert plan.sort[0].field == "closedate"
    assert plan.sort[0].direction == "desc"

    quarter_plan = plan_crm_query("Give me summary of my pipeline for this quatar")
    assert quarter_plan.date_range is not None
    assert quarter_plan.date_range.relative == "this quarter"
    assert quarter_plan.chart_requested is True
    assert quarter_plan.chart_type == "funnel"
    assert quarter_plan.aggregations[0].operation == "sum"
    assert quarter_plan.group_by == "dealstage"


def test_grouping_and_aggregation_are_deterministic_for_deal_records():
    plan = CRMQueryPlan(
        object="deal",
        group_by="dealstage",
        aggregations=[
            CRMIntelligenceAggregation(operation="sum", field="amount"),
            CRMIntelligenceAggregation(operation="count", field="dealname"),
        ],
        chart_type="bar",
    )
    deals = [
        HubSpotDeal(id="d-1", properties={"dealname": "Alpha", "dealstage": "open", "amount": "50000"}),  # noqa: E501
        HubSpotDeal(id="d-2", properties={"dealname": "Beta", "dealstage": "open", "amount": "25000"}),  # noqa: E501
        HubSpotDeal(id="d-3", properties={"dealname": "Gamma", "dealstage": "won", "amount": "150000"}),  # noqa: E501
    ]

    result = execute_plan(plan, deals)
    rows = result["rows"]
    assert [row["label"] for row in rows] == ["open", "won"]
    assert rows[0]["sum_amount"] == "75,000"
    assert rows[0]["count_dealname"] == 2
    assert rows[1]["sum_amount"] == "150,000"


def test_pipeline_summary_uses_stage_order_and_totals():
    pipeline = HubSpotPipeline(
        id="sales",
        label="Sales",
        stages=[
            HubSpotPipelineStage(id="open", label="Qualification", metadata={}),
            HubSpotPipelineStage(id="won", label="Closed Won", metadata={}),
            HubSpotPipelineStage(id="lost", label="Closed Lost", metadata={}),
        ],
    )
    deals = [
        HubSpotDeal(id="d-1", properties={"dealstage": "open", "amount": "50000"}),
        HubSpotDeal(id="d-2", properties={"dealstage": "won", "amount": "150000"}),
        HubSpotDeal(id="d-3", properties={"dealstage": "lost", "amount": "20000"}),
    ]

    summary = summarize_pipeline(deals, pipelines=[pipeline])
    assert [row["stage"] for row in summary] == ["Qualification", "Closed Won", "Closed Lost"]
    assert summary[0]["total_amount"] == "50,000"
    assert summary[1]["total_amount"] == "150,000"
    assert summary[2]["total_amount"] == "20,000"


def test_quickchart_contract_is_preserved_for_grouped_results():
    plan = CRMQueryPlan(
        object="deal",
        group_by="dealstage",
        aggregations=[CRMIntelligenceAggregation(operation="sum", field="amount")],
        chart_requested=True,
        chart_type="funnel",
    )

    payload = build_chart_payload(
        plan,
        [
            {"label": "Qualification", "sum_amount": "50000"},
            {"label": "Closed Won", "sum_amount": "150000"},
        ],
    )

    assert payload["chart_type"] == "funnel"
    assert payload["chart"]["type"] == "bar"
    assert payload["chart"]["data"]["labels"] == ["Qualification", "Closed Won"]
    assert payload["chart"]["data"]["datasets"][0]["data"] == [50000.0, 150000.0]
