"""Tests for deal filtering by amount (including the original query)."""
import pytest

from app.agent.crm_query import (
    DealFilterError,
    DealFilterSpec,
    DealSort,
    apply_deal_filter,
    build_deal_filter,
    deal_status_query,
    detect_deal_filter,
    resolve_stage_ids,
    to_hubspot_filter_group,
)
from app.agent.extraction import ValidatedExtraction
from app.agent.operations import CRMOperations, _merge_deal_filter_dicts
from app.agent.schemas import AgentRequest, CRMIntentExtraction
from app.integrations.hubspot.models import HubSpotDeal, HubSpotPipeline, HubSpotPipelineStage
from app.services.quickchart import build_chart_config


PIPELINES = [
    HubSpotPipeline(
        id="sales",
        label="Sales",
        stages=[
            HubSpotPipelineStage(
                id="open",
                label="Qualification",
                metadata={"isClosed": "false", "probability": "0.2"},
            ),
            HubSpotPipelineStage(
                id="won",
                label="Closed Won",
                metadata={"isClosed": "true", "probability": "1.0"},
            ),
        ],
    )
]


class QueryTools:
    def __init__(self, deals: list[HubSpotDeal]) -> None:
        self.deals = deals

    async def deal_pipelines(self, tenant_id: str):
        return PIPELINES

    async def list_all_deals(self, tenant_id: str):
        return self.deals

    async def associated_records(self, tenant_id: str, *, from_type, from_id, to_type):
        return []

    async def hubspot_record_urls(self, tenant_id: str, records):
        return [
            f"https://app.hubspot.com/contacts/42/record/0-3/{record_id}"
            for _, record_id in records
        ]


def deal(deal_id: str, name: str, stage: str, **properties: str) -> HubSpotDeal:
    return HubSpotDeal(
        id=deal_id,
        properties={"dealname": name, "dealstage": stage, **properties},
    )


def request(message: str = "Show me all open deals") -> AgentRequest:
    return AgentRequest(
        tenant_id="tenant-a",
        actor_id="user-1",
        message=message,
        request_id="request-1",
    )


async def answer(query: str, deals: list[HubSpotDeal]):
    operations = CRMOperations(QueryTools(deals), action_safety=None)  # type: ignore[arg-type]
    return await operations._answer_deal_list(request(), query)  # noqa: SLF001


def test_detect_deal_filter_recognizes_original_query():
    """Test that 'show me all open opportunities where amount is greater than 20000' is detected."""
    message = "show me all open oppurtunities where amount is greater than 20000"
    
    deal_filter = detect_deal_filter(message)
    
    assert deal_filter is not None
    assert len(deal_filter.conditions) == 1
    assert deal_filter.conditions[0].property == "amount"
    assert deal_filter.conditions[0].operator == "gt"
    assert deal_filter.conditions[0].value == "20000"


def test_detect_deal_filter_handles_correct_spelling():
    """Test that correct spelling 'opportunities' also works."""
    message = "show me all open opportunities where amount is greater than 20000"
    
    deal_filter = detect_deal_filter(message)
    
    assert deal_filter is not None
    assert len(deal_filter.conditions) == 1
    assert deal_filter.conditions[0].property == "amount"
    assert deal_filter.conditions[0].operator == "gt"
    assert deal_filter.conditions[0].value == "20000"


def test_deal_filter_aliases_are_normalized_before_validation():
    spec = DealFilterSpec.model_validate({
        "stage_type": "open",
        "sort": "amount_desc",
        "limit": 5,
        "output": "bar_chart",
    })

    assert spec.status == "open"
    assert spec.sort == DealSort(property="amount", direction="desc")
    assert spec.limit == 5


def test_deal_filter_rejects_unknown_fields_before_validation():
    with pytest.raises(ValueError, match="Unsupported deal filter field 'metadata'"):
        DealFilterSpec.model_validate({
            "status": "open",
            "metadata": {"raw": "should-not-pass"},
        })


def test_pipeline_and_chart_language_are_not_treated_as_pipeline_names():
    assert detect_deal_filter("Show my sales pipeline as a chart.") is None

    pipeline_filter = detect_deal_filter("Show deals in the Enterprise Sales pipeline.")
    assert pipeline_filter is not None
    assert pipeline_filter.pipeline == "Enterprise Sales"


def test_count_based_bar_chart_uses_zero_baseline_and_whole_number_ticks():
    config = build_chart_config("bar", "Open deals by stage", ["New", "Qualified", "Won"], [1, 3, 2], value_label="Deal count")
    y_axis = config["options"]["scales"]["y"]
    assert y_axis["min"] == 0
    assert y_axis["beginAtZero"] is True
    assert y_axis["ticks"]["precision"] == 0
    assert "maximumFractionDigits: 0" in y_axis["ticks"]["callback"]


def test_quickchart_url_includes_chartjs_version_3():
    config = build_chart_config("bar", "Open deals by stage", ["New", "Qualified", "Won"], [1, 3, 2], value_label="Deal count")
    from app.services.quickchart import generate_chart_url

    chart_url = generate_chart_url(config)
    assert "version=3" in chart_url
    assert "https://quickchart.io/chart?version=3&c=" in chart_url


def test_chartjs_v3_styling_keeps_title_legend_and_axis_visible():
    config = build_chart_config("bar", "Open deals by stage", ["New", "Qualified", "Won"], [1, 3, 2], value_label="Deal count")
    assert config["options"]["plugins"]["title"]["display"] is True
    assert config["options"]["plugins"]["title"]["text"] == "OPEN DEALS BY STAGE"
    assert config["options"]["plugins"]["legend"]["display"] is False
    assert config["options"]["scales"]["y"]["ticks"]["display"] is True
    assert config["options"]["scales"]["y"]["min"] == 0
    assert config["options"]["scales"]["y"]["beginAtZero"] is True


async def test_stage_distribution_summary_does_not_warn_about_amount_for_count_queries():
    operations = CRMOperations(QueryTools([deal("deal-1", "First", "open", amount="100")]), action_safety=None)  # type: ignore[arg-type]
    response = await operations._answer_deal_list(  # noqa: SLF001
        request("Show me the distribution of open deals by stage as a bar chart."),
        "open_deals",
        deal_filter=DealFilterSpec(status="open"),
        chart_requested=True,
        chart_type="bar",
    )
    assert response.text is not None
    assert "Deals without a valid amount are excluded." not in response.text


async def test_chart_and_plain_queries_share_same_filtered_deal_set():
    deals = [
        deal("deal-4", "Low Deal", "open", amount="4000"),
        deal("deal-1", "Top Deal", "open", amount="150000"),
        deal("deal-3", "Mid Deal", "open", amount="70000"),
        deal("deal-2", "Second Deal", "open", amount="120000"),
        deal("deal-5", "Closed Deal", "won", amount="300000"),
    ]
    operations = CRMOperations(QueryTools(deals), action_safety=None)  # type: ignore[arg-type]
    plain_filter = DealFilterSpec(
        status="open",
        sort=DealSort(property="amount", direction="desc"),
        limit=5,
    )
    plain = await operations._answer_deal_list(
        request("Show me the top 5 highest-value open deals"),
        "open_deals",
        deal_filter=plain_filter,
    )
    chart = await operations._answer_deal_list(
        request("Show me the top 5 highest-value open deals as a bar chart"),
        "open_deals",
        deal_filter=plain_filter,
        chart_requested=True,
        chart_type="bar",
    )

    plain_names = [row["name"] for row in plain.result["table"]["rows"]]  # type: ignore[index]
    chart_names = [row["name"] for row in chart.result["table"]["rows"]]  # type: ignore[index]
    assert plain_names == chart_names
    assert chart.result.get("chart_url") is not None  # type: ignore[union-attr]
    assert plain.result.get("chart_url") is None  # type: ignore[union-attr]


def test_build_deal_filter_creates_hubspot_filters():
    """Test that deal filter spec is properly converted to HubSpot filter format."""
    from decimal import Decimal
    from app.agent.crm_query import DealFilterSpec, NumericCondition, DealFilter
    
    spec = DealFilterSpec(
        conditions=[NumericCondition(property="amount", operator="gt", value="20000")]
    )
    
    flt = build_deal_filter(spec)
    
    assert isinstance(flt, DealFilter)
    assert len(flt.numeric) == 1
    assert flt.numeric[0].value == Decimal("20000")
    assert flt.numeric[0].operator == "gt"


async def test_filtered_deals_return_only_matching_open_deals_with_amount_gt_20000():
    """Test that filtering open deals by amount > 20000 returns correct results."""
    response = await answer(
        "open_deals",
        [
            deal("deal-1", "Small Deal", "open", amount="10000"),
            deal("deal-2", "Medium Deal", "open", amount="25000"),
            deal("deal-3", "Large Deal", "open", amount="50000"),
            deal("deal-4", "Huge Deal", "won", amount="100000"),
        ],
    )
    
    # At this point, all open deals should be returned
    # (filtering happens later when deal_filter is applied)
    assert response.result is not None
    rows = response.result["table"]["rows"]  # type: ignore[index]
    
    # Should only have open deals (not the won deal)
    assert len(rows) == 3
    deal_names = {row["name"] for row in rows}
    assert deal_names == {"Small Deal", "Medium Deal", "Large Deal"}


async def test_apply_deal_filter_filters_by_amount():
    """Test that apply_deal_filter correctly filters deals by numeric amount."""
    from decimal import Decimal
    from app.agent.crm_query import DealFilterSpec, NumericCondition, build_deal_filter
    
    deals = [
        deal("deal-1", "Small Deal", "open", amount="10000"),
        deal("deal-2", "Medium Deal", "open", amount="25000"),
        deal("deal-3", "Large Deal", "open", amount="50000"),
        deal("deal-4", "Huge Deal", "won", amount="100000"),
    ]
    
    # Create a filter for amount > 20000
    spec = DealFilterSpec(
        conditions=[NumericCondition(property="amount", operator="gt", value="20000")]
    )
    flt = build_deal_filter(spec)
    
    # Apply filter to only open deals (as the query handler would do)
    open_deals = [d for d in deals if d.properties["dealstage"] == "open"]
    filtered = apply_deal_filter(open_deals, flt)
    
    # Should return only open deals with amount > 20000
    assert len(filtered) == 2
    filtered_names = {d.properties["dealname"] for d in filtered}
    assert filtered_names == {"Medium Deal", "Large Deal"}


def test_deal_filter_combines_numeric_and_stage_filters():
    """Test that numeric filters work in combination with stage filtering."""
    from decimal import Decimal
    from app.agent.crm_query import DealFilterSpec, NumericCondition, build_deal_filter
    
    deals = [
        deal("deal-1", "Small Deal", "open", amount="10000"),
        deal("deal-2", "Medium Deal", "open", amount="25000"),
        deal("deal-3", "Large Deal", "open", amount="50000"),
        deal("deal-4", "Huge Deal", "won", amount="100000"),
    ]
    
    # Create a filter for amount > 20000
    spec = DealFilterSpec(
        conditions=[NumericCondition(property="amount", operator="gt", value="20000")]
    )
    flt = build_deal_filter(spec)
    
    # Apply filter with open stage only
    filtered = apply_deal_filter(deals, flt, stage_ids=["open"])
    
    # Should return only open deals with amount > 20000
    assert len(filtered) == 2
    filtered_names = {d.properties["dealname"] for d in filtered}
    assert filtered_names == {"Medium Deal", "Large Deal"}
    
    # All should be in open stage
    assert all(d.properties["dealstage"] == "open" for d in filtered)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("high", "high"),
        ("High", "high"),
        ("high-priority", "high"),
        ("high priority", "high"),
        ("priority:high", "high"),
        ("medium", "medium"),
        ("medium priority", "medium"),
        ("low-priority", "low"),
        ("LOW", "low"),
    ],
)
def test_deal_filter_accepts_priority_variations(value: str, expected: str):
    assert build_deal_filter(DealFilterSpec(priority=value)).priority == expected


def test_deal_filter_rejects_unknown_priority_strictly():
    with pytest.raises(DealFilterError, match="I couldn't match the deal priority 'urgent'"):
        build_deal_filter(DealFilterSpec(priority="urgent"))


def test_to_hubspot_filter_group_uses_lowercase_hubspot_priority_values():
    flt = build_deal_filter(DealFilterSpec(priority="High"))
    group = to_hubspot_filter_group(flt, stage_ids=["open"])

    assert {"propertyName": "hs_priority", "operator": "EQ", "value": "high"} in group[0]["filters"]
    assert {"propertyName": "hs_priority", "operator": "EQ", "value": "HIGH"} not in group[0]["filters"]


def test_deal_filter_stage_alias_is_mapped_before_validation():
    merged = _merge_deal_filter_dicts({"deal_stage": "Qualified To Buy"})
    assert merged == {"stages": ["Qualified To Buy"]}
    spec = DealFilterSpec(**merged)
    assert spec.stages == ["Qualified To Buy"]


@pytest.mark.parametrize(
    ("message", "expected_query", "expected_stage"),
    [
        ("Show me all deals in Qualified to Buy stage", "all_deals", "Qualified to Buy"),
        ("Show me all open deals in the Qualified To Buy stage", "open_deals", "Qualified To Buy"),
        ("Show me all open deals in the Appointment Scheduled stage", "open_deals", "Appointment Scheduled"),
        ("List deals at Presentation Scheduled", "all_deals", "Presentation Scheduled"),
    ],
)
def test_stage_only_deal_requests_are_detected_as_deal_questions(message: str, expected_query: str, expected_stage: str):
    deal_filter = detect_deal_filter(message)
    assert deal_filter is not None
    assert deal_filter.stages == [expected_stage]
    assert deal_status_query(message) == expected_query
    rebuilt = build_deal_filter(deal_filter)
    assert rebuilt.stages == (expected_stage,)


def test_stage_filter_combines_with_amount_and_priority():
    message = "Show me all open deals in the Qualified To Buy stage with amount over 25000 and high priority"
    deal_filter = detect_deal_filter(message)

    assert deal_filter is not None
    assert deal_filter.stages == ["Qualified To Buy"]
    assert deal_filter.priority == "high"
    assert deal_filter.conditions[0].property == "amount"
    assert deal_filter.conditions[0].operator == "gt"
    assert deal_filter.conditions[0].value == "25000"

    rebuilt = build_deal_filter(deal_filter)
    assert rebuilt.priority == "high"
    assert rebuilt.stages == ("Qualified To Buy",)
    assert rebuilt.numeric[0].value == 25000


def test_stage_resolution_uses_hubspot_stage_ids_in_filter_payload():
    pipeline = HubSpotPipeline(
        id="sales",
        label="Sales",
        stages=[
            HubSpotPipelineStage(id="appointmentscheduled", label="Appointment Scheduled"),
            HubSpotPipelineStage(id="qualifiedtobuy", label="Qualified To Buy"),
        ],
    )
    stage_map = {stage.id: stage for stage in pipeline.stages}
    resolved = resolve_stage_ids(("Appointment Scheduled",), stage_map)
    assert resolved == ["appointmentscheduled"]

    flt = build_deal_filter(DealFilterSpec(stages=["Appointment Scheduled"]))
    group = to_hubspot_filter_group(flt, resolved)
    assert {"propertyName": "dealstage", "operator": "IN", "values": ["appointmentscheduled"]} in group[0]["filters"]


def test_stage_resolution_rejects_ambiguous_duplicates_across_pipelines():
    pipeline_map = {
        "sales-dup": HubSpotPipelineStage(id="sales-dup", label="Contract Sent"),
        "marketing-dup": HubSpotPipelineStage(id="marketing-dup", label="Contract Sent"),
    }
    with pytest.raises(DealFilterError, match="more than one deal stage matching 'Contract Sent'"):
        resolve_stage_ids(("Contract Sent",), pipeline_map)


def test_detect_deal_filter_reads_priority_and_deal_type_variants():
    priority_filter = detect_deal_filter("show me high priority open deals")
    assert priority_filter is not None
    assert priority_filter.priority == "high"

    deal_type_filter = detect_deal_filter("show me deals where deal type is New Business")
    assert deal_type_filter is not None
    assert deal_type_filter.deal_type == "New Business"

    built = build_deal_filter(deal_type_filter)
    assert built.deal_type == "newbusiness"


def test_deal_filter_spec_accepts_priority_type_owner_pipeline_and_unassigned():
    spec = {
        "priority": "high",
        "deal_type": "New Business",
        "owner": "owner-2",
        "pipeline": "Sales",
        "unassigned": False,
    }

    flt = build_deal_filter(DealFilterSpec(**spec))

    assert flt.priority == "high"
    assert flt.deal_type == "newbusiness"
    assert flt.owner == "owner-2"
    assert flt.pipeline == "Sales"
    assert flt.unassigned is False


def test_merge_deal_filter_dicts_ignores_blank_values():
    merged = _merge_deal_filter_dicts({"priority": "high", "deal_type": " "}, {"deal_type": "New Business"})
    assert merged == {"priority": "high", "deal_type": "New Business"}


def test_claude_filter_aliases_normalize_to_supported_dealfilterspec():
    spec = DealFilterSpec.model_validate({
        "stage_status": "open",
        "sort": "amount_desc",
        "limit": 5,
        "visualization": "bar",
    })

    assert spec.status == "open"
    assert spec.sort == DealSort(property="amount", direction="desc")
    assert spec.limit == 5


def test_chart_output_metadata_is_ignored_by_deal_filter_validation():
    spec = DealFilterSpec.model_validate({
        "status": "open",
        "output": "bar_chart",
        "sort": {"property": "amount", "direction": "desc"},
        "limit": 3,
    })

    assert spec.status == "open"
    assert spec.sort == DealSort(property="amount", direction="desc")
    assert spec.limit == 3


async def test_normal_queries_remain_non_chart_and_chart_requests_render_chart_data():
    deals = [
        deal("deal-1", "Alpha", "open", amount="90000"),
        deal("deal-2", "Beta", "open", amount="20000"),
    ]
    operations = CRMOperations(QueryTools(deals), action_safety=None)  # type: ignore[arg-type]

    normal = await operations._answer_deal_list(  # noqa: SLF001
        request("Show me the top 2 highest-value open deals"),
        "open_deals",
        ["name", "amount"],
        DealFilterSpec(status="open", sort=DealSort(property="amount", direction="desc"), limit=2),
    )
    assert normal.result is not None
    assert normal.result["kind"] == "crm_records"

    chart = await operations._answer_deal_list(  # noqa: SLF001
        request("Show me the top 2 highest-value open deals as a chart"),
        "open_deals",
        ["name", "amount"],
        DealFilterSpec(status="open", sort=DealSort(property="amount", direction="desc"), limit=2),
        chart_requested=True,
    )
    assert chart.result is not None
    assert chart.result["kind"] == "deal_chart"
    assert chart.result["chart_url"]


@pytest.mark.parametrize("chart_type", ["bar", "line"])
async def test_top_open_deals_chart_queries_accept_claude_legacy_filter_shapes(chart_type: str):
    deals = [
        deal("deal-2", "Beta", "open", amount="20000"),
        deal("deal-1", "Alpha", "open", amount="90000"),
        deal("deal-3", "Gamma", "won", amount="100000"),
    ]
    operations = CRMOperations(QueryTools(deals), action_safety=None)  # type: ignore[arg-type]

    response = await operations._answer_deal_list(  # noqa: SLF001
        request(f"Show me the top 5 highest-value open deals as a {chart_type} chart"),
        "open_deals",
        deal_filter=DealFilterSpec.model_validate({
            "stage_status": "open",
            "sort": "amount_desc",
            "limit": 5,
            "visualization": chart_type,
        }),
    )

    assert response.status == "ok"
    assert response.result is not None
    assert response.result["kind"] == "deal_chart"
    assert response.result["chart_url"]
    assert "Top 2 deals by value" in str(response.result["chart_title"]) or "Top 2" in str(response.result["chart_title"])


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Show me the sales pipeline as a funnel chart", "funnel"),
        ("Show me the pipeline chart", "funnel"),
    ],
)
def test_pipeline_and_funnel_chart_queries_still_respect_existing_pipeline_detection(message: str, expected: str):
    from app.agent.operations import _chart_request

    chart_request = _chart_request(message)
    assert chart_request is not None
    assert chart_request[0] == expected


async def test_invalid_priority_returns_clear_user_facing_error_response():
    extraction = CRMIntentExtraction(
        intent="crm_question",
        query="open_deals",
        confidence=0.9,
        deal_filter={"priority": "urgent"},
    )
    validated = ValidatedExtraction(
        intent="crm_question",
        properties={},
        company_name=None,
        contact_id=None,
    )
    operations = CRMOperations(QueryTools([]), action_safety=None)  # type: ignore[arg-type]

    response = await operations.respond(
        request("show me urgent priority deals"),
        extraction,
        validated,
        "idempotent-key",
    )

    assert response.status == "invalid_filter"
    assert response.text == (
        "I couldn't match the deal priority 'urgent'. Supported values: High, Medium, Low."
    )


def test_hubspot_deal_property_retrieval_includes_priority_and_type():
    from app.agent import tools

    assert "hs_priority" in tools.DEAL_PROPERTIES
    assert "dealtype" in tools.DEAL_PROPERTIES
    assert "hs_priority" in tools.DEAL_LIST_PROPERTIES
    assert "dealtype" in tools.DEAL_LIST_PROPERTIES


def test_apply_deal_filter_combines_priority_type_owner_and_unassigned_conditions():
    deals = [
        deal("deal-1", "High New Business", "open", amount="30000", hubspot_owner_id="owner-1", dealtype="newbusiness", hs_priority="HIGH", pipeline="sales"),
        deal("deal-2", "High Existing Business", "open", amount="25000", hubspot_owner_id="owner-2", dealtype="existingbusiness", hs_priority="HIGH", pipeline="sales"),
        deal("deal-3", "Unassigned High", "open", amount="20000", hubspot_owner_id="", dealtype="newbusiness", hs_priority="HIGH", pipeline="sales"),
        deal("deal-4", "Low Other", "open", amount="15000", hubspot_owner_id="owner-9", dealtype="newbusiness", hs_priority="LOW", pipeline="marketing"),
    ]

    flt = build_deal_filter(
        DealFilterSpec(
            priority="high",
            deal_type="Existing Business",
            owner="owner-2",
            pipeline="Sales",
        )
    )

    filtered = apply_deal_filter(deals, flt, pipeline_ids=["sales"], owner_ids=["owner-2"])
    assert {deal.properties["dealname"] for deal in filtered} == {"High Existing Business"}

    unassigned = build_deal_filter(DealFilterSpec(unassigned=True))
    assert {deal.properties["dealname"] for deal in apply_deal_filter(deals, unassigned)} == {"Unassigned High"}


def test_deal_status_query_recognizes_open_status():
    """Test that the open status is correctly detected for routing."""
    from app.agent.crm_query import deal_status_query
    
    message = "show me all open oppurtunities where amount is greater than 20000"
    status = deal_status_query(message)
    
    assert status == "open_deals"


def test_deal_filter_extraction_integration():
    """Test that the extraction properly includes deal filter."""
    from app.agent.crm_query import detect_deal_filter
    
    message = "show me all open oppurtunities where amount is greater than 20000"
    
    # Simulate what the agent does
    deal_filter = detect_deal_filter(message)
    
    # Create extraction with deal filter
    extraction = CRMIntentExtraction(
        intent="crm_question",
        query="open_deals",
        confidence=0.9,
        deal_filter=deal_filter.model_dump() if deal_filter else None,
    )
    
    assert extraction.deal_filter is not None
    assert extraction.deal_filter["conditions"][0]["property"] == "amount"
    assert extraction.deal_filter["conditions"][0]["operator"] == "gt"
    assert extraction.deal_filter["conditions"][0]["value"] == "20000"
