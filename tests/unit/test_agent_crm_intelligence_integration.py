import pytest

from app.agent.crm_intelligence import plan_crm_query
from app.agent.schemas import AgentRequest, CRMIntentExtraction, GroundedSummary
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.provider import AIProvider
from app.ai.service import AIService
from app.api.slack import _respond
from app.integrations.errors import (
    IntegrationError,
    IntegrationRateLimitError,
    IntegrationTimeoutError,
)
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotCompany,
    HubSpotContactsPage,
    HubSpotDeal,
    HubSpotDealsPage,
    HubSpotPipeline,
    HubSpotPipelineStage,
)


class CRMRouteProvider(AIProvider):
    def __init__(self, query=None):
        self.query = query

    async def generate_structured(self, *, prompt_name: str, variables: dict, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(
                intent="crm_question",
                confidence=1.0,
                question=variables["message"],
                query=self.query,
            )
        if prompt_name == "crm-intelligence/v1":
            return plan_crm_query(variables["query"])
        if prompt_name == "account-intelligence/v1":
            company = variables["crm"]["company"]
            return GroundedSummary(
                crm_facts=[f"Company: {company.get('properties', {}).get('name', 'Unknown')}"] ,
                observations=["Mock company summary"]
            )
        raise AssertionError(f"Unexpected prompt: {prompt_name}")


class Companies:
    async def list_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=[])

    async def search_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=[])

    async def get_contact_company_associations(self, context, contact_id):
        return type("Associations", (), {"results": []})()


class Contacts:
    async def get_hubspot_account_id(self, context):
        return "42"

    async def list_contacts(self, context, **kwargs):
        return HubSpotContactsPage(results=[])

    async def search_contacts(self, context, **kwargs):
        return HubSpotContactsPage(results=[])


class Deals:
    def __init__(self, results: list[HubSpotDeal] | None = None):
        self._results = results or []

    async def list_deals(self, context, **kwargs):
        return HubSpotDealsPage(results=self._results)

    async def get_deal_type_options(self, context):
        return []

    async def list_pipelines(self, context):
        return [
            HubSpotPipeline(
                id="sales",
                label="Sales",
                stages=[
                    HubSpotPipelineStage(id="qualification", label="Qualification", metadata={}),
                    HubSpotPipelineStage(id="won", label="Closed Won", metadata={}),
                    HubSpotPipelineStage(id="lost", label="Closed Lost", metadata={}),
                ],
            )
        ]


class FailingDeals(Deals):
    async def list_deals(self, context, **kwargs):
        raise IntegrationError("deal service failure")


def request(message: str) -> AgentRequest:
    return AgentRequest(
        tenant_id="tenant-a",
        actor_id="user-a",
        message=message,
        request_id="req-intelligence",
    )


@pytest.mark.asyncio
async def test_agent_routes_group_by_stage_query_through_intelligence_layer():
    deals = [
        HubSpotDeal(
            id="deal-1",
            properties={"dealname": "Alpha", "dealstage": "qualification", "amount": "50000", "closedate": "2026-01-02"},  # noqa: E501
        ),
        HubSpotDeal(
            id="deal-2",
            properties={"dealname": "Beta", "dealstage": "won", "amount": "150000", "closedate": "2026-01-10"},  # noqa: E501
        ),
    ]
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts(), Deals(deals)),
        AIService(CRMRouteProvider()),
        object(),
    )

    result = await agent.respond(request("show me all the latest deal groupby stagename"))

    assert result.status == "ok"
    assert result.tools_used == ["list_all_deals", "deal_pipelines"]
    assert result.result["plan"]["group_by"] == "dealstage"
    assert result.result["summary"]["returned_records"] == 2
    assert {row["label"] for row in result.result["rows"]} == {"Qualification", "Closed Won"}
    assert len(result.result["details"]) == 2
    assert "Open in HubSpot" in result.text


@pytest.mark.asyncio
async def test_agent_routes_quarterly_pipeline_summary_and_preserves_chart_contract():
    deals = [
        HubSpotDeal(
            id="deal-1",
            properties={"dealname": "Alpha", "dealstage": "qualification", "amount": "50000", "closedate": "2026-10-05"},  # noqa: E501
        ),
        HubSpotDeal(
            id="deal-2",
            properties={"dealname": "Beta", "dealstage": "won", "amount": "150000", "closedate": "2026-10-15"},  # noqa: E501
        ),
        HubSpotDeal(
            id="deal-3",
            properties={"dealname": "Gamma", "dealstage": "lost", "amount": "20000", "closedate": "2025-10-15"},  # noqa: E501
        ),
    ]
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts(), Deals(deals)),
        AIService(CRMRouteProvider()),
        object(),
    )

    result = await agent.respond(request("Give me summary of my pipeline for this quatar"))

    assert result.status == "ok"
    assert [row["stage"] for row in result.result["rows"]] == ["Qualification", "Closed Won"]
    assert result.result["chart"]["type"] == "bar"
    assert result.result["chart_type"] == "funnel"


@pytest.mark.asyncio
async def test_quarterly_status_summary_reports_open_won_lost_and_total_from_records():
    deals = [
        HubSpotDeal(
            id="open-1",
            properties={
                "dealname": "Open One", "dealstage": "qualification", "amount": "100",
                "closedate": "2026-10-05",
            },
        ),
        HubSpotDeal(
            id="won-1",
            properties={
                "dealname": "Won One", "dealstage": "won", "amount": "200",
                "closedate": "2026-10-10",
            },
        ),
        HubSpotDeal(
            id="lost-1",
            properties={
                "dealname": "Lost One", "dealstage": "lost", "amount": "300",
                "closedate": "2026-10-15",
            },
        ),
    ]
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts(), Deals(deals)),
        AIService(CRMRouteProvider()), object(),
    )

    result = await agent.respond(request(
        "Give me a summary of our sales pipeline for this quarter, including "
        "how many deals are open, won, and lost."
    ))

    assert result.status == "ok"
    assert "Pipeline status summary" in result.text
    assert "Open: 1 deal; value 100" in result.text
    assert "Won: 1 deal; value 200" in result.text
    assert "Lost: 1 deal; value 300" in result.text
    assert "Total: 3 deals; value 600" in result.text


@pytest.mark.asyncio
async def test_agent_returns_empty_result_when_no_deals_match_the_plan():
    deals = [
        HubSpotDeal(
            id="deal-1",
            properties={"dealname": "Old", "dealstage": "qualification", "amount": "50000", "closedate": "2025-01-01"},  # noqa: E501
        )
    ]
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts(), Deals(deals)),
        AIService(CRMRouteProvider()),
        object(),
    )

    result = await agent.respond(request("Give me summary of my pipeline for this quatar"))

    assert result.status == "ok"
    assert result.result["rows"] == []
    assert result.result["summary"]["total_records"] == 1


@pytest.mark.asyncio
async def test_overdue_deal_request_returns_only_open_past_due_records_with_links():
    deals = [
        HubSpotDeal(
            id="past-due",
            properties={
                "dealname": "Past Due", "dealstage": "qualification", "amount": "5000",
                "closedate": "2026-10-10",
            },
        ),
        HubSpotDeal(
            id="future",
            properties={
                "dealname": "Future", "dealstage": "qualification", "amount": "7000",
                "closedate": "2026-10-25",
            },
        ),
        HubSpotDeal(
            id="won",
            properties={
                "dealname": "Won", "dealstage": "won", "amount": "9000",
                "closedate": "2026-10-10",
            },
        ),
    ]
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts(), Deals(deals)),
        AIService(CRMRouteProvider()), object(),
    )

    result = await agent.respond(request("Which of my deals are overdue?"))

    assert result.status == "ok"
    assert [row["id"] for row in result.result["rows"]] == ["past-due"]
    assert "Past Due" in result.text
    assert "Closed Won" not in result.text
    assert "record/0-3/past-due" in result.text


@pytest.mark.asyncio
async def test_top_five_deal_chart_has_sorted_amounts_and_clickable_record_links():
    deals = [
        HubSpotDeal(
            id=f"deal-{amount}",
            properties={
                "dealname": f"Deal {amount}", "dealstage": "qualification",
                "amount": str(amount), "closedate": f"2026-10-{amount:02d}",
            },
        )
        for amount in (10, 50, 30, 20, 40, 5)
    ]
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts(), Deals(deals)),
        AIService(CRMRouteProvider()), object(),
    )

    result = await agent.respond(request("Show the top five deals by amount in a bar chart"))

    assert result.status == "ok"
    assert result.result["chart"]["type"] == "bar"
    assert result.result["chart"]["data"]["labels"] == [
        "Deal 50", "Deal 40", "Deal 30", "Deal 20", "Deal 10"
    ]
    assert result.result["chart"]["data"]["datasets"][0]["data"] == [50, 40, 30, 20, 10]
    assert "Chart shown below" in result.text
    assert "<https://app.hubspot.com/contacts/42/record/0-3/deal-50|Open>" in result.text


@pytest.mark.asyncio
async def test_agent_returns_safe_response_on_hubspot_data_failure():
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts(), FailingDeals()),
        AIService(CRMRouteProvider()),
        object(),
    )

    result = await agent.respond(request("show me all the latest deal groupby stagename"))

    assert result.status == "unavailable"
    assert "couldn't retrieve the CRM summary" in result.text


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_phrase"),
    [
        (IntegrationRateLimitError("provider details"), "hubspot_rate_limited", "rate limiting"),
        (IntegrationTimeoutError("provider details"), "hubspot_timeout", "did not respond"),
    ],
)
async def test_stage1_reports_actionable_hubspot_transient_failures(
    failure, expected_status, expected_phrase
):
    class TransientlyFailingDeals(Deals):
        async def list_deals(self, context, **kwargs):
            raise failure

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts(), TransientlyFailingDeals()),
        AIService(CRMRouteProvider()),
        object(),
    )

    result = await agent.respond(request("show me all deals"))

    assert result.status == expected_status
    assert expected_phrase in result.text
    assert "provider details" not in result.text


@pytest.mark.asyncio
async def test_agent_ignores_unsupported_queries_and_keeps_legacy_route():
    class CompanyMatches(Companies):
        async def search_companies(self, context, **kwargs):
            return HubSpotCompaniesPage(
                results=[HubSpotCompany(id="company-1", properties={"name": "Example Company"})]
            )

        async def list_companies(self, context, **kwargs):
            return HubSpotCompaniesPage(
                results=[HubSpotCompany(id="company-1", properties={"name": "Example Company"})]
            )

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(CompanyMatches(), Contacts(), Deals()),
        AIService(CRMRouteProvider()),
        object(),
    )

    result = await agent.respond(request("Tell me about Example Company"))

    assert result.status == "ok"
    assert result.tools_used == ["resolve_company"]
    assert "Example Company" in result.text


@pytest.mark.asyncio
async def test_stage1_result_is_delivered_to_slack_with_tenant_scoping_and_real_newlines():
    calls = {"read": 0, "mutations": 0}

    class ScopedDeals(Deals):
        async def list_deals(self, context, **kwargs):
            assert context.tenant_id == "tenant-a"
            calls["read"] += 1
            return HubSpotDealsPage(
                results=[
                    HubSpotDeal(
                        id="deal-q3",
                        properties={
                            "dealname": "Quarter End Renewal",
                            "dealstage": "qualification",
                            "amount": "12000",
                            "closedate": "2026-09-30",
                        },
                    ),
                    HubSpotDeal(
                        id="deal-q4",
                        properties={
                            "dealname": "October Expansion",
                            "dealstage": "qualification",
                            "amount": "25000",
                            "closedate": "2026-10-01",
                        },
                    ),
                ]
            )

        async def create_deal(self, context, **kwargs):
            calls["mutations"] += 1
            raise AssertionError("Stage 1 must be read-only")

        async def update_deal(self, context, **kwargs):
            calls["mutations"] += 1
            raise AssertionError("Stage 1 must be read-only")

    class SlackClient:
        async def post_message(self, channel, text, **kwargs):
            calls["channel"] = channel
            calls["text"] = text

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts(), ScopedDeals()),
        AIService(CRMRouteProvider(query="all_deals")),
        object(),
    )

    await _respond(
        agent,
        SlackClient(),
        {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "C1",
                "text": "Show me all HubSpot deals closing in Q3 2026.",
            },
        },
        "tenant-a",
        "req-slack-q3",
    )

    assert calls["read"] == 1
    assert calls["mutations"] == 0
    assert calls["channel"] == "C1"
    assert "Quarter End Renewal" in calls["text"]
    assert "October Expansion" not in calls["text"]
    assert "\\n" not in calls["text"]
    assert "\n" in calls["text"]
    assert "access_token" not in calls["text"].lower()


@pytest.mark.asyncio
async def test_stage1_authorization_failure_returns_safe_response_without_leaking_error():
    class UnauthorizedDeals(Deals):
        async def list_deals(self, context, **kwargs):
            assert context.tenant_id == "tenant-a"
            raise ValueError("secret access_token should not be exposed")

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(Companies(), Contacts(), UnauthorizedDeals()),
        AIService(CRMRouteProvider()),
        object(),
    )

    result = await agent.respond(
        request("How many HubSpot deals are in each deal stage?")
    )

    assert result.status == "hubspot_not_authorized"
    assert "HubSpot is not connected" in result.text
    assert "secret" not in result.text and "access_token" not in result.text
