import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from app.agent.schemas import AgentResponse
from app.api.app import create_app
from app.api.assistant import get_hubspot_connected
from app.api.slack import (
    get_action_safety,
    get_agent,
    get_conversation_history,
    get_slack_authorization,
    get_slack_client,
)
from app.core.config import Settings
from app.web.session import SESSION_COOKIE, WebIdentity, WebSessionSigner

SECRET = "signing"


def signed_headers(body: bytes, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    )
    return {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={signature.hexdigest()}",
    }


class FakeAgent:
    def __init__(self) -> None:
        self.requests = []

    async def respond(self, request):
        self.requests.append(request)
        return AgentResponse(status="ok", text="answer", request_id=request.request_id)


class FakeClient:
    async def post_message(self, channel: str, text: str, **kwargs) -> None:
        return None

    async def publish_home_view(self, user_id: str, view: dict) -> None:
        return None


class FakeAuth:
    def __init__(self, *, allowed: bool, role: str = "sales_agent") -> None:
        self.allowed = allowed
        self.role = role

    async def is_authorized(self, **kwargs):
        return self.allowed

    async def is_admin(self, **kwargs):
        return self.role == "admin"

    async def list_for_tenant(self, **kwargs):
        return [{
            "tenant_id": kwargs.get("tenant_id"),
            "slack_team_id": kwargs.get("slack_team_id") or "T1",
            "slack_user_id": "U1",
            "role": self.role,
            "active": self.allowed,
        }]


class RecordingClient(FakeClient):
    def __init__(self) -> None:
        self.messages = []
        self.views = []

    async def post_message(self, channel: str, text: str, **kwargs) -> None:
        self.messages.append((channel, text))

    async def publish_home_view(self, user_id: str, view: dict) -> None:
        self.views.append((user_id, view))


class FakeActionSafety:
    def __init__(self) -> None:
        self.calls = []

    async def recent_actions(self, **kwargs):
        self.calls.append(kwargs)
        return []


class FakeConversationHistory:
    def __init__(self) -> None:
        self.calls = []

    async def latest(self, **kwargs):
        self.calls.append(kwargs)
        return []


def app_with_auth(*, allowed: bool, role: str = "sales_agent"):
    app = create_app(
        Settings(slack_signing_secret=SECRET, slack_team_tenant_map='{"T1":"tenant-a"}')
    )
    app.dependency_overrides[get_agent] = lambda: FakeAgent()
    app.dependency_overrides[get_slack_client] = lambda: FakeClient()
    app.dependency_overrides[get_slack_authorization] = lambda: FakeAuth(allowed=allowed, role=role)
    app.dependency_overrides[get_action_safety] = lambda: FakeActionSafety()
    app.dependency_overrides[get_conversation_history] = lambda: FakeConversationHistory()
    return app


def test_authorized_sales_agent_is_allowed():
    app = app_with_auth(allowed=True)
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev1",
        "event": {
            "type": "message",
            "user": "U1",
            "channel": "C1",
            "text": "Tell me about Test AI Company",
        },
    }
    body = json.dumps(payload).encode()
    with TestClient(app) as client:
        response = client.post("/api/v1/slack/events", content=body, headers=signed_headers(body, SECRET))
    assert response.status_code == 200


def test_authorized_administrator_is_allowed():
    app = app_with_auth(allowed=True, role="admin")
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev2",
        "event": {
            "type": "app_home_opened",
            "user": "U1",
            "tab": "home",
        },
    }
    body = json.dumps(payload).encode()
    with TestClient(app) as client:
        response = client.post("/api/v1/slack/events", content=body, headers=signed_headers(body, SECRET))
    assert response.status_code == 200


def test_unauthorized_slack_user_is_rejected():
    app = app_with_auth(allowed=False)
    agent, slack_client = FakeAgent(), RecordingClient()
    app.dependency_overrides[get_agent] = lambda: agent
    app.dependency_overrides[get_slack_client] = lambda: slack_client
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev3",
        "event": {
            "type": "message",
            "user": "U2",
            "channel": "C1",
            "text": "hello",
        },
    }
    body = json.dumps(payload).encode()
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/slack/events", content=body, headers=signed_headers(body, SECRET)
        )
    assert response.status_code == 200
    assert agent.requests == []
    assert slack_client.messages == []


def test_inactive_user_is_rejected():
    app = app_with_auth(allowed=False)
    agent, slack_client = FakeAgent(), RecordingClient()
    app.dependency_overrides[get_agent] = lambda: agent
    app.dependency_overrides[get_slack_client] = lambda: slack_client
    payload = {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "view": {"type": "home", "blocks": []},
        "actions": [{"action_id": "home_submit", "action_ts": "123.45"}],
    }
    body = f"payload={json.dumps(payload)}".encode()
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/slack/interactions",
            content=body,
            headers=signed_headers(body, SECRET),
        )
    assert response.status_code == 200
    assert agent.requests == []
    assert slack_client.views == []


def test_cross_tenant_access_attempt_is_rejected():
    app = create_app(
        Settings(slack_signing_secret=SECRET, slack_team_tenant_map='{"T1":"tenant-a"}')
    )
    app.dependency_overrides[get_slack_authorization] = lambda: FakeAuth(allowed=False)
    app.dependency_overrides[get_agent] = lambda: FakeAgent()
    app.dependency_overrides[get_slack_client] = lambda: FakeClient()
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev4",
        "event": {"type": "message", "user": "U1", "channel": "C1", "text": "Hi"},
    }
    body = json.dumps(payload).encode()
    with TestClient(app) as client:
        response = client.post("/api/v1/slack/events", content=body, headers=signed_headers(body, SECRET))
    assert response.status_code == 200


def test_unauthorized_signed_web_session_is_rejected(monkeypatch):
    app = create_app(
        Settings(slack_signing_secret=SECRET, slack_team_tenant_map='{"T1":"tenant-a"}')
    )
    app.dependency_overrides[get_hubspot_connected] = lambda: True

    async def fake_is_authorized(self, *, tenant_id, slack_team_id, slack_user_id):
        return False

    monkeypatch.setattr(
        "app.services.slack_authorized_users.SlackAuthorizationService.is_authorized",
        fake_is_authorized,
    )
    token = WebSessionSigner(SECRET).issue(
        WebIdentity(tenant_id="tenant-a", user_id="U1", team_id="T1"),
        purpose="session",
        ttl_seconds=600,
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/assistant/session", cookies={SESSION_COOKIE: token})
    assert response.status_code == 403
    assert response.json()["code"] == "web_session_not_authorized"


def test_existing_authorized_user_functionality_still_allows_work():
    app = app_with_auth(allowed=True)
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev5",
        "event": {"type": "message", "user": "U1", "channel": "C1", "text": "hello"},
    }
    body = json.dumps(payload).encode()
    with TestClient(app) as client:
        response = client.post("/api/v1/slack/events", content=body, headers=signed_headers(body, SECRET))
    assert response.status_code == 200


def test_signed_web_session_without_workspace_claim_is_denied():
    app = app_with_auth(allowed=True)
    app.dependency_overrides[get_hubspot_connected] = lambda: True
    token = WebSessionSigner(SECRET).issue(
        WebIdentity(tenant_id="tenant-a", user_id="U1"),
        purpose="session",
        ttl_seconds=600,
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/assistant/session", cookies={SESSION_COOKIE: token})
    assert response.status_code == 403
    assert response.json()["code"] == "web_session_not_authorized"


def test_signed_web_session_with_workspace_mapped_to_another_tenant_is_denied():
    app = app_with_auth(allowed=True)
    app.dependency_overrides[get_hubspot_connected] = lambda: True
    token = WebSessionSigner(SECRET).issue(
        WebIdentity(tenant_id="tenant-b", user_id="U1", team_id="T1"),
        purpose="session",
        ttl_seconds=600,
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/assistant/session", cookies={SESSION_COOKIE: token})
    assert response.status_code == 403


def test_web_authorization_database_failure_fails_closed():
    class FailingAuth:
        async def is_authorized(self, **kwargs):
            raise RuntimeError("database details must not be returned")

    app = app_with_auth(allowed=True)
    app.dependency_overrides[get_slack_authorization] = lambda: FailingAuth()
    app.dependency_overrides[get_hubspot_connected] = lambda: True
    token = WebSessionSigner(SECRET).issue(
        WebIdentity(tenant_id="tenant-a", user_id="U1", team_id="T1"),
        purpose="session",
        ttl_seconds=600,
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/assistant/session", cookies={SESSION_COOKIE: token})
    assert response.status_code == 503
    assert "database details" not in response.text


def test_slack_authorization_database_failure_does_not_schedule_agent_work():
    class FailingAuth:
        async def is_authorized(self, **kwargs):
            raise RuntimeError("database unavailable")

    app = create_app(
        Settings(slack_signing_secret=SECRET, slack_team_tenant_map='{"T1":"tenant-a"}')
    )
    agent, client = FakeAgent(), RecordingClient()
    app.dependency_overrides[get_agent] = lambda: agent
    app.dependency_overrides[get_slack_client] = lambda: client
    app.dependency_overrides[get_slack_authorization] = lambda: FailingAuth()
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev-db-failure",
        "event": {"type": "message", "user": "U1", "channel": "C1", "text": "hello"},
    }
    body = json.dumps(payload).encode()
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/slack/events", content=body, headers=signed_headers(body, SECRET)
        )
    assert response.status_code == 200
    assert agent.requests == []
    assert client.messages == []


def test_authorization_is_rechecked_when_slack_background_work_runs():
    class RevokedBetweenChecks:
        def __init__(self):
            self.results = iter([True, False])
            self.calls = 0

        async def is_authorized(self, **kwargs):
            self.calls += 1
            return next(self.results)

    auth = RevokedBetweenChecks()
    app = create_app(
        Settings(slack_signing_secret=SECRET, slack_team_tenant_map='{"T1":"tenant-a"}')
    )
    agent, client = FakeAgent(), RecordingClient()
    app.dependency_overrides[get_agent] = lambda: agent
    app.dependency_overrides[get_slack_client] = lambda: client
    app.dependency_overrides[get_slack_authorization] = lambda: auth
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev-revoked",
        "event": {"type": "message", "user": "U1", "channel": "C1", "text": "hello"},
    }
    body = json.dumps(payload).encode()
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/slack/events", content=body, headers=signed_headers(body, SECRET)
        )
    assert response.status_code == 200
    assert auth.calls == 2
    assert agent.requests == []
    assert client.messages == []


class RevokedAfterScheduling:
    def __init__(self):
        self.results = iter([True, False])
        self.calls = []

    async def is_authorized(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.results)


def test_app_home_publication_rechecks_authorization_before_background_work():
    auth = RevokedAfterScheduling()
    client = RecordingClient()
    app = app_with_auth(allowed=True)
    app.dependency_overrides[get_slack_authorization] = lambda: auth
    app.dependency_overrides[get_slack_client] = lambda: client
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev-home-revoked",
        "event": {"type": "app_home_opened", "user": "U1", "tab": "home"},
    }
    body = json.dumps(payload).encode()
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/slack/events", content=body, headers=signed_headers(body, SECRET)
        )
    assert response.status_code == 200
    assert len(auth.calls) == 2
    assert auth.calls == [
        {"tenant_id": "tenant-a", "slack_team_id": "T1", "slack_user_id": "U1"},
        {"tenant_id": "tenant-a", "slack_team_id": "T1", "slack_user_id": "U1"},
    ]
    assert client.views == []


def test_home_interaction_rechecks_authorization_before_background_work():
    auth = RevokedAfterScheduling()
    client = RecordingClient()
    agent = FakeAgent()
    app = app_with_auth(allowed=True)
    app.dependency_overrides[get_slack_authorization] = lambda: auth
    app.dependency_overrides[get_slack_client] = lambda: client
    app.dependency_overrides[get_agent] = lambda: agent
    payload = {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "view": {"type": "home", "blocks": []},
        "actions": [{"action_id": "home_submit", "action_ts": "1712345678.000200"}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/slack/interactions",
            content=body,
            headers={
                **signed_headers(body, SECRET),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    assert response.status_code == 200
    assert len(auth.calls) == 2
    assert agent.requests == []
    assert client.views == []
