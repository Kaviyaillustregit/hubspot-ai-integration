import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from app.agent.crm_intelligence import plan_crm_query
from app.agent.schemas import AgentRequest, AgentResponse, CRMIntentExtraction
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.provider import AIProvider
from app.ai.service import AIService
from app.api.slack import _respond
from app.core.config import Settings
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotContactsPage,
    HubSpotDeal,
    HubSpotDealsPage,
    HubSpotPipeline,
    HubSpotPipelineStage,
)
from app.integrations.slack.client import SlackClient
from app.integrations.slack.http_client import SlackWebApiClient


class MockRecordingSlackClient(SlackClient):
    """Slack client that records all post_message and update_message calls."""

    def __init__(self, *, fail_update: bool = False, fail_initial_post: bool = False) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self.fail_update = fail_update
        self.fail_initial_post = fail_initial_post
        self._msg_counter = 0

    async def post_message(
        self,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str | None:
        self._msg_counter += 1
        ts = f"1710000000.{self._msg_counter:06d}"
        if self.fail_initial_post and self._msg_counter == 1:
            raise IntegrationError("Slack post_message failed")
        self.posted.append({"channel": channel, "text": text, "blocks": blocks, "ts": ts})
        return ts

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        if self.fail_update:
            raise IntegrationError("Slack update_message failed")
        self.updated.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})

    async def delete_message(self, channel: str, ts: str) -> None:
        self.deleted.append({"channel": channel, "ts": ts})

    async def publish_home_view(self, user_id: str, view: dict[str, Any]) -> None:
        return None


class LegacyMockClient:
    """Mock client that only implements post_message, simulating legacy/text-only test doubles."""

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    async def post_message(self, channel: str, text: str, **kwargs: Any) -> None:
        self.posted.append({"channel": channel, "text": text, **kwargs})


class AllowAuthorization:
    async def is_authorized(self, **kwargs: Any) -> bool:
        return True


class DenyAuthorization:
    async def is_authorized(self, **kwargs: Any) -> bool:
        return False


def make_slack_event(
    text: str = "Show me deals in Q3 2026", channel: str = "C1", user: str = "U1"
) -> dict[str, Any]:
    return {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": f"Ev-{int(time.time() * 1000)}",
        "event": {
            "type": "message",
            "user": user,
            "channel": channel,
            "text": text,
            "ts": "1712345678.000100",
        },
    }


@pytest.mark.asyncio
async def test_slack_progress_stages_and_final_response_update():
    """Verify that progress stages are reported in order and final response updates the message."""
    client = MockRecordingSlackClient()
    auth = AllowAuthorization()

    # Agent that simulates execution across all stages
    class MockAgent:
        async def respond(self, request: AgentRequest, on_progress=None) -> AgentResponse:
            assert on_progress is not None
            await on_progress("Understanding your request")
            await on_progress("Retrieving CRM data")
            await on_progress("Analyzing results")
            await on_progress("Preparing your response")
            return AgentResponse(
                status="ok",
                text="Here is your deal summary for Q3.",
                request_id=request.request_id,
            )

    event = make_slack_event("Show me deals in Q3 2026")
    await _respond(MockAgent(), client, auth, event, "tenant-a", "req-1")

    # The initial progress message was posted
    assert len(client.posted) == 1
    assert "Understanding your request" in client.posted[0]["text"]
    progress_ts = client.posted[0]["ts"]

    # Subsequent progress stages updated the same message
    update_texts = [u["text"] for u in client.updated]
    assert any("Retrieving CRM data" in t for t in update_texts)
    assert any("Analyzing results" in t for t in update_texts)
    assert any("Preparing your response" in t for t in update_texts)

    # All updates targeted the exact same message timestamp
    for update in client.updated:
        assert update["ts"] == progress_ts
        assert update["channel"] == "C1"

    # Final response updated the progress message, so no second message was posted
    assert len(client.posted) == 1
    final_update = client.updated[-1]
    assert final_update["ts"] == progress_ts
    assert "Here is your deal summary for Q3." in final_update["text"]


@pytest.mark.asyncio
async def test_slack_progress_failure_in_update_falls_back_to_post_message():
    """Verify that if updating message with final response fails, it falls back to post_message."""
    client = MockRecordingSlackClient(fail_update=True)
    auth = AllowAuthorization()

    class MockAgent:
        async def respond(self, request: AgentRequest, on_progress=None) -> AgentResponse:
            if on_progress:
                await on_progress("Understanding your request")
            return AgentResponse(
                status="ok",
                text="Fallback final answer.",
                request_id=request.request_id,
            )

    event = make_slack_event("What is our pipeline?")
    await _respond(MockAgent(), client, auth, event, "tenant-a", "req-2")

    # Initial progress was posted (message 1)
    # Then update failed, so fallback post_message was called with the final answer (message 2)
    assert len(client.posted) == 2
    assert "Understanding your request" in client.posted[0]["text"]
    assert "Fallback final answer." in client.posted[1]["text"]


@pytest.mark.asyncio
async def test_slack_progress_failure_in_initial_post_allows_final_response():
    """Verify that if initial progress post fails, the final response is still delivered."""
    client = MockRecordingSlackClient(fail_initial_post=True)
    auth = AllowAuthorization()

    class MockAgent:
        async def respond(self, request: AgentRequest, on_progress=None) -> AgentResponse:
            if on_progress:
                await on_progress("Understanding your request")
            return AgentResponse(
                status="ok",
                text="Completed answer despite progress failure.",
                request_id=request.request_id,
            )

    event = make_slack_event("Tell me about Northwind")
    await _respond(MockAgent(), client, auth, event, "tenant-a", "req-3")

    # Initial attempt failed, so fallback final response was delivered via post_message
    assert len(client.posted) == 1
    assert "Completed answer despite progress failure." in client.posted[0]["text"]


@pytest.mark.asyncio
async def test_slack_progress_unsupported_client_delivers_response_without_progress():
    """Verify that legacy clients without update_message receive only the final response."""
    client = LegacyMockClient()
    auth = AllowAuthorization()

    class MockAgent:
        async def respond(self, request: AgentRequest, on_progress=None) -> AgentResponse:
            if on_progress:
                await on_progress("Understanding your request")
                await on_progress("Retrieving CRM data")
            return AgentResponse(
                status="ok",
                text="Direct answer to legacy client.",
                request_id=request.request_id,
            )

    event = make_slack_event("List all contacts")
    await _respond(MockAgent(), client, auth, event, "tenant-a", "req-4")

    # Only one message was posted: the final answer
    assert len(client.posted) == 1
    assert client.posted[0]["text"] == "Direct answer to legacy client."
    assert "Understanding" not in client.posted[0]["text"]


@pytest.mark.asyncio
async def test_slack_progress_duplicate_delivery_does_not_reply():
    """Verify that duplicate message deliveries post neither progress nor response."""
    client = MockRecordingSlackClient()
    auth = AllowAuthorization()

    class MockAgent:
        async def respond(self, request: AgentRequest, on_progress=None) -> AgentResponse:
            return AgentResponse(
                status="duplicate_request",
                text="This message was already processed.",
                request_id=request.request_id,
            )

    event = make_slack_event("Duplicate query")
    await _respond(MockAgent(), client, auth, event, "tenant-a", "req-5")

    assert len(client.posted) == 0
    assert len(client.updated) == 0


@pytest.mark.asyncio
async def test_slack_progress_unauthorized_user_does_not_post_progress():
    """Verify that unauthorized users receive no progress messages."""
    client = MockRecordingSlackClient()
    auth = DenyAuthorization()
    agent_called = False

    class MockAgent:
        async def respond(self, request: AgentRequest, on_progress=None) -> AgentResponse:
            nonlocal agent_called
            agent_called = True
            return AgentResponse(status="ok", text="secret", request_id=request.request_id)

    event = make_slack_event("Unauthorized query")
    await _respond(MockAgent(), client, auth, event, "tenant-a", "req-6")

    assert not agent_called
    assert len(client.posted) == 0
    assert len(client.updated) == 0


@pytest.mark.asyncio
async def test_slack_progress_state_isolation_concurrent_requests():
    """Verify that concurrent requests have isolated progress state without crosstalk."""
    client1 = MockRecordingSlackClient()
    client2 = MockRecordingSlackClient()
    auth = AllowAuthorization()

    class DelayedAgent:
        def __init__(self, delay: float, answer: str) -> None:
            self.delay = delay
            self.answer = answer

        async def respond(self, request: AgentRequest, on_progress=None) -> AgentResponse:
            if on_progress:
                await on_progress("Understanding your request")
                await asyncio.sleep(self.delay)
                await on_progress("Retrieving CRM data")
                await on_progress("Preparing your response")
            return AgentResponse(status="ok", text=self.answer, request_id=request.request_id)

    event1 = make_slack_event("Query 1", channel="C1", user="U1")
    event2 = make_slack_event("Query 2", channel="C2", user="U2")

    await asyncio.gather(
        _respond(DelayedAgent(0.02, "Answer 1"), client1, auth, event1, "tenant-1", "req-c1"),
        _respond(DelayedAgent(0.01, "Answer 2"), client2, auth, event2, "tenant-2", "req-c2"),
    )

    # Check client 1
    assert len(client1.posted) == 1
    assert client1.posted[0]["channel"] == "C1"
    for u in client1.updated:
        assert u["channel"] == "C1"
    assert "Answer 1" in client1.updated[-1]["text"]

    # Check client 2
    assert len(client2.posted) == 1
    assert client2.posted[0]["channel"] == "C2"
    for u in client2.updated:
        assert u["channel"] == "C2"
    assert "Answer 2" in client2.updated[-1]["text"]


@pytest.mark.asyncio
async def test_slack_web_api_client_update_and_delete_message():
    """Verify update_message and delete_message calls against Slack API endpoints."""
    settings = Settings(slack_bot_token="xoxb-test-token")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        assert request.headers.get("Authorization") == "Bearer xoxb-test-token"
        body = json.loads(request.content)
        if url_str == "https://slack.com/api/chat.postMessage":
            assert body["channel"] == "C1"
            assert body["text"] == "hello"
            return httpx.Response(
                200, json={"ok": True, "channel": "C1", "ts": "1710000000.123456"}
            )
        if url_str == "https://slack.com/api/chat.update":
            assert body["channel"] == "C1"
            assert body["ts"] == "1710000000.123456"
            assert body["text"] == "updated hello"
            return httpx.Response(
                200, json={"ok": True, "channel": "C1", "ts": "1710000000.123456"}
            )
        if url_str == "https://slack.com/api/chat.delete":
            assert body["channel"] == "C1"
            assert body["ts"] == "1710000000.123456"
            return httpx.Response(
                200, json={"ok": True, "channel": "C1", "ts": "1710000000.123456"}
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = SlackWebApiClient(settings, client=mock_http)

        # 1. post_message returns ts
        ts = await client.post_message("C1", "hello")
        assert ts == "1710000000.123456"

        # 2. update_message succeeds
        await client.update_message("C1", ts, "updated hello")

        # 3. delete_message succeeds
        await client.delete_message("C1", ts)


class _CRMRouteProvider(AIProvider):
    async def generate_structured(self, *, prompt_name: str, variables: dict, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(
                intent="crm_question",
                confidence=1.0,
                question=variables["message"],
                query="all_deals",
            )
        if prompt_name == "crm-intelligence/v1":
            return plan_crm_query(variables["query"])
        raise AssertionError(f"Unexpected prompt: {prompt_name}")


class _Companies:
    async def list_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=[])

    async def search_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=[])


class _Contacts:
    async def get_hubspot_account_id(self, context):
        return "42"

    async def list_contacts(self, context, **kwargs):
        return HubSpotContactsPage(results=[])

    async def search_contacts(self, context, **kwargs):
        return HubSpotContactsPage(results=[])


class _Deals:
    async def list_deals(self, context, **kwargs):
        return HubSpotDealsPage(results=[
            HubSpotDeal(
                id="deal-1",
                properties={
                    "dealname": "Acme Renewal",
                    "dealstage": "qualification",
                    "amount": "15000",
                    "closedate": "2026-09-30",
                },
            )
        ])

    async def get_deal_type_options(self, context):
        return []

    async def list_pipelines(self, context):
        return [
            HubSpotPipeline(
                id="sales",
                label="Sales",
                stages=[
                    HubSpotPipelineStage(id="qualification", label="Qualification", metadata={}),
                ],
            )
        ]


@pytest.mark.asyncio
async def test_real_agent_crm_query_invokes_execution_stages_in_order():
    """Verify that AccountIntelligenceAgent calls the execution stages in order for a CRM query."""
    client = MockRecordingSlackClient()
    auth = AllowAuthorization()

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(_Companies(), _Contacts(), _Deals()),
        AIService(_CRMRouteProvider()),
        object(),
    )

    event = make_slack_event("Show me all deals closing in Q3 2026")
    await _respond(agent, client, auth, event, "tenant-a", "req-real-1")

    # Initial progress was posted
    assert len(client.posted) == 1
    assert "Understanding your request" in client.posted[0]["text"]
    ts = client.posted[0]["ts"]

    # All stage updates occurred
    update_texts = [u["text"] for u in client.updated]
    assert any("Retrieving CRM data" in t for t in update_texts)
    assert any("Analyzing results" in t for t in update_texts)
    assert any("Preparing your response" in t for t in update_texts)

    # Final update contains the deal answer
    final_update = client.updated[-1]
    assert final_update["ts"] == ts
    assert "Acme Renewal" in final_update["text"]
    # Exactly one message was posted in the channel
    assert len(client.posted) == 1
