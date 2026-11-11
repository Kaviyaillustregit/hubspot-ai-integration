import pytest
from unittest.mock import AsyncMock
from uuid import uuid4
from datetime import UTC, datetime, timedelta

from app.agent.schemas import AgentRequest
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.integrations.hubspot.models import HubSpotContact
from app.services.action_safety import ActionSafetyService, ConfirmedAction


class DummyAIService:
    async def generate(self, prompt_name: str, variables: dict, output_schema=None):
        return None


class FakeActionSafetyWithChannel:
    def __init__(self):
        self.actions = []
        self.confirmed = []

    async def create_pending_action(
        self, tenant_id: str, actor_id: str, action_type: str, resource_type: str, payload: dict
    ):
        action_id = uuid4().hex
        self.actions.append({
            "id": action_id,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "action_type": action_type,
            "resource_type": resource_type,
            "payload": payload,
            "status": "pending",
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        })
        return action_id

    async def get_latest_pending_action(self, tenant_id: str, actor_id: str, channel_id: str | None = None):
        for act in reversed(self.actions):
            if act["tenant_id"] == tenant_id and act["actor_id"] == actor_id and act["status"] == "pending":
                act_channel = act["payload"].get("_channel_id")
                if act_channel and channel_id and act_channel != channel_id:
                    return None
                return ConfirmedAction(
                    id=act["id"],
                    tenant_id=act["tenant_id"],
                    actor_id=act["actor_id"],
                    action_type=act["action_type"],
                    resource_type=act["resource_type"],
                    payload=dict(act["payload"]),
                )
        return None

    async def confirm_and_claim_action(self, action_id: str, tenant_id: str, actor_id: str, request_fingerprint: str):
        for act in self.actions:
            if act["id"] == action_id and act["tenant_id"] == tenant_id and act["actor_id"] == actor_id and act["status"] == "pending":
                act["status"] = "confirmed"
                self.confirmed.append(action_id)
                return ConfirmedAction(
                    id=act["id"],
                    tenant_id=act["tenant_id"],
                    actor_id=act["actor_id"],
                    action_type=act["action_type"],
                    resource_type=act["resource_type"],
                    payload=dict(act["payload"]),
                )
        return None

    async def complete_action(self, **kwargs):
        pass

    async def fail_action(self, **kwargs):
        pass

    async def get_action_record(self, action_id: str, tenant_id: str, actor_id: str | None = None):
        for act in self.actions:
            if act["id"] == action_id:
                is_expired = act["expires_at"] <= datetime.now(UTC)
                return {
                    "id": act["id"],
                    "status": act["status"],
                    "is_expired": is_expired,
                }
        return None


@pytest.fixture
def mock_tools():
    tools = AsyncMock(spec=HubSpotToolRegistry)
    tools.get_contact.return_value = HubSpotContact(
        id="12345",
        properties={"firstname": "Jane", "lastname": "Doe", "email": "jane@example.com"},
    )
    tools.update_contact.return_value = HubSpotContact(
        id="12345",
        properties={"firstname": "Jane", "lastname": "Doe", "email": "jane@example.com", "jobtitle": "VP Engineering"},
    )
    tools.hubspot_record_urls.return_value = ["https://app.hubspot.com/contacts/tenant-1/contact/12345"]
    return tools


@pytest.mark.asyncio
async def test_natural_confirmation_succeeds_in_same_channel(mock_tools):
    safety = FakeActionSafetyWithChannel()
    agent = AccountIntelligenceAgent(tools=mock_tools, ai_service=DummyAIService(), action_safety=safety)

    # 1. User proposes update in channel C1
    res1 = await agent.respond(
        AgentRequest(
            tenant_id="tenant-1",
            actor_id="user-1",
            channel_id="C1",
            message="update contact 12345 job title VP Engineering",
            request_id="req-1",
        )
    )
    assert res1.status == "pending_confirmation"

    # 2. User confirms with "yes" in same channel C1
    res2 = await agent.respond(
        AgentRequest(
            tenant_id="tenant-1",
            actor_id="user-1",
            channel_id="C1",
            message="yes",
            request_id="req-2",
        )
    )
    assert res2.status == "ok"
    assert "Contact updated successfully" in res2.text


@pytest.mark.asyncio
async def test_natural_confirmation_isolated_across_different_channels(mock_tools):
    safety = FakeActionSafetyWithChannel()
    agent = AccountIntelligenceAgent(tools=mock_tools, ai_service=DummyAIService(), action_safety=safety)

    # 1. User proposes update in channel C1
    res1 = await agent.respond(
        AgentRequest(
            tenant_id="tenant-1",
            actor_id="user-1",
            channel_id="C1",
            message="update contact 12345 job title VP Engineering",
            request_id="req-1",
        )
    )
    assert res1.status == "pending_confirmation"

    # 2. Same user says "yes" in a DIFFERENT channel C2
    res2 = await agent.respond(
        AgentRequest(
            tenant_id="tenant-1",
            actor_id="user-1",
            channel_id="C2",
            message="yes",
            request_id="req-2",
        )
    )
    # Must NOT confirm the action from C1
    assert res2.status != "ok"
    assert len(safety.confirmed) == 0


@pytest.mark.asyncio
async def test_natural_confirmation_app_home_succeeds(mock_tools):
    safety = FakeActionSafetyWithChannel()
    agent = AccountIntelligenceAgent(tools=mock_tools, ai_service=DummyAIService(), action_safety=safety)

    # 1. User proposes update in App Home
    app_home_channel = "apphome:user-1"
    res1 = await agent.respond(
        AgentRequest(
            tenant_id="tenant-1",
            actor_id="user-1",
            channel_id=app_home_channel,
            message="update contact 12345 job title VP Engineering",
            request_id="req-1",
        )
    )
    assert res1.status == "pending_confirmation"

    # 2. User says "proceed" in App Home
    res2 = await agent.respond(
        AgentRequest(
            tenant_id="tenant-1",
            actor_id="user-1",
            channel_id=app_home_channel,
            message="proceed",
            request_id="req-2",
        )
    )
    assert res2.status == "ok"
    assert "Contact updated successfully" in res2.text


@pytest.mark.asyncio
async def test_unrelated_yes_does_not_confirm_without_pending_action(mock_tools):
    safety = FakeActionSafetyWithChannel()
    agent = AccountIntelligenceAgent(tools=mock_tools, ai_service=DummyAIService(), action_safety=safety)

    res = await agent.respond(
        AgentRequest(
            tenant_id="tenant-1",
            actor_id="user-1",
            channel_id="C1",
            message="yes",
            request_id="req-1",
        )
    )
    assert res.status != "ok"
    assert len(safety.confirmed) == 0


@pytest.mark.asyncio
async def test_button_confirmation_works_regardless_of_channel(mock_tools):
    safety = FakeActionSafetyWithChannel()
    agent = AccountIntelligenceAgent(tools=mock_tools, ai_service=DummyAIService(), action_safety=safety)

    # 1. Propose update
    res1 = await agent.respond(
        AgentRequest(
            tenant_id="tenant-1",
            actor_id="user-1",
            channel_id="C1",
            message="update contact 12345 job title VP Engineering",
            request_id="req-1",
        )
    )
    action_id = res1.result["action_id"]

    # 2. Button callback passes "confirm <action_id>"
    res2 = await agent.respond(
        AgentRequest(
            tenant_id="tenant-1",
            actor_id="user-1",
            channel_id="C2",
            message=f"confirm {action_id}",
            request_id="req-2",
        )
    )
    assert res2.status == "ok"
    assert action_id in safety.confirmed

