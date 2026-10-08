import pytest

from app.agent.extraction import validate_extraction
from app.agent.operations import CRMOperations, _Plan
from app.agent.schemas import AgentRequest, CRMIntentExtraction
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.service import AIService
from app.integrations.hubspot.models import (
    HubSpotCompany,
    HubSpotDeal,
    HubSpotDealsPage,
    HubSpotPipeline,
    HubSpotPipelineStage,
    HubSpotRecord,
)

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
            HubSpotPipelineStage(
                id="lost",
                label="Closed Lost",
                metadata={"isClosed": "true", "probability": "0.0"},
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
        return (
            [HubSpotRecord(id="company-1", properties={"name": "KAVIYA Corp"})]
            if from_id == "deal-open"
            else []
        )

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


async def test_open_deals_return_each_open_record_in_a_structured_table():
    response = await answer(
        "open_deals",
        [
            deal(
                "deal-open",
                "Open Renewal",
                "open",
                amount="50000",
                hs_probability="0.7",
                hubspot_owner_id="owner-9",
                closedate="2026-12-30T00:00:00Z",
            ),
            deal("deal-won", "Won", "won"),
        ],
    )

    result = response.result
    assert result is not None
    table = result["table"]
    assert len(table["rows"]) == 1  # type: ignore[index]
    assert table["rows"][0] == {  # type: ignore[index]
        "name": "Open Renewal",
        "company": "KAVIYA Corp",
        "owner": "owner-9",
        "stage": "Qualification",
        "amount": "50,000",
        "probability": "70%",
        "close_date": "2026-12-30",
        "view_url": "https://app.hubspot.com/contacts/42/record/0-3/deal-open",
    }


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("closed_deals", {"Won", "Lost"}),
        ("closed_won_deals", {"Won"}),
        ("closed_lost_deals", {"Lost"}),
    ],
)
async def test_closed_queries_classify_using_pipeline_stage_metadata(query, expected):
    response = await answer(
        query,
        [
            deal("deal-won", "Won", "won"),
            deal("deal-lost", "Lost", "lost"),
            deal("open", "Open", "open"),
        ],
    )

    assert response.result is not None
    assert {row["name"] for row in response.result["table"]["rows"]} == expected  # type: ignore[index]


async def test_best_chance_query_ranks_actual_probability_and_does_not_invent_it():
    response = await answer(
        "best_chance_deals",
        [
            deal("deal-low", "Low", "open", hs_probability="0.25"),
            deal("deal-high", "High", "open", hs_deal_stage_probability="0.9"),
            deal("deal-unknown", "Unknown", "open"),
        ],
    )

    assert response.result is not None
    rows = response.result["table"]["rows"]  # type: ignore[index]
    assert [row["name"] for row in rows] == ["High", "Low", "Unknown"]
    assert [row["probability"] for row in rows] == ["90%", "25%", ""]
    assert "High (90%)" in response.result["message"]


async def test_deal_list_uses_requested_column_order_and_marks_missing_value():
    operations = CRMOperations(QueryTools([deal("deal-1", "Example", "open")]), action_safety=None)  # type: ignore[arg-type]
    response = await operations._answer_deal_list(  # noqa: SLF001
        request(), "open_deals", ["probability", "amount"]
    )

    assert response.result is not None
    table = response.result["table"]  # type: ignore[index]
    assert [column["key"] for column in table["columns"]] == [
        "probability",
        "amount",
        "view_url",
    ]
    assert table["rows"][0]["probability"] == "—"
    assert table["rows"][0]["amount"] == "—"


def test_list_deal_queries_are_selected_by_the_existing_crm_question_route():
    extraction = CRMIntentExtraction(intent="crm_question", query="open_deals", confidence=0.9)

    assert CRMOperations.handles(
        extraction, validate_extraction("Show all open opportunities", extraction)
    )


class PagedDeals:
    def __init__(self, records: list[HubSpotDeal]) -> None:
        self.records = records
        self.cursors: list[str | None] = []

    async def list_deals(self, context, *, limit, after, properties):
        self.cursors.append(after)
        offset = int(after or "0")
        page_records = self.records[offset : offset + limit]
        next_offset = offset + limit
        next_after = str(next_offset) if next_offset < len(self.records) else None
        return HubSpotDealsPage(results=page_records, next_after=next_after)

    async def list_pipelines(self, context):
        return PIPELINES


async def test_registry_follows_every_hubspot_deal_cursor():
    records = [deal(f"deal-{index}", f"Deal {index}", "open") for index in range(205)]
    source = PagedDeals(records)
    tools = HubSpotToolRegistry(None, None, source)  # type: ignore[arg-type]

    listed = await tools.list_all_deals("tenant-a")

    assert len(listed) == 205
    assert source.cursors == [None, "100", "200"]


class MisclassifiedRevenueProvider:
    async def generate_structured(self, **kwargs):
        return CRMIntentExtraction(
            intent="crm_question",
            query="company_details",
            confidence=0.95,
        )


@pytest.mark.parametrize(
    "message",
    [
        "What is our revenue?",
        "How much revenue have we generated?",
        "How much have we won?",
        "What is our closed-won revenue?",
    ],
)
async def test_revenue_questions_route_to_closed_won_amount_total(message):
    tools = QueryTools(
        [
            deal("deal-won-1", "Renewal", "won", amount="50000"),
            deal("deal-won-2", "Expansion", "won", amount="25000"),
            deal("deal-open", "Open Deal", "open", amount="900000"),
            deal("deal-lost", "Lost Deal", "lost", amount="300000"),
        ]
    )
    agent = AccountIntelligenceAgent(
        tools,  # type: ignore[arg-type]
        AIService(MisclassifiedRevenueProvider()),  # type: ignore[arg-type]
        action_safety=None,  # type: ignore[arg-type]
    )

    response = await agent.respond(request(message))

    assert response.status == "ok"
    assert response.result is not None
    assert response.result["total_amount"] == "75,000"
    assert response.result["closed_won_deal_count"] == 2
    assert response.result["deals_with_amount"] == 2
    assert "Renewal: 50,000" in response.text
    assert "Expansion: 25,000" in response.text
    assert "company" not in response.text.lower()


async def test_revenue_total_includes_closed_won_deals_from_every_page():
    deals = [
        deal(f"won-{index}", f"Won {index}", "won", amount="100")
        for index in range(205)
    ]
    source = PagedDeals(deals)

    class ContactPortal:
        async def get_hubspot_account_id(self, context):
            return "42"

    tools = HubSpotToolRegistry(None, ContactPortal(), source)  # type: ignore[arg-type]
    agent = AccountIntelligenceAgent(
        tools,  # type: ignore[arg-type]
        AIService(MisclassifiedRevenueProvider()),  # type: ignore[arg-type]
        action_safety=None,  # type: ignore[arg-type]
    )

    response = await agent.respond(request("How much have we won?"))

    assert response.status == "ok"
    assert response.result is not None
    assert response.result["total_amount"] == "20,500"
    assert response.result["closed_won_deal_count"] == 205
    assert source.cursors == [None, "100", "200"]


async def test_closed_won_revenue_breakdown_links_each_deal_in_hubspot():
    tools = QueryTools(
        [
            deal("deal-won-1", "Renewal", "won", amount="50000"),
            deal("deal-open", "Open Deal", "open", amount="900000"),
            deal("deal-won-2", "Expansion", "won"),
        ]
    )
    agent = AccountIntelligenceAgent(
        tools,  # type: ignore[arg-type]
        AIService(MisclassifiedRevenueProvider()),  # type: ignore[arg-type]
        action_safety=None,  # type: ignore[arg-type]
    )

    response = await agent.respond(request("What is our revenue?"))

    assert response.result is not None
    table = response.result["table"]
    assert table["columns"][-1] == {
        "key": "view_url",
        "label": "View in HubSpot",
    }
    assert table["rows"] == [
        {
            "name": "Renewal",
            "amount": "50,000",
            "view_url": "https://app.hubspot.com/contacts/42/record/0-3/deal-won-1",
        },
        {
            "name": "Expansion",
            "amount": "—",
            "view_url": "https://app.hubspot.com/contacts/42/record/0-3/deal-won-2",
        },
    ]


class AccountCreationTools:
    async def resolve_company(self, tenant_id: str, name: str):
        from app.agent.tools import CompanyResolution

        return CompanyResolution("not_found")

    async def create_company(self, tenant_id: str, properties: dict[str, str]):
        self.properties = properties
        return HubSpotCompany(id="company-1", properties=properties)

    async def hubspot_record_urls(self, tenant_id: str, records):
        return [
            f"https://app.hubspot.com/contacts/42/record/0-2/{record_id}"
            for _, record_id in records
        ]


async def test_account_creation_maps_phone_and_employees_to_hubspot_company():
    message = "Create an account named Testing KAVIYA with phone number 2385 and employees 1000."
    extraction = CRMIntentExtraction(
        intent="create_company",
        company_name="Testing KAVIYA",
        company_phone="2385",
        company_employees="1000",
        confidence=0.9,
    )
    validated = validate_extraction(message, extraction)
    tools = AccountCreationTools()
    operations = CRMOperations(tools, action_safety=None)  # type: ignore[arg-type]
    plan = _Plan()

    await operations._prepare(  # noqa: SLF001
        request(),
        validated,
        {"company": "create", "contact": None, "deal": None},
        plan,
    )
    await operations._execute(request(), validated, [], plan)  # noqa: SLF001

    assert tools.properties == {
        "name": "Testing KAVIYA",
        "phone": "2385",
        "numberofemployees": "1000",
    }
    assert plan.cards[0]["hubspot_id"] == "company-1"
    assert plan.cards[0]["hubspot_url"].endswith("/record/0-2/company-1")
