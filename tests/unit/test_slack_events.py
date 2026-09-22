import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from app.agent.schemas import AgentResponse
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.service import AIService
from app.api.app import create_app
from app.api.slack import get_agent, get_slack_client
from app.core.config import Settings
from app.integrations.slack.events import (
    SlackRequestVerifier,
    SlackSignatureError,
)


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


def test_url_verification_returns_slack_challenge_after_signature_validation():
    app = create_app(Settings(slack_signing_secret="signing"))
    body = json.dumps({"type": "url_verification", "challenge": "challenge-value"}).encode()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/slack/events", content=body, headers=signed_headers(body, "signing")
        )

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-value"}


def test_app_mention_routes_to_agent():
    received: dict[str, str] = {}

    class Agent:
        async def respond(self, request):
            received["tenant_id"] = request.tenant_id
            return AgentResponse(status="ok", text="answer", request_id=request.request_id)

    class Client:
        async def post_message(self, channel: str, text: str) -> None:
            received["channel"] = channel

    app = create_app(
        Settings(slack_signing_secret="signing", slack_team_tenant_map='{"T1":"tenant-a"}')
    )
    app.dependency_overrides[get_agent] = lambda: Agent()
    app.dependency_overrides[get_slack_client] = lambda: Client()
    payload = event_payload()
    payload["event"] = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "text": "<@bot> Tell me about Test AI Company",
    }
    body = json.dumps(payload).encode()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/slack/events", content=body, headers=signed_headers(body, "signing")
        )

    assert response.status_code == 200
    assert received == {"tenant_id": "tenant-a", "channel": "C1"}


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

def test_slack_contact_create_request_creates_pending_action_and_confirmation():
    received: dict[str, str] = {}
    created_action: dict[str, object] = {}

    class Provider:
        async def generate_structured(
            self,
            *,
            prompt_name,
            variables,
            output_schema,
        ):
            raise AssertionError(
                "AI provider should not be called for contact creation"
            )

    class ActionSafety:
        async def create_pending_action(
            self,
            *,
            tenant_id: str,
            actor_id: str,
            action_type: str,
            resource_type: str,
            payload: dict[str, object],
        ) -> str:
            created_action.update(
                {
                    "tenant_id": tenant_id,
                    "actor_id": actor_id,
                    "action_type": action_type,
                    "resource_type": resource_type,
                    "payload": payload,
                }
            )
            return "0123456789abcdef0123456789abcdef"

    class Companies:
        pass

    class Contacts:
        pass

    class Client:
        async def post_message(
            self,
            channel: str,
            text: str,
        ) -> None:
            received["channel"] = channel
            received["text"] = text

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(),
            Contacts(),
        ),
        AIService(Provider()),
        ActionSafety(),  # type: ignore[arg-type]
    )

    app = create_app(
        Settings(
            slack_signing_secret="signing",
            slack_team_tenant_map='{"T1":"tenant-a"}',
        )
    )
    app.dependency_overrides[get_agent] = lambda: agent
    app.dependency_overrides[get_slack_client] = lambda: Client()

    payload = event_payload()
    payload["event"]["text"] = (
        "Create a contact firstname Arun "
        "lastname Kumar email arun@test.com"
    )

    body = json.dumps(payload).encode()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/slack/events",
            content=body,
            headers=signed_headers(body, "signing"),
        )

    assert response.status_code == 200

    assert created_action == {
        "tenant_id": "tenant-a",
        "actor_id": "U1",
        "action_type": "create_contact",
        "resource_type": "contact",
        "payload": {
            "email": "arun@test.com",
            "firstname": "Arun",
            "lastname": "Kumar",
        },
    }

    assert received["channel"] == "C1"
    assert "pending_confirmation" not in received["text"]
    assert "arun@test.com" in received["text"]
    assert "0123456789abcdef0123456789abcdef" in received["text"]
    assert "confirm" in received["text"].lower()

def test_slack_confirmation_creates_hubspot_contact_and_completes_action():
    received: dict[str, str] = {}
    completed: dict[str, object] = {}

    class ActionSafety:
        async def confirm_and_claim_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_fingerprint: str,
        ):
            assert action_id == "0123456789abcdef0123456789abcdef"
            assert tenant_id == "tenant-a"
            assert actor_id == "U1"
            assert request_fingerprint == (
                "confirm 0123456789abcdef0123456789abcdef"
            )

            return type(
                "ConfirmedAction",
                (),
                {
                    "id": action_id,
                    "payload": {
                        "email": "arun@test.com",
                        "firstname": "Arun",
                        "lastname": "Kumar",
                    },
                },
            )()

        async def complete_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            resource_id: str | None,
            result: dict[str, object],
        ) -> None:
            completed.update(
                {
                    "action_id": action_id,
                    "tenant_id": tenant_id,
                    "actor_id": actor_id,
                    "request_id": request_id,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "result": result,
                }
            )

        async def fail_action(
            self,
            *,
            action_id: str,
            tenant_id: str,
            actor_id: str,
            request_id: str,
            resource_type: str,
            error_code: str,
        ) -> None:
            raise AssertionError(
                "fail_action should not be called on successful creation"
            )

    class Companies:
        pass

    class Contacts:
        async def create_contact(
            self,
            context,
            *,
            properties: dict[str, str | None],
        ):
            assert context.tenant_id == "tenant-a"
            assert properties == {
                "email": "arun@test.com",
                "firstname": "Arun",
                "lastname": "Kumar",
            }

            from app.integrations.hubspot.models import HubSpotContact

            return HubSpotContact(
                id="contact-2",
                properties=properties,
            )

    class Client:
        async def post_message(
            self,
            channel: str,
            text: str,
        ) -> None:
            received["channel"] = channel
            received["text"] = text

    class Provider:
        async def generate_structured(
            self,
            *,
            prompt_name,
            variables,
            output_schema,
        ):
            raise AssertionError(
                "AI provider should not be called for confirmation"
            )

    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(
            Companies(),
            Contacts(),
        ),
        AIService(Provider()),
        ActionSafety(),  # type: ignore[arg-type]
    )

    app = create_app(
        Settings(
            slack_signing_secret="signing",
            slack_team_tenant_map='{"T1":"tenant-a"}',
        )
    )
    app.dependency_overrides[get_agent] = lambda: agent
    app.dependency_overrides[get_slack_client] = lambda: Client()

    payload = event_payload()
    payload["event"]["text"] = (
        "confirm 0123456789abcdef0123456789abcdef"
    )

    body = json.dumps(payload).encode()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/slack/events",
            content=body,
            headers=signed_headers(body, "signing"),
        )

    assert response.status_code == 200
    assert received["channel"] == "C1"
    assert "Contact created successfully in HubSpot." in received["text"]
    assert "contact-2" in received["text"]

    assert completed == {
        "action_id": "0123456789abcdef0123456789abcdef",
        "tenant_id": "tenant-a",
        "actor_id": "U1",
        "request_id": completed["request_id"],
        "resource_type": "contact",
        "resource_id": "contact-2",
        "result": {
            "contact_id": "contact-2",
        },
    }