import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.schemas import AgentResponse
from app.api.app import create_app
from app.api.slack import get_action_safety, get_agent, get_slack_client
from app.core.config import Settings
from app.integrations.errors import IntegrationError
from app.integrations.slack.events import parse_app_home_opened
from app.integrations.slack.home import (
    CONFIRM_ACTION,
    QUICK_ACTION_PREFIX,
    REQUEST_INPUT_ACTION,
    SUBMIT_ACTION,
    build_home_view,
    parse_home_interaction,
    render_response_blocks,
)
from app.integrations.slack.http_client import SlackWebApiClient
from app.services.action_safety import RecentAction

ACTION_ID = "0123456789abcdef0123456789abcdef"


def all_text(blocks: list[dict]) -> str:
    return json.dumps(blocks, ensure_ascii=False)


def blocks_of_type(view: dict, block_type: str) -> list[dict]:
    return [block for block in view["blocks"] if block["type"] == block_type]


# --- Home view rendering -------------------------------------------------------------


def test_home_view_has_branding_input_send_and_quick_actions():
    view = build_home_view(recent=[])

    assert view["type"] == "home"
    assert len(view["blocks"]) <= 100
    headers = [block["text"]["text"] for block in blocks_of_type(view, "header")]
    assert headers == ["HubSpot AI Agent"]
    assert len(blocks_of_type(view, "divider")) == 3
    text = all_text(view["blocks"])
    assert "✦  Your AI assistant for HubSpot CRM" in text
    assert "✦  *AI Assistant*" in text
    assert "I can create and update contacts, companies and deals" in text

    [request_input] = blocks_of_type(view, "input")
    assert request_input["dispatch_action"] is True
    assert request_input["element"]["action_id"] == REQUEST_INPUT_ACTION
    assert request_input["element"]["placeholder"]["text"] == "Ask your HubSpot AI Agent..."
    assert "hint" not in request_input
    assert "initial_value" not in request_input["element"]

    action_ids = [
        element["action_id"]
        for block in blocks_of_type(view, "actions")
        for element in block["elements"]
    ]
    assert action_ids[0] == SUBMIT_ACTION
    quick_keys = (
        "create_contact",
        "find_contact",
        "find_company",
        "account_overview",
        "search_crm",
    )
    assert action_ids[1:] == [f"{QUICK_ACTION_PREFIX}{key}" for key in quick_keys]
    assert len(set(action_ids)) == len(action_ids)
    assert "View Contact" not in text


def test_quick_action_draft_prefills_a_fresh_input_block():
    first = blocks_of_type(build_home_view(draft="Create a contact named "), "input")[0]
    second = blocks_of_type(build_home_view(draft="Create a contact named "), "input")[0]

    assert first["element"]["initial_value"] == "Create a contact named "
    assert first["block_id"] != second["block_id"]


def test_working_state_shows_user_bubble_then_assistant_loading():
    blocks = build_home_view(request_text="Tell me about <ABC>", working=True)["blocks"]
    types = [block["type"] for block in blocks]

    user_label = next(i for i, b in enumerate(blocks) if all_text([b]).count("*You*"))
    bubble = blocks[user_label + 1]
    assert bubble["type"] == "rich_text"
    quote = bubble["elements"][0]
    assert quote["type"] == "rich_text_quote"
    # Rich text is literal: user input is shown as typed and can't become a mention.
    assert quote["elements"] == [{"type": "text", "text": "Tell me about <ABC>"}]
    assert blocks[user_label + 2]["elements"][0]["text"] == "✦  *AI Assistant*"
    assert blocks[user_label + 3]["text"]["text"] == "⋯"
    assert types.index("rich_text") < types.index("input")


def test_contact_created_renders_professional_fields():
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text="Contact Angel John was created successfully in HubSpot.",
            request_id="r",
            result={
                "kind": "contact_created",
                "contact_id": "565238436562",
                "name": "Angel John",
                "email": "angel@example.com",
                "company_name": "ABC <!channel>",
            },
        )
    )

    assert blocks[0]["text"]["text"] == "✓  *Contact created*"
    assert blocks[1]["text"]["text"] == (
        "Angel John was successfully added to HubSpot. Associated with ABC &lt;!channel&gt;."
    )
    assert blocks[2]["elements"][0]["text"] == (
        "angel@example.com  ·  HubSpot ID `565238436562`"
    )


def test_pending_confirmation_renders_confirm_button_instead_of_typed_instructions():
    blocks = render_response_blocks(
        AgentResponse(
            status="pending_confirmation",
            text=(
                "I found a request to delete this HubSpot contact:\n• Contact ID: 123\n\n"
                f"Action ID: `{ACTION_ID}`\n"
                f"Reply with `confirm {ACTION_ID}` to delete this contact."
            ),
            request_id="r",
            result={
                "kind": "pending_confirmation",
                "action_id": ACTION_ID,
                "action_type": "delete_contact",
                "contact_id": "123",
            },
        )
    )

    text = all_text(blocks)
    assert "🛡  *Confirmation needed*" in text
    assert "Reply with" not in text
    [actions] = [block for block in blocks if block["type"] == "actions"]
    button = actions["elements"][0]
    assert button["action_id"] == CONFIRM_ACTION
    assert button["value"] == ACTION_ID
    assert button["style"] == "danger"
    assert button["confirm"]["confirm"]["text"] == "Delete"


@pytest.mark.parametrize(
    ("status", "title"),
    [
        ("missing_fields", "ⓘ  *More information needed*"),
        ("invalid_request", "ⓘ  *More information needed*"),
        ("company_not_found", "⚠  *Unable to complete request*"),
        ("duplicate", "⚠  *Unable to complete request*"),
        ("hubspot_not_authorized", "⚠  *Unable to complete request*"),
        ("unavailable", "⚠  *Unable to complete request*"),
    ],
)
def test_non_success_states_are_clearly_labelled(status, title):
    blocks = render_response_blocks(
        AgentResponse(status=status, text="Reason <@U123>.", request_id="r")
    )

    assert blocks[0]["text"]["text"] == title
    assert blocks[1]["text"]["text"] == "Reason &lt;@U123&gt;."


def test_summary_text_is_split_into_section_sized_chunks():
    long_text = "\n\n".join("• fact " + "x" * 1000 for _ in range(6))
    blocks = render_response_blocks(AgentResponse(status="ok", text=long_text, request_id="r"))

    assert blocks[0]["text"]["text"] == "✓  *Here's what I found*"
    assert len(blocks) > 2
    assert all(len(block["text"]["text"]) <= 3000 for block in blocks)


def test_recent_activity_lines_reflect_real_action_state():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    recent = [
        RecentAction(
            "create_contact",
            "completed",
            {"firstname": "Angel", "lastname": "John", "email": "angel@example.com"},
            now - timedelta(minutes=3),
            now,
        ),
        RecentAction(
            "delete_contact", "pending", {"contact_id": "123"}, now, now + timedelta(minutes=4)
        ),
        RecentAction(
            "update_contact",
            "pending",
            {"contact_id": "456"},
            now - timedelta(hours=1),
            now - timedelta(minutes=55),
        ),
    ]

    text = all_text(build_home_view(recent=recent, now=now)["blocks"])

    assert "✅ Created contact *Angel John*" in text
    assert "🛡️ Delete of contact `123` is awaiting your confirmation" in text
    assert "⌛ Update of contact `456` expired before confirmation" in text
    assert "<!date^" in text


def test_recent_activity_empty_and_unavailable_states():
    assert "No CRM actions yet" in all_text(build_home_view(recent=[])["blocks"])
    assert "unavailable" in all_text(build_home_view(recent_unavailable=True)["blocks"])


# --- Interaction parsing ------------------------------------------------------------


def home_payload(action: dict, *, view_type: str = "home", request_text: str = "") -> dict:
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1", "team_id": "T1"},
        "view": {
            "type": view_type,
            "blocks": [{"type": "input", "block_id": "home_request:abc"}],
            "state": {
                "values": {
                    "home_request:abc": {
                        REQUEST_INPUT_ACTION: {"type": "plain_text_input", "value": request_text}
                    }
                }
            },
        },
        "actions": [{"action_ts": "1712345678.000100", **action}],
    }


def stale_state_payload(current_text: str) -> dict:
    """Shape of the real Slack payload from the failed test (13:27, 7 Oct 2026)."""

    def entry(value: str | None) -> dict:
        return {REQUEST_INPUT_ACTION: {"type": "plain_text_input", "value": value}}

    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1", "team_id": "T1"},
        "view": {
            "type": "home",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "How can I help you?"}},
                {"type": "input", "block_id": "home_request:ac9587d8b86c"},
                {"type": "actions", "block_id": "home_submit_actions"},
            ],
            "state": {
                "values": {
                    "home_request:7f3bd79e47dc": entry(None),
                    "home_request:36ac9ca5403d": entry("Tell me about the company "),
                    "home_request:9b475e12abc1": entry("Give me an account overview of "),
                    "home_request:d153fa32c192": entry("Create a company called Ai acro Company."),
                    "home_request:ac9587d8b86c": entry(current_text),
                }
            },
        },
        "actions": [{"action_id": SUBMIT_ACTION, "value": "send", "action_ts": "1791.0001"}],
    }


def test_send_reads_the_input_on_screen_not_stale_quick_action_text():
    interaction = parse_home_interaction(
        stale_state_payload("Create a company called Test AI Company.")
    )

    assert interaction is not None
    assert interaction.kind == "submit"
    assert interaction.text == "Create a company called Test AI Company."


def test_send_with_empty_on_screen_input_ignores_stale_values():
    interaction = parse_home_interaction(stale_state_payload(""))

    assert interaction is not None
    assert interaction.text == ""


def test_home_send_delivers_the_typed_request_to_the_agent():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()

    post_interaction(
        app_with(agent, client, safety),
        stale_state_payload("Create a company called Test AI Company."),
    )

    assert [request.message for request in agent.requests] == [
        "Create a company called Test AI Company."
    ]
    assert "Create a company called Test AI Company." in all_text(client.views[-1][1]["blocks"])


def test_send_button_reads_request_from_view_state():
    interaction = parse_home_interaction(
        home_payload({"action_id": SUBMIT_ACTION}, request_text="  Create Angel John  ")
    )

    assert interaction is not None
    assert interaction.kind == "submit"
    assert interaction.text == "Create Angel John"
    assert (interaction.team_id, interaction.user_id) == ("T1", "U1")
    assert interaction.action_ts == "1712345678.000100"


def test_enter_in_input_uses_the_dispatched_value():
    interaction = parse_home_interaction(
        home_payload({"action_id": REQUEST_INPUT_ACTION, "value": "Tell me about ABC"})
    )

    assert interaction is not None and interaction.text == "Tell me about ABC"


def test_quick_action_template_comes_from_server_not_payload():
    interaction = parse_home_interaction(
        home_payload({"action_id": f"{QUICK_ACTION_PREFIX}find_company", "value": "<evil>"})
    )

    assert interaction is not None
    assert interaction.kind == "quick_action"
    assert interaction.template == "Tell me about the company "


@pytest.mark.parametrize(
    "payload",
    [
        home_payload({"action_id": SUBMIT_ACTION}, view_type="modal"),
        home_payload({"action_id": "something_else"}),
        home_payload({"action_id": f"{QUICK_ACTION_PREFIX}unknown"}),
        home_payload({"action_id": CONFIRM_ACTION, "value": "not-an-action-id"}),
        {"type": "view_submission"},
        "not a dict",
    ],
)
def test_untrusted_or_foreign_interactions_are_ignored(payload):
    assert parse_home_interaction(payload) is None


def test_confirm_button_carries_validated_action_id():
    interaction = parse_home_interaction(
        home_payload({"action_id": CONFIRM_ACTION, "value": ACTION_ID})
    )

    assert interaction is not None
    assert interaction.kind == "confirm"
    assert interaction.action_id == ACTION_ID


def test_app_home_opened_is_parsed_only_for_the_home_tab():
    event = {
        "type": "event_callback",
        "team_id": "T1",
        "event": {"type": "app_home_opened", "user": "U1", "tab": "home"},
    }
    opened = parse_app_home_opened(event)
    assert opened is not None and (opened.team_id, opened.user_id) == ("T1", "U1")

    event["event"]["tab"] = "messages"
    assert parse_app_home_opened(event) is None


# --- Endpoints ----------------------------------------------------------------------


class RecordingClient:
    def __init__(self) -> None:
        self.views: list[tuple[str, dict]] = []
        self.messages: list[tuple[str, str]] = []

    async def publish_home_view(self, user_id, view):
        self.views.append((user_id, view))

    async def post_message(self, channel, text):
        self.messages.append((channel, text))


class FakeSafety:
    def __init__(self, recent: list[RecentAction] | None = None) -> None:
        self.recent = recent or []
        self.lookups: list[tuple[str, str]] = []

    async def recent_actions(self, *, tenant_id, actor_id, limit=5):
        self.lookups.append((tenant_id, actor_id))
        return self.recent


class RecordingAgent:
    def __init__(self, response: AgentResponse | None = None) -> None:
        self.requests = []
        self.response = response

    async def respond(self, request):
        self.requests.append(request)
        return self.response or AgentResponse(
            status="ok",
            text="Contact Angel John was created successfully in HubSpot.",
            request_id=request.request_id,
            result={"kind": "contact_created", "contact_id": "42", "name": "Angel John"},
        )


def app_with(agent, client, safety, team_map='{"T1":"tenant-a"}'):
    app = create_app(Settings(slack_signing_secret="signing", slack_team_tenant_map=team_map))
    app.dependency_overrides[get_agent] = lambda: agent
    app.dependency_overrides[get_slack_client] = lambda: client
    app.dependency_overrides[get_action_safety] = lambda: safety
    return app


def signed(body: bytes, secret: str = "signing") -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256)
    return {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={signature.hexdigest()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def post_interaction(app, payload: dict, headers: dict[str, str] | None = None):
    body = urlencode({"payload": json.dumps(payload)}).encode()
    with TestClient(app) as test_client:
        return test_client.post(
            "/api/v1/slack/interactions", content=body, headers=headers or signed(body)
        )


def test_app_home_opened_publishes_home_for_mapped_workspace():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()
    app = app_with(agent, client, safety)
    body = json.dumps(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {"type": "app_home_opened", "user": "U1", "tab": "home"},
        }
    ).encode()

    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/slack/events", content=body, headers=signed(body)
        )

    assert response.status_code == 200
    assert [user for user, _ in client.views] == ["U1"]
    assert client.views[0][1]["type"] == "home"
    assert safety.lookups == [("tenant-a", "U1")]
    assert agent.requests == []
    assert client.messages == []


def test_home_request_goes_through_the_existing_agent_and_renders_result():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()
    app = app_with(agent, client, safety)

    response = post_interaction(
        app,
        home_payload(
            {"action_id": SUBMIT_ACTION},
            request_text="Create a contact named Angel John, email angel@example.com",
        ),
    )

    assert response.status_code == 200
    [request] = agent.requests
    assert request.tenant_id == "tenant-a"
    assert request.actor_id == "U1"
    assert request.message == "Create a contact named Angel John, email angel@example.com"
    assert request.channel_id == "apphome:U1"
    assert request.message_ts == "1712345678.000100"
    assert len(client.views) == 2
    assert "⋯" in all_text(client.views[0][1]["blocks"])
    assert "✓  *Contact created*" in all_text(client.views[1][1]["blocks"])
    assert client.messages == []


def test_home_confirm_button_sends_confirmation_through_the_agent():
    client, safety = RecordingClient(), FakeSafety()
    agent = RecordingAgent(
        AgentResponse(status="ok", text="Contact deleted successfully in HubSpot.", request_id="r")
    )

    post_interaction(
        app_with(agent, client, safety),
        home_payload({"action_id": CONFIRM_ACTION, "value": ACTION_ID}),
    )

    assert [request.message for request in agent.requests] == [f"confirm {ACTION_ID}"]
    assert "Contact deleted successfully" in all_text(client.views[-1][1]["blocks"])


def test_quick_action_only_prefills_input_without_calling_the_agent():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()

    post_interaction(
        app_with(agent, client, safety),
        home_payload({"action_id": f"{QUICK_ACTION_PREFIX}create_contact"}),
    )

    assert agent.requests == []
    [request_input] = blocks_of_type(client.views[0][1], "input")
    assert request_input["element"]["initial_value"] == "Create a contact named "


def test_empty_home_request_asks_for_input_without_calling_the_agent():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()

    post_interaction(
        app_with(agent, client, safety), home_payload({"action_id": SUBMIT_ACTION})
    )

    assert agent.requests == []
    assert "More information needed" in all_text(client.views[0][1]["blocks"])


def test_agent_failure_is_rendered_as_an_error_state():
    class FailingAgent(RecordingAgent):
        async def respond(self, request):
            raise RuntimeError("boom")

    client = RecordingClient()
    post_interaction(
        app_with(FailingAgent(), client, FakeSafety()),
        home_payload({"action_id": SUBMIT_ACTION}, request_text="Tell me about ABC"),
    )

    final = all_text(client.views[-1][1]["blocks"])
    assert "Unable to complete request" in final
    assert "boom" not in final


def test_interactions_require_a_valid_slack_signature():
    client, agent = RecordingClient(), RecordingAgent()
    app = app_with(agent, client, FakeSafety())

    response = post_interaction(
        app,
        home_payload({"action_id": SUBMIT_ACTION}),
        headers={
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=" + "0" * 64,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    assert response.status_code == 401
    assert agent.requests == [] and client.views == []


def test_interactions_from_unmapped_workspaces_are_ignored():
    client, agent = RecordingClient(), RecordingAgent()

    response = post_interaction(
        app_with(agent, client, FakeSafety(), team_map='{"T999":"tenant-z"}'),
        home_payload({"action_id": SUBMIT_ACTION}, request_text="Create Angel John"),
    )

    assert response.status_code == 200
    assert agent.requests == [] and client.views == []


def test_channel_mention_flow_still_posts_a_message_and_not_a_home_view():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()
    body = json.dumps(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "ts": "1.0",
                "text": "<@BOT> Create a contact named Angel John",
            },
        }
    ).encode()

    with TestClient(app_with(agent, client, safety)) as test_client:
        test_client.post("/api/v1/slack/events", content=body, headers=signed(body))

    assert client.messages == [
        ("C1", "Contact Angel John was created successfully in HubSpot.")
    ]
    assert client.views == []
    assert agent.requests[0].channel_id == "C1"


# --- Slack Web API adapter ----------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_home_view_calls_views_publish():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["json"] = json.loads(request.read())
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await SlackWebApiClient(Settings(slack_bot_token="xoxb-test"), http).publish_home_view(
            "U1", {"type": "home", "blocks": []}
        )

    assert captured == {
        "url": "https://slack.com/api/views.publish",
        "authorization": "Bearer xoxb-test",
        "json": {"user_id": "U1", "view": {"type": "home", "blocks": []}},
    }


@pytest.mark.asyncio
async def test_publish_home_view_surfaces_slack_error_code():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": False, "error": "invalid_blocks"})
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(IntegrationError, match="invalid_blocks"):
            await SlackWebApiClient(
                Settings(slack_bot_token="xoxb-test"), http
            ).publish_home_view("U1", {"type": "home", "blocks": []})


# --- Companies, deals and multi-step results ------------------------------------------


def test_titled_crm_results_render_their_title():
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text="✅ Company created: TechNova\n✅ Contact created: John Smith",
            request_id="r",
            result={"kind": "crm_records", "title": "CRM Updated"},
        )
    )

    assert blocks[0]["text"]["text"] == "✓  *CRM Updated*"
    assert "Company created: TechNova" in blocks[1]["text"]["text"]


def test_partial_completion_is_labelled():
    blocks = render_response_blocks(
        AgentResponse(status="partial", text="✅ Company created\n❌ Link failed", request_id="r")
    )

    assert blocks[0]["text"]["text"] == "⚠  *Partially completed*"


def test_ambiguous_records_ask_for_more_information():
    blocks = render_response_blocks(
        AgentResponse(status="company_ambiguous", text="Which one?", request_id="r")
    )

    assert blocks[0]["text"]["text"] == "ⓘ  *More information needed*"


def test_company_update_confirmation_names_the_company():
    blocks = render_response_blocks(
        AgentResponse(
            status="pending_confirmation",
            text="I found a request to update this HubSpot company:",
            request_id="r",
            result={
                "kind": "pending_confirmation",
                "action_id": ACTION_ID,
                "action_type": "update_company",
                "record_label": "company ABC Technologies",
            },
        )
    )

    button = next(block for block in blocks if block["type"] == "actions")["elements"][0]
    assert button["text"]["text"] == "Confirm Update"
    assert button["style"] == "primary"
    assert button["confirm"]["title"]["text"] == "Update company?"
    assert button["confirm"]["text"]["text"] == (
        "This will update HubSpot company ABC Technologies."
    )


def test_recent_activity_covers_plans_and_record_updates():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    recent = [
        RecentAction(
            "crm_plan", "completed", {"request": "Create TechNova and add John"}, now, now
        ),
        RecentAction("crm_plan", "reconciliation_required", {"request": "Big request"}, now, now),
        RecentAction(
            "update_deal", "pending", {"deal_name": "Renewal"}, now, now + timedelta(minutes=3)
        ),
        RecentAction("update_company", "completed", {"company_name": "TechNova"}, now, now),
    ]

    text = all_text(build_home_view(recent=recent, now=now)["blocks"])

    assert "✅ Completed *Create TechNova and add John*" in text
    assert "❗ Partially completed *Big request*" in text
    assert "🛡️ Update of deal *Renewal* is awaiting your confirmation" in text
    assert "✅ Updated company *TechNova*" in text


def test_existing_record_outcome_is_informational_not_an_error():
    blocks = render_response_blocks(
        AgentResponse(
            status="already_exists",
            text=(
                "Test AI Company already exists in HubSpot, so I used the existing company "
                "record. No duplicate was created."
            ),
            request_id="r",
            result={"kind": "existing_record", "title": "Company already exists"},
        )
    )

    assert blocks[0]["text"]["text"] == "ⓘ  *Company already exists*"
    assert "No duplicate was created." in blocks[1]["text"]["text"]
    assert "Unable to complete request" not in all_text(blocks)


def test_contact_flow_duplicate_error_is_still_an_error():
    blocks = render_response_blocks(
        AgentResponse(
            status="duplicate",
            text="A contact with the email a@b.co already exists in HubSpot.",
            request_id="r",
        )
    )

    assert blocks[0]["text"]["text"] == "⚠  *Unable to complete request*"
