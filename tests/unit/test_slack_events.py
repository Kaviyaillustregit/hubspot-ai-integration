import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from app.agent.schemas import AgentResponse, CRMIntentExtraction
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.service import AIService
from app.api.app import create_app
from app.api.slack import get_agent, get_slack_client
from app.core.config import Settings
from app.integrations.hubspot.models import HubSpotContact
from app.integrations.slack.events import (
    SlackRequestVerifier,
    SlackSignatureError,
    parse_message,
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

def test_slack_contact_create_request_creates_contact_directly():
    received: dict[str, str] = {}
    started: dict[str, object] = {}
    created: dict[str, object] = {}

    class Provider:
        async def generate_structured(
            self,
            *,
            prompt_name,
            variables,
            output_schema,
        ):
            assert prompt_name == "crm-intent/v2"
            return CRMIntentExtraction(
                intent="create_contact",
                first_name="Arun",
                last_name="Kumar",
                email="arun@test.com",
                confidence=0.95,
            )

    class ActionSafety:
        async def start_direct_action(self, **kwargs):
            started.update(kwargs)
            return type("Claim", (), {"action_id": "direct-1", "claimed": True})()

        async def complete_action(self, **kwargs) -> None:
            created["completed_action_id"] = kwargs["action_id"]

        async def create_pending_action(self, **kwargs) -> str:
            raise AssertionError("contact creation must not require confirmation")

    class Companies:
        pass

    class Contacts:
        async def create_contact(self, context, *, properties):
            assert context.tenant_id == "tenant-a"
            created["properties"] = properties
            return HubSpotContact(id="contact-2", properties=properties)

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
    payload["event"]["text"] = "Please add Arun Kumar, email arun@test.com, as a contact"
    payload["event"]["ts"] = "1712345678.000100"

    body = json.dumps(payload).encode()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/slack/events",
            content=body,
            headers=signed_headers(body, "signing"),
        )

    assert response.status_code == 200

    assert started["tenant_id"] == "tenant-a"
    assert started["actor_id"] == "U1"
    assert started["action_type"] == "create_contact"
    assert started["idempotency_key"] == "slack-message:C1:1712345678.000100"
    assert created == {
        "properties": {
            "firstname": "Arun",
            "lastname": "Kumar",
            "email": "arun@test.com",
        },
        "completed_action_id": "direct-1",
    }

    assert received["channel"] == "C1"
    assert "Contact Arun Kumar was created successfully in HubSpot." in received["text"]
    assert "confirm" not in received["text"].lower()


def test_duplicate_slack_delivery_is_not_replied_to_twice():
    posted: list[str] = []

    class Agent:
        async def respond(self, request):
            assert request.channel_id == "C1"
            assert request.message_ts == "1712345678.000100"
            assert request.event_id == "Ev1"
            return AgentResponse(
                status="duplicate_request",
                text="This message was already processed.",
                request_id=request.request_id,
            )

    class Client:
        async def post_message(self, channel: str, text: str) -> None:
            posted.append(text)

    app = create_app(
        Settings(slack_signing_secret="signing", slack_team_tenant_map='{"T1":"tenant-a"}')
    )
    app.dependency_overrides[get_agent] = lambda: Agent()
    app.dependency_overrides[get_slack_client] = lambda: Client()
    payload = event_payload()
    payload["event"]["ts"] = "1712345678.000100"
    body = json.dumps(payload).encode()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/slack/events", content=body, headers=signed_headers(body, "signing")
        )

    assert response.status_code == 200
    assert posted == []

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
                    "action_type": "create_contact",
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


def test_parse_message_keeps_slack_message_timestamp():
    payload = event_payload()
    payload["event"]["ts"] = "1712345678.000100"

    parsed = parse_message(payload)

    assert parsed is not None
    assert parsed.ts == "1712345678.000100"
    assert parse_message(event_payload()).ts is None  # type: ignore[union-attr]
