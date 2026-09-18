import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from app.agent.schemas import AgentResponse
from app.api.app import create_app
from app.api.slack import get_agent, get_slack_client
from app.core.config import Settings
from app.integrations.slack.events import SlackRequestVerifier, SlackSignatureError


def signed_headers(body: bytes, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    )
    return {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={signature.hexdigest()}",
    }


def event_payload() -> dict[str, object]:
    return {
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


def test_valid_slack_message_routes_server_mapped_tenant_to_agent():
    received: dict[str, str] = {}

    class Agent:
        async def respond(self, request):
            received["tenant_id"] = request.tenant_id
            received["message"] = request.message
            return AgentResponse(status="ok", text="answer", request_id=request.request_id)

    class Client:
        async def post_message(self, channel: str, text: str) -> None:
            received["channel"] = channel
            received["text"] = text

    app = create_app(
        Settings(slack_signing_secret="signing", slack_team_tenant_map='{"T1":"tenant-a"}')
    )
    app.dependency_overrides[get_agent] = lambda: Agent()
    app.dependency_overrides[get_slack_client] = lambda: Client()
    body = json.dumps(event_payload()).encode()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/slack/events", content=body, headers=signed_headers(body, "signing")
        )

    assert response.status_code == 200
    assert received == {
        "tenant_id": "tenant-a",
        "message": "Tell me about Test AI Company",
        "channel": "C1",
        "text": "answer",
    }


def test_invalid_slack_signature_is_rejected_without_agent_routing():
    app = create_app(
        Settings(slack_signing_secret="signing", slack_team_tenant_map='{"T1":"tenant-a"}')
    )
    body = json.dumps(event_payload()).encode()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/slack/events",
            content=body,
            headers={
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": "v0=bad",
            },
        )

    assert response.status_code == 401
    assert "signing" not in response.text


def test_verifier_rejects_old_replay_requests():
    body = b"{}"
    timestamp = str(int(time.time()) - 301)
    signature = hmac.new(
        b"secret", b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()

    try:
        SlackRequestVerifier("secret").verify(
            {"x-slack-request-timestamp": timestamp, "x-slack-signature": f"v0={signature}"},
            body,
        )
    except SlackSignatureError:
        pass
    else:
        raise AssertionError("old Slack request should be rejected")
