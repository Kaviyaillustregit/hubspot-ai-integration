from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agent.schemas import AgentRequest, CRMIntentExtraction, DealAnalysis
from app.agent.service import AccountIntelligenceAgent, _hubspot_date
from app.agent.tools import DEAL_ANALYSIS_PROPERTIES
from app.ai.service import AIService
from app.integrations.hubspot.models import (
    HubSpotDeal,
    HubSpotPipeline,
    HubSpotPipelineStage,
)


class ScriptedProvider:
    def __init__(self, *, closed: bool = False, email_draft: str | None = None) -> None:
        self.analysis_variables: dict[str, object] | None = None
        self.analysis_calls = 0
        self.closed = closed
        self.email_draft = email_draft

    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(
                intent="crm_question",
                query="deal_analysis",
                deal_name="Renewal",
                confidence=0.95,
            )
        assert prompt_name == "deal-analysis/v1"
        assert output_schema is DealAnalysis
        self.analysis_calls += 1
        self.analysis_variables = variables
        return DealAnalysis(
            summary="The deal needs timely attention.",
            identified_issues=["Recent activity is limited."],
            recommendations=["Confirm the decision process.", "Schedule a next-step meeting."],
            follow_up_email_draft=self.email_draft,
        )


class FakeDealTools:
    def __init__(
        self,
        *,
        closed: bool = False,
        activity: str | None = "1791873840000",
        deal_status: str = "found",
    ) -> None:
        self.deal_status = deal_status
        self.deal = HubSpotDeal(
            id="deal-1",
            properties={
                "dealname": "Renewal",
                "amount": "50000",
                "dealstage": "stage-1",
                "pipeline": "pipeline-1",
                "closedate": (datetime.now(UTC).date() + timedelta(days=7)).isoformat(),
                "hubspot_owner_id": "owner-9",
                "notes_last_updated": activity,
            },
        )
        self.pipeline = HubSpotPipeline(
            id="pipeline-1",
            label="Sales",
            stages=[
                HubSpotPipelineStage(
                    id="stage-1",
                    label="Qualification",
                    metadata={"isClosed": "true" if closed else "false"},
                )
            ],
        )

    async def resolve_deal(self, tenant_id: str, name: str):
        return SimpleNamespace(
            status=self.deal_status,
            deal=self.deal if self.deal_status == "found" else None,
        )

    async def get_deal(self, tenant_id: str, deal_id: str, *, properties):
        self.requested_deal_properties = properties
        return self.deal

    async def deal_pipelines(self, tenant_id: str):
        return [self.pipeline]

    async def hubspot_record_urls(self, tenant_id: str, records):
        return ["https://app.hubspot.com/contacts/42/record/0-3/deal-1"]


def request(
    message: str = "Analyze the deal Renewal and suggest next best actions to close it.",
) -> AgentRequest:
    return AgentRequest(
        tenant_id="tenant-a",
        actor_id="user-1",
        message=message,
        request_id="request-1",
    )


def agent(provider: ScriptedProvider, tools: FakeDealTools) -> AccountIntelligenceAgent:
    return AccountIntelligenceAgent(
        tools,  # type: ignore[arg-type]
        AIService(provider),
        object(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_deal_analysis_uses_verified_facts_and_returns_recommendations():
    provider = ScriptedProvider(email_draft="Unrequested email draft")
    tools = FakeDealTools()
    response = await agent(provider, tools).respond(request())

    assert response.status == "ok"
    assert response.result is not None
    facts = provider.analysis_variables["crm_facts"]  # type: ignore[index]
    assert facts["deal_name"] == "Renewal"  # type: ignore[index]
    assert facts["pipeline"] == "Sales"  # type: ignore[index]
    assert facts["stage"] == "Qualification"  # type: ignore[index]
    assert facts["owner_id"] == "owner-9"  # type: ignore[index]
    assert facts["last_activity_date"] == "2026-10-13"  # type: ignore[index]
    assert facts["inactivity_days"] == max(
        (datetime.now(UTC).date() - date(2026, 10, 13)).days, 0
    )  # type: ignore[index]
    assert provider.analysis_variables["requested_email_draft"] is False
    assert response.result["follow_up_email_draft"] is None
    assert "Unrequested email draft" not in response.text
    assert "Last activity date: 2026-10-13" in response.text
    assert "Inactive days: 0" in response.text
    assert "Activity status: Active today" in response.text
    assert "notes_last_updated" in tools.requested_deal_properties
    assert "hs_lastactivitydate" not in tools.requested_deal_properties
    assert "Next best actions" in response.text
    assert "record/0-3/deal-1" in response.text


@pytest.mark.asyncio
async def test_deal_analysis_calculates_overdue_close_date_deterministically():
    provider = ScriptedProvider()
    tools = FakeDealTools()
    tools.deal.properties["closedate"] = (
        datetime.now(UTC).date() - timedelta(days=4)
    ).isoformat()
    response = await agent(provider, tools).respond(request())

    assert response.status == "ok"
    assert provider.analysis_variables is not None
    facts = provider.analysis_variables["crm_facts"]
    assert facts["close_date_overdue"] is True  # type: ignore[index]
    assert facts["days_past_close_date"] == 4  # type: ignore[index]


@pytest.mark.asyncio
async def test_deal_analysis_reports_unknown_activity_and_only_includes_requested_draft():
    provider = ScriptedProvider(email_draft="Hello, could we schedule time to discuss next steps?")
    tools = FakeDealTools(activity=None)
    message = "Analyze the deal Renewal and draft a follow-up email."
    response = await agent(provider, tools).respond(request(message))

    assert response.status == "ok"
    assert provider.analysis_variables is not None
    assert provider.analysis_variables["requested_email_draft"] is True
    facts = provider.analysis_variables["crm_facts"]
    assert facts["inactivity_days"] is None  # type: ignore[index]
    assert "last activity date" in facts["missing_fields"]  # type: ignore[index]
    assert "Last activity date: Unknown" in response.text
    assert "Inactive days: Unknown" in response.text
    assert "Activity status: Unknown (no activity date in HubSpot)" in response.text
    assert "Unknown (Unknown" not in response.text
    assert "Follow-up email draft" in response.text


@pytest.mark.asyncio
async def test_closed_deal_is_excluded_from_closing_recommendations():
    provider = ScriptedProvider()
    response = await agent(provider, FakeDealTools(closed=True)).respond(request())

    assert response.status == "closed_deal"
    assert provider.analysis_calls == 0


@pytest.mark.asyncio
async def test_ambiguous_deal_is_not_analyzed():
    provider = ScriptedProvider()
    response = await agent(
        provider, FakeDealTools(deal_status="ambiguous")
    ).respond(request())

    assert response.status == "ambiguous"
    assert provider.analysis_calls == 0


def test_deal_analysis_read_requests_last_activity_date():
    assert "notes_last_updated" in DEAL_ANALYSIS_PROPERTIES
    assert "hs_lastactivitydate" not in DEAL_ANALYSIS_PROPERTIES


@pytest.mark.parametrize(
    "timestamp",
    [
        "1791873840000",
        "2026-10-13T06:44:00.000Z",
        "2026-10-13T12:44:00+05:30",
    ],
)
def test_hubspot_last_activity_timestamp_formats_parse_to_date(timestamp: str):
    assert _hubspot_date(timestamp) == date(2026, 10, 13)
