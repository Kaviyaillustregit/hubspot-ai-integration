from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agent.schemas import AgentRequest, CRMIntentExtraction, DealFollowUpAssessment
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import DEAL_FOLLOW_UP_PROPERTIES
from app.ai.service import AIService
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.models import (
    HubSpotDeal,
    HubSpotPipeline,
    HubSpotPipelineStage,
)


class FollowUpProvider:
    def __init__(
        self,
        *,
        fail: bool = False,
        fail_first: bool = False,
        max_length_output: bool = False,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = fail
        self.fail_first = fail_first
        self.max_length_output = max_length_output

    async def generate_structured(self, *, prompt_name, variables, output_schema):
        assert prompt_name == "deal-follow-up/v1"
        assert output_schema is DealFollowUpAssessment
        self.calls.append(variables)
        if self.fail or (self.fail_first and len(self.calls) == 1):
            raise IntegrationError("AI request failed")
        if self.max_length_output:
            return DealFollowUpAssessment(
                summary="S" * 400,
                reason="R" * 400,
                recommendations=["A" * 280, "B" * 280, "C" * 280],
                follow_up_email_draft="E" * 1200,
            )
        return DealFollowUpAssessment(
            summary="The deal has been inactive.",
            reason="The last recorded activity was several days ago.",
            recommendations=["Confirm the next step.", "Offer a short review meeting."],
            follow_up_email_draft="Hello, would you be available to discuss next steps?",
        )


class IntentAwareFollowUpProvider(FollowUpProvider):
    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            assert output_schema is CRMIntentExtraction
            return CRMIntentExtraction(
                intent="crm_question",
                query="deal_follow_up",
                confidence=0.95,
            )
        return await super().generate_structured(
            prompt_name=prompt_name,
            variables=variables,
            output_schema=output_schema,
        )


class FollowUpTools:
    def __init__(self, deals: list[HubSpotDeal], *, fail_listing: bool = False) -> None:
        self.deals = deals
        self.fail_listing = fail_listing
        self.requested_properties: tuple[str, ...] | None = None
        self.url_records: list[tuple[str, str]] = []

    async def list_all_deals(self, tenant_id: str, *, properties):
        self.requested_properties = tuple(properties)
        if self.fail_listing:
            raise IntegrationError("HubSpot request failed")
        return self.deals

    async def deal_pipelines(self, tenant_id: str):
        return [
            HubSpotPipeline(
                id="pipeline-1",
                label="New Business",
                stages=[
                    HubSpotPipelineStage(
                        id="open",
                        label="Qualification",
                        metadata={"isClosed": "false"},
                    ),
                    HubSpotPipelineStage(
                        id="won",
                        label="Closed Won",
                        metadata={"isClosed": "true", "isClosedWon": "true"},
                    ),
                    HubSpotPipelineStage(
                        id="lost",
                        label="Closed Lost",
                        metadata={"isClosed": "true", "isClosedWon": "false"},
                    ),
                    HubSpotPipelineStage(
                        id="unknown",
                        label="Unknown",
                        metadata={},
                    ),
                ],
            )
        ]

    async def hubspot_record_urls(self, tenant_id: str, records):
        self.url_records = list(records)
        return [
            f"https://app.hubspot.com/contacts/42/record/0-3/{deal_id}"
            for _, deal_id in records
        ]


@pytest.mark.asyncio
async def test_follow_up_query_is_routed_when_intent_extraction_selects_it():
        tools = FollowUpTools([make_deal("aged", inactive_days=7)])
        provider = IntentAwareFollowUpProvider()
        response = await make_agent(tools, provider).respond(
            request("Review our opportunities and give me recommended actions.")
        )

        assert response.status == "ok"
        assert response.result is not None
        assert response.result["deals_needing_follow_up"] == 1
        assert len(provider.calls) == 1


def make_deal(
    deal_id: str,
    *,
    inactive_days: int | None,
    stage: str = "open",
) -> HubSpotDeal:
    activity = (
        (datetime.now(UTC).date() - timedelta(days=inactive_days)).isoformat()
        if inactive_days is not None
        else None
    )
    return HubSpotDeal(
        id=deal_id,
        properties={
            "dealname": f"Deal {deal_id}",
            "amount": "25000",
            "dealstage": stage,
            "pipeline": "pipeline-1",
            "closedate": (datetime.now(UTC).date() + timedelta(days=10)).isoformat(),
            "hubspot_owner_id": "owner-12",
            "notes_last_updated": activity,
        },
    )


def request(
    message: str = "Find deals that need follow-up and prepare recommended actions.",
) -> AgentRequest:
    return AgentRequest(
        tenant_id="tenant-a",
        actor_id="slack-user",
        message=message,
        request_id="request-1",
    )


def make_agent(
    tools: FollowUpTools,
    provider: FollowUpProvider,
    *,
    batch_size: int = 10,
) -> AccountIntelligenceAgent:
    return AccountIntelligenceAgent(
        tools,  # type: ignore[arg-type]
        AIService(provider),
        SimpleNamespace(),  # type: ignore[arg-type]
        deal_follow_up_batch_size=batch_size,
    )


@pytest.mark.asyncio
async def test_follow_up_uses_stage_metadata_activity_bands_and_grounded_results():
    tools = FollowUpTools(
        [
            make_deal("high", inactive_days=14),
            make_deal("medium", inactive_days=7),
            make_deal("low", inactive_days=3),
            make_deal("recent", inactive_days=2),
            make_deal("unknown-activity", inactive_days=None),
            make_deal("won", inactive_days=30, stage="won"),
            make_deal("lost", inactive_days=30, stage="lost"),
            make_deal("unknown-stage", inactive_days=30, stage="unknown"),
        ]
    )
    provider = FollowUpProvider()
    response = await make_agent(tools, provider).respond(request())

    assert response.status == "ok"
    assert response.result is not None
    assert response.result["open_deals_reviewed"] == 5
    assert response.result["deals_needing_follow_up"] == 3
    assert response.result["unknown_activity_count"] == 1
    assert response.result["recently_active_count"] == 1
    assert response.result["unknown_stage_count"] == 1
    assert [call["crm_facts"]["priority"] for call in provider.calls] == [
        "High",
        "Medium",
        "Low",
    ]
    assert all(
        call["crm_facts"]["follow_up_task_status"] == "Not verified"
        for call in provider.calls
    )
    assert "notes_last_updated" in tools.requested_properties
    assert "Last activity: " in response.text
    assert "inactive days" in response.text
    assert "High priority" in response.text
    assert "Suggested email draft" in response.text
    assert "Upcoming follow-up task: Not verified" in response.text
    assert "<https://app.hubspot.com/contacts/42/record/0-3/high|Open in HubSpot>" in (
        response.text
    )
    assert "Deal won" not in response.text
    assert "Deal lost" not in response.text
    assert "Deal unknown-activity" in response.text
    assert tools.url_records == [
        ("deals", "high"),
        ("deals", "medium"),
        ("deals", "low"),
        ("deals", "unknown-activity"),
    ]


@pytest.mark.asyncio
async def test_follow_up_batch_limit_prioritizes_highest_risk_and_reports_remaining():
    tools = FollowUpTools(
        [make_deal(f"deal-{days}", inactive_days=days) for days in (3, 14, 7, 20)]
    )
    provider = FollowUpProvider()
    response = await make_agent(tools, provider, batch_size=2).respond(request())

    assert response.status == "ok"
    assert len(provider.calls) == 2
    assert [call["crm_facts"]["inactivity_days"] for call in provider.calls] == [20, 14]
    assert response.result is not None
    assert response.result["deals_needing_follow_up"] == 4
    assert response.result["deals_not_ai_analyzed_due_to_limit"] == 2
    assert "2 additional deal(s) need follow-up" in response.text


@pytest.mark.asyncio
async def test_missing_activity_is_not_assumed_inactive_and_recent_deals_need_no_ai():
    tools = FollowUpTools(
        [
            make_deal("missing", inactive_days=None),
            make_deal("recent", inactive_days=1),
        ]
    )
    provider = FollowUpProvider()
    response = await make_agent(tools, provider).respond(request())

    assert response.status == "ok"
    assert not provider.calls
    assert response.result is not None
    assert response.result["deals_needing_follow_up"] == 0
    assert response.result["unknown_activity_count"] == 1
    assert response.result["recently_active_count"] == 1
    assert "not counted as inactive" in response.text
    assert "No deals with a known activity date" in response.text


@pytest.mark.asyncio
async def test_follow_up_reports_no_open_deals():
    tools = FollowUpTools([make_deal("won", inactive_days=30, stage="won")])
    response = await make_agent(tools, FollowUpProvider()).respond(request())

    assert response.status == "ok"
    assert response.result is not None
    assert response.result["open_deals_reviewed"] == 0
    assert response.result["deals_needing_follow_up"] == 0
    assert "Open deals reviewed: 0" in response.text


@pytest.mark.asyncio
async def test_follow_up_hubspot_errors_return_an_explicit_unavailable_response():
    response = await make_agent(
        FollowUpTools([], fail_listing=True),
        FollowUpProvider(),
    ).respond(request())

    assert response.status == "unavailable"
    assert "couldn't retrieve HubSpot deals" in response.text


@pytest.mark.asyncio
async def test_follow_up_ai_failures_are_explicit_and_do_not_stop_other_deals():
    tools = FollowUpTools(
        [make_deal("one", inactive_days=14), make_deal("two", inactive_days=7)]
    )
    response = await make_agent(tools, FollowUpProvider(fail_first=True)).respond(request())

    assert response.status == "partial"
    assert response.result is not None
    assert response.result["deals_ai_analyzed"] == 1
    assert "AI recommendations could not be generated" in response.text
    assert "Deals needing follow-up" in response.text


@pytest.mark.asyncio
async def test_follow_up_slack_report_respects_message_size_limit():
    tools = FollowUpTools(
        [
            make_deal(f"deal-{index}-{ 'N' * 180}", inactive_days=14)
            for index in range(25)
        ]
    )
    provider = FollowUpProvider(max_length_output=True)
    response = await make_agent(tools, provider, batch_size=25).respond(request())

    assert len(response.text) <= 39_000
    assert "omitted to fit Slack's message limit" in response.text
    assert response.result is not None
    assert response.result["deals_ai_analyzed"] == 25


def test_follow_up_property_selection_uses_hubspot_last_activity_property():
    assert "notes_last_updated" in DEAL_FOLLOW_UP_PROPERTIES
    assert "hs_lastactivitydate" not in DEAL_FOLLOW_UP_PROPERTIES


def test_follow_up_batch_size_setting_has_safe_bounds():
    from app.core.config import Settings

    assert Settings().deal_follow_up_batch_size == 10
    with pytest.raises(ValueError):
        Settings(deal_follow_up_batch_size=0)
