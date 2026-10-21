from datetime import UTC, date, datetime, timedelta

import pytest

from app.agent.crm_intelligence import (
    CRMIntelligenceAggregation,
    CRMIntelligenceDateRange,
    CRMQueryPlan,
    _filter_matches,
    _relative_date_range,
    _StageResolver,
    analyze_deals,
    plan_crm_query,
    unsupported_intelligence_message,
)
from app.agent.schemas import AgentRequest, CRMIntentExtraction
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.provider import AIProvider
from app.ai.service import AIService
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotContactsPage,
    HubSpotDeal,
    HubSpotDealsPage,
    HubSpotPipeline,
    HubSpotPipelineStage,
)

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


class Provider(AIProvider):
    def __init__(
        self,
        query=None,
        plan_limit=None,
        deal_filter=None,
        deal_stage=None,
        plan_without_date_range=False,
    ):
        self.query = query
        self.plan_limit = plan_limit
        self.deal_filter = deal_filter
        self.deal_stage = deal_stage
        self.plan_without_date_range = plan_without_date_range

    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(
                intent="crm_question",
                confidence=1.0,
                question=variables["message"],
                query=self.query,
                deal_filter=self.deal_filter,
                deal_stage=self.deal_stage,
            )
        if prompt_name == "crm-intelligence/v1":
            if self.plan_without_date_range:
                return CRMQueryPlan(
                    object="deal",
                    properties=["dealname", "closedate"],
                    limit=11,
                )
            plan = plan_crm_query(variables["query"])
            if self.plan_limit:
                plan = plan.model_copy(update={"limit": self.plan_limit})
            return plan
        raise AssertionError(prompt_name)


class Companies:
    async def list_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=[])

    async def search_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=[])


class Contacts:
    async def get_hubspot_account_id(self, context):
        assert context.tenant_id == "tenant-a"
        return "portal-123"

    async def list_contacts(self, context, **kwargs):
        return HubSpotContactsPage(results=[])

    async def search_contacts(self, context, **kwargs):
        return HubSpotContactsPage(results=[])


class PagedDeals:
    """Serves deals in small pages so cursor pagination is exercised."""

    def __init__(self, deals, page_size=10):
        self.deals = deals
        self.page_size = page_size
        self.calls = 0

    async def list_deals(self, context, **kwargs):
        self.calls += 1
        offset = int(kwargs.get("after") or 0)
        chunk = self.deals[offset : offset + self.page_size]
        more = offset + self.page_size < len(self.deals)
        return HubSpotDealsPage(
            results=chunk, next_after=str(offset + self.page_size) if more else None
        )

    async def list_pipelines(self, context):
        return [PIPELINE]

    async def get_deal_type_options(self, context):
        return []


def deal(i, stage, amount, closedate="2026-01-15", **extra):
    props = {
        "dealname": f"Deal {i}",
        "dealstage": stage,
        "pipeline": "default",
        "closedate": closedate,
    }
    if amount is not None:
        props["amount"] = str(amount)
    props.update(extra)
    return HubSpotDeal(id=str(i), properties=props)


def twenty_six_deals():
    deals = []
    for i in range(10):
        deals.append(deal(i, "s-new", 1000))
    for i in range(10, 16):
        deals.append(deal(i, "s-prop", 2500))
    for i in range(16, 22):
        deals.append(deal(i, "s-won", 10000))
    for i in range(22, 25):
        deals.append(deal(i, "s-lost", 500))
    deals.append(deal(25, "s-won", None))
    return deals


def make_agent(deals, **provider_kwargs):
    repo = PagedDeals(deals)
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts(), repo),
        AIService(Provider(**provider_kwargs)),
        object(),
    )
    return agent, repo


def ask(message):
    return AgentRequest(tenant_id="tenant-a", actor_id="u", message=message, request_id="r-1")


def assert_clean(text):
    assert "?" * 3 not in text
    assert "\ufffd" not in text
    assert "CRM intelligence summary" not in text
    assert "Records analyzed" not in text
    assert "|---" not in text


def by_label(result):
    return {row["label"]: row for row in result.result["rows"]}


@pytest.mark.asyncio
async def test_each_stage_counts_cover_all_26_deals_despite_default_limit_of_20():
    agent, repo = make_agent(twenty_six_deals())

    result = await agent.respond(ask("How many HubSpot deals are in each deal stage?"))

    assert repo.calls == 3
    rows = by_label(result)
    assert {k: v["count"] for k, v in rows.items()} == {
        "Qualification": 10,
        "Proposal": 6,
        "Closed Won": 7,
        "Closed Lost": 3,
    }
    assert sum(row["count"] for row in rows.values()) == 26
    assert result.result["summary"]["total_records"] == 26
    assert result.result["summary"]["has_more"] is False
    assert "Qualification*: 10 deals" in result.text
    assert "Closed Won*: 7 deals" in result.text
    assert "Overall*: 26 deals" in result.text
    assert "Total" not in result.text.split("Overall")[1].split("\n")[0]
    assert "10,000" not in result.text  # count-only answers show no amounts
    assert_clean(result.text)


def test_grouped_all_deals_include_owner_and_page_detail_records_after_twenty():
    deals = [
        deal(i, "s-new", i * 100, hubspot_owner_id="owner-1")
        for i in range(26)
    ]
    plan = plan_crm_query(
        "Show me all deals grouped by owner, including deal name, amount, stage, and closing date"
    )

    query = (
        "Show me all deals grouped by owner, including deal name, amount, stage, "
        "and closing date"
    )
    analysis, text = analyze_deals(plan, deals, [PIPELINE], query)

    assert len(analysis["details"]) == 20
    assert analysis["details"][0]["hubspot_owner_id"] == "owner-1"
    assert analysis["summary"]["details_total"] == 26
    assert analysis["summary"]["details_has_more"] is True
    assert "Ask for page 2" in text
    assert "Showing 20 of 26" in text


@pytest.mark.asyncio
async def test_count_query_keeps_working_when_extractor_also_sets_a_deal_query():
    agent, _ = make_agent(twenty_six_deals(), query="all_deals")

    result = await agent.respond(ask("How many HubSpot deals are in each deal stage?"))

    assert result.result["summary"]["total_records"] == 26
    assert "Proposal*: 6 deals" in result.text


@pytest.mark.asyncio
async def test_explicit_ai_planner_limit_does_not_truncate_aggregation():
    agent, _ = make_agent(twenty_six_deals(), plan_limit=5)

    result = await agent.respond(
        ask(
            "Give me a summary of our HubSpot deal pipeline, including deal counts "
            "and total amounts by stage."
        )
    )

    assert sum(row["count"] for row in result.result["rows"]) == 26


@pytest.mark.asyncio
async def test_pipeline_summary_reports_counts_and_totals_from_all_deals():
    agent, _ = make_agent(twenty_six_deals())

    result = await agent.respond(
        ask(
            "Give me a summary of our HubSpot deal pipeline, including deal counts "
            "and total amounts by stage."
        )
    )

    rows = by_label(result)
    assert rows["Qualification"]["sum_amount"] == "10,000"
    assert rows["Proposal"]["sum_amount"] == "15,000"
    assert rows["Closed Won"]["sum_amount"] == "60,000"  # the deal without an amount adds nothing
    assert rows["Closed Lost"]["sum_amount"] == "1,500"
    assert "Qualification*: 10 deals" in result.text
    assert "Total 60,000" in result.text
    assert "Overall*: 26 deals" in result.text and "Total 86,500" in result.text
    assert "1 deal has no amount" in result.text
    assert "Insight:" in result.text
    assert_clean(result.text)
    # Structured contract is preserved.
    assert result.result["kind"] == "crm_intelligence"
    assert result.result["plan"]["group_by"] == "dealstage"
    assert result.result["chart"]["type"] == "bar"
    assert result.result["chart_type"] == "funnel"
    assert result.result["chart"]["data"]["datasets"][0]["data"] == [
        10000.0,
        15000.0,
        60000.0,
        1500.0,
    ]
    assert result.result["chart_url"].startswith("https://quickchart.io/chart")
    assert "Chart shown below" in result.text


@pytest.mark.asyncio
async def test_chart_request_for_counts_charts_counts_not_amounts():
    agent, _ = make_agent(twenty_six_deals())

    result = await agent.respond(ask("Show a bar chart of how many deals are in each stage"))

    assert result.result["chart"]["data"]["datasets"][0]["data"] == [10.0, 6.0, 7.0, 3.0]
    assert "$" not in str(result.result["chart"]["options"])
    assert_clean(result.text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent_error",
    [RuntimeError("LLM returned an invalid response"), TimeoutError("LLM timed out")],
)
async def test_rule_based_fallback_routes_stage1_deal_queries_when_llm_extraction_fails(
    intent_error,
):
    class FailingIntentProvider(AIProvider):
        def __init__(self, error):
            self.error = error

        async def generate_structured(self, *, prompt_name, variables, output_schema):
            if prompt_name == "crm-intent/v2":
                raise self.error
            if prompt_name == "crm-intelligence/v1":
                return plan_crm_query(variables["query"])
            raise AssertionError(prompt_name)

    q3_agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(),
            Contacts(),
            PagedDeals([deal(1, "s-won", 100, closedate="2026-07-15")]),
        ),
        AIService(FailingIntentProvider(intent_error)),
        object(),
    )
    q3_result = await q3_agent.respond(ask("Show me all HubSpot deals closing in Q3 2026."))
    assert q3_result.status == "ok"
    assert q3_result.result["kind"] == "crm_intelligence"
    assert q3_result.result["summary"]["matched_records"] == 1
    assert "deal stage named 'Q3 2026'" not in q3_result.text.lower()

    latest_agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(),
            Contacts(),
            PagedDeals(
                [
                    deal(1, "s-new", 10),
                    deal(2, "s-new", 20),
                    deal(3, "s-new", 30),
                ]
            ),
        ),
        AIService(FailingIntentProvider(intent_error)),
        object(),
    )
    latest = await latest_agent.respond(
        ask(
            "Show me the latest 2 HubSpot deals with their deal names, amounts, "
            "stages, and closing dates."
        )
    )
    assert latest.status == "ok"
    assert latest.result["kind"] == "crm_intelligence"
    assert latest.result["summary"]["matched_records"] == 3
    assert "deal stage named 's'" not in latest.text.lower()


@pytest.mark.asyncio
async def test_q3_date_filter_and_latest_deals_are_not_misread_as_stage_lookups():
    q3_deals = [
        deal(1, "s-won", 100, closedate="2026-07-15"),
        deal(2, "s-won", 250, closedate="2026-10-05"),
        deal(3, "s-new", 300, closedate="2027-01-10"),
    ]
    q3_agent, _ = make_agent(q3_deals)
    q3_result = await q3_agent.respond(ask("Show me all HubSpot deals closing in Q3 2026."))

    assert q3_result.status == "ok"
    assert q3_result.result["kind"] == "crm_intelligence"
    assert q3_result.result["summary"]["matched_records"] == 1
    assert [row["dealname"] for row in q3_result.result["rows"]] == ["Deal 1"]
    assert "deal stage named 'Q3 2026'" not in q3_result.text.lower()

    latest_agent, _ = make_agent(
        [
            deal(1, "s-new", 10, closedate="2026-01-01"),
            deal(2, "s-new", 20, closedate="2026-01-02"),
            deal(3, "s-new", 30, closedate="2026-01-03"),
            deal(4, "s-new", 40, closedate="2026-01-04"),
            deal(5, "s-new", 50, closedate="2026-01-05"),
            deal(6, "s-new", 60, closedate="2026-01-06"),
        ]
    )
    latest = await latest_agent.respond(
        ask(
            "Show me the latest 5 HubSpot deals with their deal names, amounts, stages, "
            "and closing dates."
        )
    )

    assert latest.status == "ok"
    assert latest.result["kind"] == "crm_intelligence"
    assert latest.result["summary"]["matched_records"] == 6
    assert [row["dealname"] for row in latest.result["rows"]] == [
        "Deal 6",
        "Deal 5",
        "Deal 4",
        "Deal 3",
        "Deal 2",
    ]
    assert "deal stage named 's'" not in latest.text.lower()
    from app.integrations.slack.home import render_response_blocks

    rendered = str(render_response_blocks(latest))
    assert "Deal 6" in rendered and "Jan 6, 2026" in rendered
    assert latest.text.count("Deal 6") == 1
    assert rendered.count("Deal 6") == 1


@pytest.mark.asyncio
async def test_explicit_q3_range_is_canonicalized_when_planner_uses_inclusive_end():
    class InclusiveQuarterProvider(Provider):
        async def generate_structured(self, *, prompt_name, variables, output_schema):
            if prompt_name == "crm-intent/v2":
                return CRMIntentExtraction(
                    intent="crm_question", confidence=1.0,
                    question=variables["message"], query="all_deals",
                )
            if prompt_name == "crm-intelligence/v1":
                return CRMQueryPlan(
                    object="deal", properties=["dealname", "closedate"],
                    date_range=CRMIntelligenceDateRange(
                        start="2026-07-01", end="2026-09-30", relative="q3 2026"
                    ), limit=20,
                )
            raise AssertionError(prompt_name)

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(), Contacts(),
            PagedDeals([
                deal(1, "s-new", 10, closedate="2026-09-29T23:30:00Z"),
                deal(2, "s-new", 20, closedate="2026-09-30"),
                deal(3, "s-new", 30, closedate="2026-10-01"),
            ]),
        ),
        AIService(InclusiveQuarterProvider()), object(),
    )

    result = await agent.respond(ask("Show me all HubSpot deals closing in Q3 2026."))

    assert result.result["plan"]["date_range"]["end"] == "2026-10-01"
    assert [row["id"] for row in result.result["rows"]] == ["2", "1"]
    assert "Sep 30, 2026" in str(result.result["table"])


@pytest.mark.asyncio
async def test_this_quarter_heading_and_table_response_are_compact_and_not_duplicated():
    agent, _ = make_agent(
        [deal(1, "s-new", 100, closedate="2026-10-10"),
         deal(2, "s-won", 250, closedate="2026-12-31")]
    )

    result = await agent.respond(ask("Show me deals closing this quarter."))

    from app.integrations.slack.home import render_response_blocks

    rendered = str(render_response_blocks(result))
    assert "Deals closing this quarter (Q4 2026)" in result.text
    assert result.text.count("Deal 1") == 1
    assert rendered.count("Deal 1") == 1
    assert "Oct 10, 2026" in rendered


@pytest.mark.parametrize(
    ("message", "deals", "expected_ids", "bounds"),
    [
        (
            "Show me all HubSpot deals closing in Q3 2026.",
            [
                deal(1, "s-new", 10, closedate="2026-07-01"),
                deal(2, "s-new", 20, closedate="2026-09-30"),
                deal(3, "s-new", 30, closedate="2026-10-01"),
            ],
            ["2", "1"],
            ("2026-07-01", "2026-10-01"),
        ),
        (
            "Show me all HubSpot deals closing in December 2026.",
            [
                deal(1, "s-new", 10, closedate="2026-11-30"),
                deal(2, "s-new", 20, closedate="2026-12-01"),
                deal(3, "s-new", 30, closedate="2026-12-31"),
                deal(4, "s-new", 40, closedate="2027-01-01"),
            ],
            ["3", "2"],
            ("2026-12-01", "2027-01-01"),
        ),
        (
            "Show me all HubSpot deals closing on December 31, 2026.",
            [
                deal(1, "s-new", 10, closedate="2026-12-30"),
                deal(2, "s-new", 20, closedate="2026-12-31"),
                deal(3, "s-new", 30, closedate="2027-01-01"),
            ],
            ["2"],
            ("2026-12-31", "2027-01-01"),
        ),
        (
            "Show me all HubSpot deals closing on February 29, 2024.",
            [
                deal(1, "s-new", 10, closedate="2024-02-28"),
                deal(2, "s-new", 20, closedate="2024-02-29"),
                deal(3, "s-new", 30, closedate="2024-03-01"),
            ],
            ["2"],
            ("2024-02-29", "2024-03-01"),
        ),
    ],
)
@pytest.mark.asyncio
async def test_explicit_date_ranges_have_inclusive_end_boundaries(
    message, deals, expected_ids, bounds
):
    agent, _ = make_agent(deals, query="all_deals")

    result = await agent.respond(ask(message))

    assert result.result["kind"] == "crm_intelligence"
    date_range = result.result["plan"]["date_range"]
    assert (date_range["start"], date_range["end"]) == bounds
    assert [row["id"] for row in result.result["rows"]] == expected_ids
    assert "deal stage named 'december 31, 2026'" not in result.text.lower()


@pytest.mark.asyncio
async def test_custom_iso_date_range_uses_an_inclusive_end_date():
    plan = plan_crm_query("Show deals from 2026-02-27 to 2026-02-28")

    assert plan.date_range is not None
    assert plan.date_range.start == "2026-02-27"
    assert plan.date_range.end == "2026-03-01"

    agent, _ = make_agent(
        [
            deal(1, "s-new", 10, closedate="2026-02-26"),
            deal(2, "s-new", 20, closedate="2026-02-27"),
            deal(3, "s-new", 30, closedate="2026-02-28"),
            deal(4, "s-new", 40, closedate="2026-03-01"),
        ],
        query="all_deals",
    )
    result = await agent.respond(ask("Show deals from 2026-02-27 to 2026-02-28"))
    assert [row["id"] for row in result.result["rows"]] == ["3", "2"]


def test_pipeline_grouping_is_supported_and_uses_pipeline_labels():
    plan = plan_crm_query("How many deals are in each pipeline?")
    plan.aggregations = [CRMIntelligenceAggregation(operation="count", field="dealname")]
    other_pipeline = deal(30, "s-new", 10, pipeline="other")
    other = HubSpotPipeline(id="other", label="Services", stages=[])
    analysis, _ = analyze_deals(
        plan,
        [deal(1, "s-new", 10), other_pipeline],
        [PIPELINE, other],
        "How many deals are in each pipeline?",
    )
    assert {row["label"]: row["count"] for row in analysis["rows"]} == {"Sales": 1, "Services": 1}


@pytest.mark.asyncio
async def test_all_deals_extraction_routes_q3_query_past_invalid_legacy_date_metadata():
    invalid_legacy_date_metadata = {"close_date": {"quarter": "Q3 2026"}}
    agent, _ = make_agent(
        [
            deal(1, "s-new", 10, closedate="2026-07-15"),
            deal(2, "s-new", 20, closedate="2026-10-05"),
        ],
        query="all_deals",
        deal_filter=invalid_legacy_date_metadata,
    )

    result = await agent.respond(ask("Show me all HubSpot deals closing in Q3 2026."))

    assert result.status == "ok"
    assert result.result["kind"] == "crm_intelligence"
    assert result.result["plan"]["date_range"]["relative"] == "q3 2026"
    assert [row["dealname"] for row in result.result["rows"]] == ["Deal 1"]
    assert "couldn't understand the deal filter metadata" not in result.text.lower()


@pytest.mark.parametrize(
    ("deal_stage", "deal_filter"),
    [
        ("December 31", None),
        (None, {"stages": ["December 31"]}),
    ],
)
@pytest.mark.asyncio
async def test_missing_llm_date_range_is_recovered_before_legacy_stage_resolution(
    deal_stage, deal_filter
):
    deals = [
        deal(1, "s-new", 10, closedate="2026-12-30"),
        deal(2, "s-new", 20, closedate="2026-12-31"),
        deal(3, "s-new", 30, closedate="2027-01-01"),
    ]
    agent, _ = make_agent(
        deals,
        query="all_deals",
        deal_stage=deal_stage,
        deal_filter=deal_filter,
        plan_without_date_range=True,
    )

    result = await agent.respond(ask("Show me all HubSpot deals closing on December 31, 2026."))

    assert result.status == "ok"
    assert result.result["kind"] == "crm_intelligence"
    assert result.result["plan"]["limit"] == 11
    assert result.result["plan"]["properties"] == ["dealname", "closedate"]
    assert result.result["plan"]["date_range"]["start"] == "2026-12-31"
    assert result.result["plan"]["date_range"]["end"] == "2027-01-01"
    assert [row["id"] for row in result.result["rows"]] == ["2"]
    assert "couldn't find a deal stage named" not in result.text.lower()


@pytest.mark.asyncio
async def test_missing_llm_date_range_can_recover_to_a_valid_empty_date_result():
    agent, _ = make_agent(
        [deal(1, "s-new", 10, closedate="2026-12-30")],
        query="all_deals",
        deal_stage="December 31",
        plan_without_date_range=True,
    )

    result = await agent.respond(ask("Show me all HubSpot deals closing on December 31, 2026."))

    assert result.status == "ok"
    assert result.result["kind"] == "crm_intelligence"
    assert result.result["rows"] == []
    assert result.result["summary"]["matched_records"] == 0
    assert "couldn't find a deal stage named" not in result.text.lower()


def test_ambiguous_date_without_year_does_not_create_a_date_range():
    message = "Show me all HubSpot deals closing on December 31."
    plan = CRMQueryPlan(object="deal", limit=13)
    recovered = AccountIntelligenceAgent._recover_missing_date_range(plan, message)

    assert plan_crm_query(message).date_range is None
    assert recovered.date_range is None
    assert recovered.limit == 13


def test_missing_date_recovery_preserves_an_explicit_planner_range_and_other_fields():
    original_range = CRMIntelligenceDateRange(start="2026-12-01", end="2027-01-01")
    original_plan = CRMQueryPlan(
        object="deal",
        properties=["dealname"],
        limit=7,
        date_range=original_range,
    )

    recovered = AccountIntelligenceAgent._recover_missing_date_range(
        original_plan,
        "Show me all HubSpot deals closing on December 31, 2026.",
    )

    assert recovered.date_range == original_range
    assert recovered.properties == ["dealname"]
    assert recovered.limit == 7


@pytest.mark.asyncio
async def test_all_deals_extraction_routes_latest_listing_to_stage1():
    agent, _ = make_agent(
        [deal(i, "s-new", i * 10, closedate=f"2026-01-0{i}") for i in range(1, 7)],
        query="all_deals",
        deal_filter={"close_date": {"sort": "latest"}},
    )

    result = await agent.respond(
        ask(
            "Show me the latest 5 HubSpot deals with their deal names, amounts, stages, "
            "and closing dates."
        )
    )

    assert result.status == "ok"
    assert result.result["kind"] == "crm_intelligence"
    assert result.result["plan"]["sort"][0]["field"] == "closedate"
    assert [row["dealname"] for row in result.result["rows"]] == [
        "Deal 6",
        "Deal 5",
        "Deal 4",
        "Deal 3",
        "Deal 2",
    ]
    assert "couldn't understand the deal filter metadata" not in result.text.lower()


@pytest.mark.asyncio
async def test_chart_uses_requested_metric_for_counts_and_averages():
    count_agent, _ = make_agent(twenty_six_deals())
    count_result = await count_agent.respond(
        ask("Show me a chart of how many HubSpot deals are in each deal stage.")
    )
    assert count_result.result["chart"]["data"]["datasets"][0]["data"] == [10.0, 6.0, 7.0, 3.0]

    average_agent, _ = make_agent(twenty_six_deals())
    average_result = await average_agent.respond(
        ask("Show a chart of the average deal amount in each deal stage.")
    )
    assert average_result.result["chart"]["data"]["datasets"][0]["data"] == [
        1000.0,
        2500.0,
        10000.0,
        500.0,
    ]
    assert (
        average_result.result["chart"]["data"]["datasets"][0]["data"]
        != count_result.result["chart"]["data"]["datasets"][0]["data"]
    )  # noqa: E501


@pytest.mark.asyncio
async def test_no_chart_claim_when_chart_not_requested():
    agent, _ = make_agent(twenty_six_deals())

    result = await agent.respond(ask("How many deals are in each stage?"))

    assert "chart" not in result.result
    assert "Chart shown" not in result.text


@pytest.mark.asyncio
async def test_quarter_filter_is_applied_to_all_pages_and_reported():
    today = datetime.now(UTC).date()
    start, _ = _relative_date_range("this quarter", today)
    inside = date.fromisoformat(start) + timedelta(days=3)
    deals = [deal(i, "s-new", 100, closedate=inside.isoformat()) for i in range(23)]
    deals += [deal(100 + i, "s-won", 999, closedate="2019-01-01") for i in range(3)]
    deals.append(deal(200, "s-won", 5, closedate=None))
    agent, _ = make_agent(deals)

    result = await agent.respond(ask("How many deals are in each stage this quarter?"))

    assert by_label(result)["Qualification"]["count"] == 23
    assert "Closed Won" not in by_label(result)
    assert result.result["summary"]["total_records"] == 27
    assert result.result["summary"]["matched_records"] == 23
    assert "(this quarter)" in result.text
    assert "without a close date was excluded" in result.text


@pytest.mark.asyncio
async def test_empty_hubspot_and_empty_filter_results_are_explained():
    agent, _ = make_agent([])
    empty = await agent.respond(ask("How many deals are in each stage?"))
    assert empty.status == "ok" and empty.result["rows"] == []
    assert "no deals" in empty.text.lower()

    agent, _ = make_agent([deal(1, "s-new", 1, closedate="2019-01-01")])
    none_match = await agent.respond(ask("Summary of my pipeline this quarter"))
    assert none_match.result["rows"] == []
    assert "No deals matched" in none_match.text
    assert "1 deal in total" in none_match.text
    assert_clean(none_match.text)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "What is the average deal amount by stage?",
            ["Average 1,000", "Average 2,500", "Average 10,000"],
        ),
        (
            "Show the highest deal amount by stage",
            ["Maximum 1,000", "Maximum 2,500", "Maximum 10,000"],
        ),
        (
            "Show the lowest deal amount by stage",
            ["Minimum 1,000", "Minimum 2,500", "Minimum 10,000"],
        ),
    ],
)
@pytest.mark.asyncio
async def test_average_min_max_per_stage(message, expected):
    agent, _ = make_agent(twenty_six_deals())

    result = await agent.respond(ask(message))

    for fragment in expected:
        assert fragment in result.text
    assert "Total" not in result.text.split("\n")[1]
    assert_clean(result.text)


@pytest.mark.asyncio
async def test_average_ignores_missing_amounts_and_unpriced_stage_shows_na():
    deals = [deal(1, "s-won", 100), deal(2, "s-won", None), deal(3, "s-new", None)]
    agent, _ = make_agent(deals)

    result = await agent.respond(ask("What is the average deal amount by stage?"))

    rows = by_label(result)
    assert rows["Closed Won"]["avg_amount"] == "100"
    assert rows["Qualification"]["avg_amount"] is None
    assert "Qualification*: 1 deal \u00b7 Average N/A" in result.text


@pytest.mark.asyncio
async def test_overall_metrics_without_grouping():
    agent, _ = make_agent(twenty_six_deals())

    result = await agent.respond(ask("What is the total amount of all my deals?"))

    assert "86,500" in result.text
    assert "26 deals" in result.text
    assert result.result["rows"][0]["sum_amount"] == "86,500"


def test_unknown_stage_and_missing_stage_are_labelled():
    plan = CRMQueryPlan(object="deal", group_by="dealstage")
    deals = [deal(1, "mystery", 1), deal(2, "", 1), deal(3, "s-new", 1)]

    analysis, text = analyze_deals(plan, deals, [PIPELINE], "deals by stage")

    labels = [row["label"] for row in analysis["rows"]]
    assert labels == ["Qualification", "Unknown stage", "Unknown stage (mystery)"]
    assert "Unknown stage (mystery)" in text


def test_currency_only_shown_when_hubspot_supplies_a_single_code():
    plan = plan_crm_query("deal totals by stage")
    usd = [
        deal(1, "s-new", 100, deal_currency_code="USD"),
        deal(2, "s-won", 50, deal_currency_code="USD"),
    ]
    _, text = analyze_deals(plan, usd, [PIPELINE], "deal totals by stage")
    assert "USD 100" in text

    none = [deal(1, "s-new", 100), deal(2, "s-won", 50)]
    _, text = analyze_deals(plan, none, [PIPELINE], "deal totals by stage")
    assert "100" in text and "USD" not in text and "INR" not in text and "$" not in text

    mixed = [
        deal(1, "s-new", 100, deal_currency_code="USD"),
        deal(2, "s-won", 50, deal_currency_code="INR"),
    ]
    _, text = analyze_deals(plan, mixed, [PIPELINE], "deal totals by stage")
    assert "not converted" in text and "USD 100" not in text
    assert "Amount totals and averages were omitted" in text
    assert "Total 150" not in text


def test_stage_comparison_limits_rows_and_gives_insight():
    plan = plan_crm_query("compare Closed Won vs Closed Lost deal stage")
    analysis, text = analyze_deals(
        plan, twenty_six_deals(), [PIPELINE], "compare Closed Won vs Closed Lost deal stage"
    )

    assert [row["label"] for row in analysis["rows"]] == ["Closed Won", "Closed Lost"]
    assert "Stage comparison" in text
    assert "Closed Won has 4 more deals than Closed Lost" in text


def test_stage_state_filters_use_pipeline_metadata():
    plan = plan_crm_query("how many closed won deals by stage")
    analysis, _ = analyze_deals(
        plan, twenty_six_deals(), [PIPELINE], "how many closed won deals by stage"
    )
    assert [row["label"] for row in analysis["rows"]] == ["Closed Won"]
    assert analysis["rows"][0]["count"] == 7

    plan = plan_crm_query("how many open deals by stage")
    analysis, _ = analyze_deals(
        plan, twenty_six_deals(), [PIPELINE], "how many open deals by stage"
    )
    assert [row["label"] for row in analysis["rows"]] == ["Qualification", "Proposal"]


@pytest.mark.asyncio
async def test_latest_deals_are_sorted_and_limited_only_for_listing():
    deals = [deal(i, "s-new", 10 * i, closedate=f"2026-01-{i:02d}") for i in range(1, 11)]
    agent, _ = make_agent(deals)

    result = await agent.respond(ask("Show me the latest 3 deals"))

    assert [row["dealname"] for row in result.result["rows"]] == ["Deal 10", "Deal 9", "Deal 8"]
    assert result.result["summary"]["has_more"] is True
    assert "showing 3 of 10 deals" in result.text
    assert_clean(result.text)


@pytest.mark.asyncio
async def test_unsupported_company_and_contact_intelligence_get_clear_answer():
    agent, repo = make_agent(twenty_six_deals())

    for message, noun in (
        ("Show contacts grouped by lifecycle stage", "contacts"),
        ("Give me a company breakdown by industry", "companies"),
    ):
        result = await agent.respond(ask(message))
        assert result.status == "ok"
        assert f"can't run analytics on {noun}" in result.text
        assert result.result["kind"] == "crm_intelligence_unsupported"
    assert repo.calls == 0


@pytest.mark.asyncio
async def test_grouping_by_owner_and_calendar_periods_is_supported():
    records = [
        deal(1, "s-new", 10, hubspot_owner_id="11", closedate="2026-01-15"),
        deal(2, "s-won", 20, hubspot_owner_id="11", closedate="2026-04-15"),
        deal(3, "s-new", 30, hubspot_owner_id="22", closedate="2027-01-15"),
        deal(4, "s-new", 40, hubspot_owner_id=None, closedate=None),
    ]
    owner_plan = plan_crm_query("Show deals grouped by owner")
    owner_plan.aggregations = [CRMIntelligenceAggregation(operation="count", field="dealname")]
    owner_rows, _ = analyze_deals(owner_plan, records, [PIPELINE], "Show deals grouped by owner")
    assert {row["label"]: row["count"] for row in owner_rows["rows"]} == {
        "11": 2,
        "22": 1,
        "Unassigned owner": 1,
    }
    for period in ("month", "quarter", "year"):
        plan = CRMQueryPlan(
            object="deal",
            group_by=period,
            aggregations=[CRMIntelligenceAggregation(operation="count", field="dealname")],
        )
        result, _ = analyze_deals(plan, records, [PIPELINE], f"deals by {period}")
        assert result["rows"]
        assert sum(row["count"] for row in result["rows"]) == 4


def test_unsupported_message_helper():
    assert (
        unsupported_intelligence_message(CRMQueryPlan(object="deal", group_by="dealstage")) is None
    )
    assert unsupported_intelligence_message(CRMQueryPlan(object="contact")) is not None


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("this quarter", ("2026-10-01", "2027-01-01")),
        ("last quarter", ("2026-07-01", "2026-10-01")),
        ("q1 2026", ("2026-01-01", "2026-04-01")),
        ("last month", ("2026-09-01", "2026-10-01")),
        ("last year", ("2025-01-01", "2026-01-01")),
    ],
)
def test_relative_date_ranges(relative, expected):
    assert _relative_date_range(relative, date(2026, 10, 17)) == expected
    assert _relative_date_range("last quarter", date(2026, 1, 5)) == ("2025-10-01", "2026-01-01")


def test_total_deals_is_a_count_and_next_relative_days_are_bounded():
    plan = plan_crm_query("What is the total number of deals?")
    assert [item.operation for item in plan.aggregations] == ["count"]
    start = datetime.now(UTC).date()
    next_days = plan_crm_query("Show deals closing in the next 7 days").date_range
    assert next_days is not None
    assert date.fromisoformat(next_days.start) == start
    assert date.fromisoformat(next_days.end) == start + timedelta(days=7)


@pytest.mark.parametrize(
    ("query", "field", "start", "end"),
    [
        ("deals closing before 2026-05-10", "closedate", None, "2026-05-10"),
        ("deals closing after 2026-05-10", "closedate", "2026-05-11", None),
        ("deals created since 2026-05-10", "createdate", "2026-05-10", None),
        ("deals created yesterday", "createdate", "dynamic", "dynamic"),
        ("deals closing today", "closedate", "dynamic", "dynamic"),
    ],
)
def test_date_query_phrases_select_date_field_and_parse_ranges(query, field, start, end):
    parsed = plan_crm_query(query).date_range
    assert parsed is not None
    assert parsed.field == field
    if start == "dynamic":
        assert parsed.relative in {"yesterday", "today"}
        assert date.fromisoformat(parsed.end) - date.fromisoformat(parsed.start) == timedelta(
            days=1
        )
    else:
        assert parsed.start == start
        assert parsed.end == end


def test_date_parser_does_not_invent_a_range_for_ambiguous_phrase():
    assert plan_crm_query("Show me deals around the end of the year").date_range is None


def test_timezone_boundaries_use_requested_local_date_for_hubspot_timestamps():
    plan = CRMQueryPlan(
        object="deal",
        date_range=CRMIntelligenceDateRange(
            start="2025-12-31", end="2026-01-01", timezone="America/Los_Angeles"
        ),
        limit=20,
    )
    near_midnight_utc = datetime(2026, 1, 1, 1, 0, tzinfo=UTC).timestamp() * 1000
    rows, _ = analyze_deals(
        plan,
        [
            deal(1, "s-new", 10, closedate=str(int(near_midnight_utc))),
            deal(2, "s-new", 20, closedate="2026-01-01T09:00:00Z"),
        ],
        [PIPELINE],
        "deals closing Dec 31 in America/Los_Angeles",
    )
    assert [row["id"] for row in rows["rows"]] == ["1"]


def test_amount_name_stage_pipeline_owner_and_createdate_filters_combine():
    query = (
        'Show deal named "Northwind Renewal" stage is Proposal pipeline is Sales owner is 11 '
        "with amount over $500 created in 2026"
    )
    plan = plan_crm_query(query)
    records = [
        deal(
            1,
            "s-prop",
            900,
            dealname="Northwind Renewal",
            pipeline="default",
            hubspot_owner_id="11",
            createdate="2026-02-01",
        ),
        deal(
            2,
            "s-new",
            900,
            dealname="Northwind Renewal",
            pipeline="default",
            hubspot_owner_id="11",
            createdate="2026-02-01",
        ),
        deal(
            3,
            "s-prop",
            100,
            dealname="Northwind Renewal",
            pipeline="default",
            hubspot_owner_id="11",
            createdate="2026-02-01",
        ),
    ]
    # Pipeline label aliases are resolved by the pipeline ID map during filtering.
    resolver = _StageResolver([PIPELINE])
    matched = [
        item
        for item in records
        if all(
            _filter_matches(item, condition, resolver, [PIPELINE], "UTC")
            for condition in plan.filters
        )
        and plan.date_range is not None
        and plan.date_range.start <= item.properties["createdate"] < plan.date_range.end
    ]
    assert [item.id for item in matched] == ["1"]


@pytest.mark.asyncio
async def test_latest_oldest_highest_lowest_and_page_offset_are_deterministic():
    deals = [
        deal(1, "s-new", 10, closedate="2026-01-01"),
        deal(2, "s-new", 50, closedate="2026-01-02"),
        deal(3, "s-new", 30, closedate="2026-01-03"),
    ]
    for query, expected in (
        ("Show oldest 2 deals", ["1", "2"]),
        ("Show highest 2 deals by amount", ["2", "3"]),
        ("Show lowest 2 deals by amount", ["1", "3"]),
    ):
        agent, _ = make_agent(deals)
        result = await agent.respond(ask(query))
        assert [row["id"] for row in result.result["rows"]] == expected
    agent, _ = make_agent(deals)
    page = await agent.respond(ask("Show latest 1 deals page 2"))
    assert [row["id"] for row in page.result["rows"]] == ["2"]


@pytest.mark.asyncio
async def test_deal_listing_table_contains_live_hubspot_record_links():
    agent, _ = make_agent([deal(1, "s-new", 10)])

    result = await agent.respond(ask("Show me the latest 1 HubSpot deal"))

    assert result.result["table"]["columns"][-1] == {
        "key": "view_url",
        "label": "View in HubSpot",
    }
    assert result.result["table"]["rows"][0]["view_url"] == (
        "https://app.hubspot.com/contacts/portal-123/record/0-3/1"
    )


@pytest.mark.asyncio
async def test_follow_up_date_question_reuses_only_prior_query_context():
    from app.agent.crm_intelligence import CRMIntelligencePlanner

    planner = CRMIntelligencePlanner()
    plan = await planner.plan("what about last quarter?", "Show me closed won HubSpot deals")
    assert plan.date_range is not None
    assert plan.date_range.relative == "last quarter"
    assert any(item.field == "dealstage" for item in plan.filters)


def test_planner_aggregations_distinguish_count_sum_and_extremes():
    def ops(text):
        return [a.operation for a in plan_crm_query(text).aggregations]

    assert ops("How many deals are in each deal stage?") == ["count"]
    assert ops("deal counts and total amounts by stage") == ["sum", "count"]
    assert ops("total number of deals by stage") == ["count"]
    assert ops("average deal amount") == ["avg"]
    assert "min" in ops("minimum deal amount") and "max" in ops("largest deal")
    assert plan_crm_query("How many deals are in each deal stage?").group_by == "dealstage"


@pytest.mark.asyncio
async def test_exact_live_count_average_and_total_stage_questions_return_correct_metrics():
    cases = [
        ("How many HubSpot deals are in each deal stage?", "count", 10),
        ("What is the average deal amount in each deal stage?", "avg_amount", "1,000"),
        ("What is the total deal amount in each deal stage?", "sum_amount", "10,000"),
    ]
    for message, field, expected in cases:
        agent, _ = make_agent(twenty_six_deals(), query="all_deals")
        result = await agent.respond(ask(message))
        assert result.result["kind"] == "crm_intelligence"
        row = by_label(result)["Qualification"]
        assert row[field] == expected


@pytest.mark.asyncio
async def test_overall_deal_count_is_correct():
    agent, _ = make_agent(twenty_six_deals())
    result = await agent.respond(ask("How many HubSpot deals do we have?"))

    assert result.result["kind"] == "crm_intelligence"
    assert result.result["summary"]["matched_records"] == 26
    assert result.result["rows"][0]["count"] == 26


def test_aggregation_model_still_accepts_plain_operations():
    assert CRMIntelligenceAggregation(operation="max", field="amount").operation == "max"
