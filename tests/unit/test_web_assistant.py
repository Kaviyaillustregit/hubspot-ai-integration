import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.schemas import AgentResponse
from app.api.app import create_app
from app.api.assistant import get_hubspot_connected, recent_item
from app.api.slack import get_action_safety, get_agent, get_slack_client
from app.core.config import Settings
from app.integrations.slack.home import build_home_view
from app.services.action_safety import RecentAction
from app.web.session import (
    LINK_TTL_SECONDS,
    SESSION_COOKIE,
    WebIdentity,
    WebSessionSigner,
)

SECRET = "signing"
IDENTITY = WebIdentity(tenant_id="tenant-a", user_id="U1")
STATIC = Path(__file__).resolve().parents[2] / "src" / "app" / "web"


def signer() -> WebSessionSigner:
    return WebSessionSigner(SECRET)


def session_cookie(identity: WebIdentity = IDENTITY) -> dict[str, str]:
    token = signer().issue(identity, purpose="session", ttl_seconds=3600)
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


class RecordingAgent:
    def __init__(self, response: AgentResponse | Exception | None = None) -> None:
        self.requests = []
        self.response = response

    async def respond(self, request):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response or AgentResponse(
            status="ok",
            text="✅ Company created: Demo AI Company 001",
            request_id=request.request_id,
            result={
                "kind": "crm_records",
                "title": "Company created",
                "message": "Demo AI Company 001 was successfully added to HubSpot.",
            },
            cards=[
                {
                    "kind": "company",
                    "title": "Company created",
                    "name": "Demo AI Company 001",
                    "hubspot_id": "123",
                }
            ],
        )


class FakeSafety:
    def __init__(self, recent=None) -> None:
        self.recent = recent or []
        self.lookups = []

    async def recent_actions(self, *, tenant_id, actor_id, limit=5):
        self.lookups.append((tenant_id, actor_id))
        return self.recent


def app_with(agent=None, safety=None, *, connected=True, **settings):
    app = create_app(
        Settings(
            slack_signing_secret=SECRET,
            slack_team_tenant_map='{"T1":"tenant-a"}',
            **settings,
        )
    )
    app.dependency_overrides[get_agent] = lambda: agent or RecordingAgent()
    app.dependency_overrides[get_action_safety] = lambda: safety or FakeSafety()
    app.dependency_overrides[get_hubspot_connected] = lambda: connected
    return app


# ------------------------------------------------------------------------- signing


def test_signed_tokens_round_trip_and_bind_purpose():
    token = signer().issue(IDENTITY, purpose="link", ttl_seconds=60)

    assert signer().verify(token, purpose="link") == IDENTITY
    assert signer().verify(token, purpose="session") is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda token: token[:-2] + ("AA" if not token.endswith("AA") else "BB"),
        lambda token: "e30" + token[3:],
        lambda token: token.replace(".", ""),
        lambda token: "",
        lambda token: "not.a-token",
    ],
)
def test_tampered_tokens_are_rejected(mutate):
    token = signer().issue(IDENTITY, purpose="session", ttl_seconds=60)

    assert signer().verify(mutate(token), purpose="session") is None


def test_expired_and_foreign_tokens_are_rejected():
    expired = signer().issue(IDENTITY, purpose="session", ttl_seconds=60, now=0)
    assert signer().verify(expired, purpose="session") is None

    foreign = WebSessionSigner("another-secret").issue(IDENTITY, purpose="session", ttl_seconds=60)
    assert signer().verify(foreign, purpose="session") is None


# ---------------------------------------------------------------------- page + login


def test_page_is_served_with_strict_security_headers():
    with TestClient(app_with()) as client:
        response = client.get("/assistant")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy and "connect-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "Ask HubSpot AI anything..." in response.text
    assert "Revenue Intelligence Assistant" in response.text


def test_slack_link_is_exchanged_for_an_http_only_session_cookie():
    link = signer().issue(IDENTITY, purpose="link", ttl_seconds=LINK_TTL_SECONDS)

    with TestClient(app_with()) as client:
        response = client.get(f"/assistant?token={link}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/assistant"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    session_token = cookie.split(";")[0].split("=", 1)[1]
    assert signer().verify(session_token, purpose="session") == IDENTITY


@pytest.mark.parametrize(
    "token",
    [
        "garbage",
        WebSessionSigner(SECRET).issue(IDENTITY, purpose="session", ttl_seconds=60),
        WebSessionSigner(SECRET).issue(IDENTITY, purpose="link", ttl_seconds=60, now=0),
    ],
)
def test_invalid_or_expired_links_do_not_sign_in(token):
    with TestClient(app_with()) as client:
        response = client.get(f"/assistant?token={token}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/assistant?link=expired"
    assert "set-cookie" not in response.headers


def test_static_assets_are_served():
    with TestClient(app_with()) as client:
        script = client.get("/assistant/static/app.js")
        render = client.get("/assistant/static/render.js")
        styles = client.get("/assistant/static/app.css")

    assert script.status_code == render.status_code == styles.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert "text/css" in styles.headers["content-type"]


def test_frontend_only_talks_to_this_backend():
    sources = [
        path.read_text(encoding="utf-8")
        for path in [STATIC / "index.html", *(STATIC / "static").iterdir()]
        if path.is_file()
    ]

    for source in sources:
        assert "hubapi.com" not in source
        assert "api.hubspot" not in source
        assert "innerHTML" not in source
    assert '"/api/v1/assistant"' in (STATIC / "static" / "app.js").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------ API


def test_session_requires_sign_in():
    with TestClient(app_with()) as client:
        response = client.get("/api/v1/assistant/session")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_session_reports_connection_and_recent_activity():
    now = datetime.now(UTC)
    safety = FakeSafety(
        [
            RecentAction(
                "crm_plan", "completed", {"request": "Create Demo AI Company 001"}, now, now
            )
        ]
    )

    with TestClient(app_with(safety=safety, connected=True)) as client:
        response = client.get("/api/v1/assistant/session", headers=session_cookie())

    body = response.json()
    assert response.status_code == 200
    assert body["user_id"] == "U1"
    assert body["hubspot_connected"] is True
    assert body["recent"][0]["title"] == "Request completed"
    assert body["recent"][0]["detail"] == "Create Demo AI Company 001"
    assert body["recent"][0]["tone"] == "success"
    assert safety.lookups == [("tenant-a", "U1")]


def post_message(app, payload: dict, headers: dict[str, str] | None = None):
    with TestClient(app) as client:
        return client.post(
            "/api/v1/assistant/messages",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json", **(headers or {})},
        )


def test_message_goes_through_the_existing_agent_with_slack_identity():
    agent = RecordingAgent()

    response = post_message(
        app_with(agent),
        {
            "message": "Create a company called Demo AI Company 001.",
            "client_message_id": "c0ffee-1234",
        },
        session_cookie(),
    )

    assert response.status_code == 200
    [request] = agent.requests
    assert request.tenant_id == "tenant-a"
    assert request.actor_id == "U1"
    assert request.message == "Create a company called Demo AI Company 001."
    assert request.channel_id == "web:U1"
    assert request.message_ts == "c0ffee-1234"
    body = response.json()
    assert body["status"] == "ok"
    assert body["result"]["title"] == "Company created"
    assert body["cards"][0]["name"] == "Demo AI Company 001"


def test_message_requires_a_session_not_a_link_token():
    agent = RecordingAgent()
    link = signer().issue(IDENTITY, purpose="link", ttl_seconds=60)

    response = post_message(
        app_with(agent),
        {"message": "Create a company", "client_message_id": "abcdef12"},
        {"Cookie": f"{SESSION_COOKIE}={link}"},
    )

    assert response.status_code == 401
    assert agent.requests == []


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "", "client_message_id": "abcdef12"},
        {"message": "hi", "client_message_id": "bad id!"},
        {"message": "hi", "client_message_id": "abcdef12", "tenant_id": "tenant-b"},
        {"message": "x" * 3001, "client_message_id": "abcdef12"},
    ],
)
def test_invalid_messages_are_rejected_before_the_agent(payload):
    agent = RecordingAgent()

    response = post_message(app_with(agent), payload, session_cookie())

    assert response.status_code == 422
    assert agent.requests == []


def test_non_json_posts_are_rejected():
    agent = RecordingAgent()
    with TestClient(app_with(agent)) as client:
        response = client.post(
            "/api/v1/assistant/messages",
            content="message=Create+a+company&client_message_id=abcdef12",
            headers={"Content-Type": "application/x-www-form-urlencoded", **session_cookie()},
        )

    assert response.status_code == 422
    assert agent.requests == []


def test_agent_failure_returns_a_safe_error_reply():
    response = post_message(
        app_with(RecordingAgent(RuntimeError("secret detail"))),
        {"message": "Create a company", "client_message_id": "abcdef12"},
        session_cookie(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert "secret detail" not in response.text


# ------------------------------------------------------------------- Slack entry point


def test_slack_home_is_an_in_slack_conversation_without_external_navigation():
    view = build_home_view(recent=[])
    text = json.dumps(view)

    assert view["type"] == "home"
    assert any(block["type"] == "input" for block in view["blocks"])
    assert "Open HubSpot AI" not in text
    assert "/assistant" not in text
    assert '"url"' not in text


def test_app_home_opened_publishes_the_conversation_inside_slack():
    views = []

    class Client:
        async def publish_home_view(self, user_id, view):
            views.append((user_id, view))

    app = app_with()
    app.dependency_overrides[get_slack_client] = lambda: Client()
    body = json.dumps(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {"type": "app_home_opened", "user": "U1", "tab": "home"},
        }
    ).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        SECRET.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()

    with TestClient(app) as client:
        client.post(
            "/api/v1/slack/events",
            content=body,
            headers={
                "X-Slack-Request-Timestamp": timestamp,
                "X-Slack-Signature": f"v0={signature}",
            },
        )

    assert views[0][0] == "U1"
    assert any(block["type"] == "input" for block in views[0][1]["blocks"])
    assert "/assistant" not in json.dumps(views[0][1])


# --------------------------------------------------------------------- recent activity


@pytest.mark.parametrize(
    ("action", "title", "tone"),
    [
        (
            ("create_contact", "completed", {"firstname": "John", "lastname": "Smith"}),
            "Contact created",
            "success",
        ),
        (
            ("crm_plan", "reconciliation_required", {"request": "Big request"}),
            "Partially completed",
            "warning",
        ),
        (("crm_plan", "failed", {"request": "Bad request"}), "Request not completed", "error"),
        (("update_deal", "pending", {"deal_name": "Renewal"}), "Awaiting confirmation", "pending"),
        (
            ("update_company", "completed", {"company_name": "TechNova"}),
            "Company updated",
            "success",
        ),
    ],
)
def test_recent_activity_is_summarized_in_plain_language(action, title, tone):
    now = datetime.now(UTC)
    item = recent_item(RecentAction(*action, now, now + timedelta(minutes=5)), now)

    assert (item.title, item.tone) == (title, tone)


def test_expired_pending_action_is_not_shown_as_awaiting():
    now = datetime.now(UTC)
    item = recent_item(
        RecentAction("delete_contact", "pending", {"contact_id": "9"}, now, now - timedelta(1)),
        now,
    )

    assert item.title == "Delete expired"
    assert item.tone == "info"
