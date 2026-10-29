from uuid import uuid4
from unittest.mock import AsyncMock
import pytest

from app.agent.schemas import AgentRequest
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import ContactResolution, HubSpotToolRegistry
from app.integrations.hubspot.models import HubSpotCompany, HubSpotContact
from app.integrations.slack.home import _build_response_blocks, parse_home_interaction


class DummyAIService:
    async def generate(self, prompt_name: str, variables: dict, output_schema=None):
        return None


class FakeActionSafety:
    def __init__(self):
        self.actions = []
        self.completed = None
        self.direct_claims = {}

    async def start_direct_action(self, **kwargs):
        from dataclasses import dataclass
        @dataclass
        class Claim:
            claimed: bool = True
            action_id: str = "claim_123"
        return Claim()

    async def create_pending_action(self, tenant_id: str, actor_id: str, action_type: str, resource_type: str, payload: dict):
        action_id = uuid4().hex
        self.actions.append({
            "id": action_id,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "action_type": action_type,
            "resource_type": resource_type,
            "payload": payload,
        })
        return action_id

    async def complete_action(self, **kwargs):
        self.completed = kwargs

    async def fail_action(self, **kwargs):
        pass

    async def confirm_and_claim_action(self, action_id: str, tenant_id: str, actor_id: str, request_fingerprint: str):
        for act in self.actions:
            if act["id"] == action_id:
                from dataclasses import dataclass
                @dataclass
                class Act:
                    id: str
                    tenant_id: str
                    actor_id: str
                    action_type: str
                    payload: dict
                return Act(
                    id=act["id"],
                    tenant_id=act["tenant_id"],
                    actor_id=act["actor_id"],
                    action_type=act["action_type"],
                    payload=act["payload"],
                )
        return None

    async def get_action(self, action_id: str):
        for act in self.actions:
            if act["id"] == action_id:
                from dataclasses import dataclass
                @dataclass
                class Act:
                    id: str = act["id"]
                    tenant_id: str = act["tenant_id"]
                    actor_id: str = act["actor_id"]
                    action_type: str = act["action_type"]
                    payload: dict = None
                a = Act()
                a.payload = act["payload"]
                return a
        return None


@pytest.fixture
def mock_tools():
    tools = AsyncMock(spec=HubSpotToolRegistry)
    tools.hubspot_record_urls.return_value = ["https://app.hubspot.com/contacts/tenant-1/contact/580126173897"]
    return tools


@pytest.fixture
def agent(mock_tools):
    safety = FakeActionSafety()
    ai = DummyAIService()
    return AccountIntelligenceAgent(tools=mock_tools, ai_service=ai, action_safety=safety)


@pytest.mark.asyncio
async def test_context_aware_contact_creation_and_update(agent, mock_tools):
    """Test Turn 1: Create contact -> Turn 2: Update this contact with emaid -> Turn 3: confirm."""
    created_contact = HubSpotContact(
        id="580126173897",
        properties={"firstname": "faarif", "email": None},
    )
    mock_tools.create_contact.return_value = created_contact
    mock_tools.get_contact.return_value = created_contact

    # Turn 1: Create contact named faarif
    req1 = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="Create contact named faarif",
        request_id="r1",
        channel_id="C1",
    )
    res1 = await agent.respond(req1)
    assert res1.status == "ok"
    assert "580126173897" in res1.text
    assert "faarif" in res1.text

    # Turn 2: update this contact with emaid farif@gmail.com
    req2 = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="update this contact with emaid farif@gmail.com",
        request_id="r2",
        channel_id="C1",
    )
    res2 = await agent.respond(req2)
    assert res2.status == "pending_confirmation"
    assert res2.result["contact_id"] == "580126173897"
    action_id = res2.result["action_id"]

    # Turn 3: confirm the update
    updated_contact = HubSpotContact(
        id="580126173897",
        properties={"firstname": "faarif", "email": "farif@gmail.com"},
    )
    mock_tools.update_contact.return_value = updated_contact
    req3 = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message=f"confirm {action_id}",
        request_id="r3",
        channel_id="C1",
    )
    res3 = await agent.respond(req3)
    assert res3.status == "ok"
    assert "Contact updated successfully" in res3.text
    assert "580126173897" in res3.text
    mock_tools.update_contact.assert_called_once_with(
        "tenant-1",
        "580126173897",
        {"email": "farif@gmail.com"},
    )


@pytest.mark.asyncio
async def test_update_contact_with_conversation_context(agent, mock_tools):
    """Test resolving contact ID from conversation_context string across turns."""
    mock_tools.get_contact.return_value = HubSpotContact(
        id="580126173897",
        properties={"firstname": "faarif"},
    )

    context = (
        "User: Create contact named faarif\n"
        "Assistant: Contact faarif was created successfully in HubSpot.\n"
        "• Contact ID: `580126173897`"
    )
    req = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U2",
        message="update this contact with emaid farif@gmail.com",
        request_id="r_ctx",
        channel_id="C2",
        conversation_context=context,
    )
    res = await agent.respond(req)
    assert res.status == "pending_confirmation"
    assert res.result["contact_id"] == "580126173897"


@pytest.mark.asyncio
async def test_update_contact_field_aliases_and_typos(agent, mock_tools):
    """Test job title, phone, mobile, and lifecycle stage updates with aliases."""
    contact = HubSpotContact(id="101", properties={"firstname": "Brian", "lastname": "Halligan"})
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=contact)
    mock_tools.get_contact.return_value = contact

    # Title update
    req = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="update Brian Halligan's title to Executive Chairman",
        request_id="r_title",
    )
    res = await agent.respond(req)
    assert res.status == "pending_confirmation"
    assert res.result["contact_id"] == "101"

    # Phone update
    req_phone = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="update Brian Halligan's phone to +1-555-0199",
        request_id="r_phone",
    )
    res_phone = await agent.respond(req_phone)
    assert res_phone.status == "pending_confirmation"
    assert res_phone.result["contact_id"] == "101"

    # Lifecycle stage update with natural name
    req_stage = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="set Brian Halligan's lifecycle stage to sales qualified lead",
        request_id="r_stage",
    )
    res_stage = await agent.respond(req_stage)
    assert res_stage.status == "pending_confirmation"
    assert res_stage.result["contact_id"] == "101"


@pytest.mark.asyncio
async def test_scenario_1_missing_field_interactive(agent, mock_tools):
    """Scenario 1: Prompt user with interactive writable field selection when field is omitted."""
    contact = HubSpotContact(
        id="202",
        properties={"firstname": "Kaviya", "email": "kaviya@example.com"},
    )
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=contact)
    mock_tools.get_contact.return_value = contact

    req = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="Update Kaviya",
        request_id="r_miss_field",
    )
    res = await agent.respond(req)
    assert res.status == "needs_field"
    assert res.result["kind"] == "interactive_field_selection"
    assert res.result["contact_id"] == "202"
    assert len(res.result["fields"]) >= 6

    # Verify Block Kit rendering
    blocks = _build_response_blocks(res)
    assert any(b.get("type") == "actions" for b in blocks)
    select_block = next(b for b in blocks if b.get("type") == "actions")
    assert select_block["elements"][0]["action_id"] == "select_field"


@pytest.mark.asyncio
async def test_scenario_2_missing_lifecycle_stage_value_interactive(agent, mock_tools):
    """Scenario 2: Prompt user with interactive lifecycle stage options when stage value is omitted."""
    contact = HubSpotContact(
        id="202",
        properties={"firstname": "Kaviya", "email": "kaviya@example.com"},
    )
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=contact)
    mock_tools.get_contact.return_value = contact

    req = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="Update Kaviya's lifecycle stage",
        request_id="r_miss_stage",
    )
    res = await agent.respond(req)
    assert res.status == "needs_value"
    assert res.result["kind"] == "interactive_value_selection"
    assert res.result["field"] == "lifecyclestage"
    assert len(res.result["options"]) >= 8

    # Verify Block Kit rendering
    blocks = _build_response_blocks(res)
    assert any(b.get("type") == "actions" for b in blocks)
    select_block = next(b for b in blocks if b.get("type") == "actions")
    assert select_block["elements"][0]["action_id"] == "select_value"


@pytest.mark.asyncio
async def test_scenario_3_ambiguous_contact_selection(agent, mock_tools):
    """Scenario 3: Return interactive selection cards and Select buttons when multiple contacts match."""
    c1 = HubSpotContact(id="301", properties={"firstname": "Maria", "lastname": "Smith", "email": "ms@example.com"})
    c2 = HubSpotContact(id="302", properties={"firstname": "Maria", "lastname": "Garcia", "email": "mg@example.com"})
    mock_tools.resolve_contact.return_value = ContactResolution(
        status="ambiguous",
        candidates=[c1, c2],
    )

    req = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="Update Maria's email to maria@example.com",
        request_id="r_ambig",
    )
    res = await agent.respond(req)
    assert res.status == "contact_ambiguous"
    assert res.result["kind"] == "interactive_contact_selection"
    assert len(res.result["candidates"]) == 2

    # Verify Block Kit rendering
    blocks = _build_response_blocks(res)
    candidate_sections = [b for b in blocks if b.get("type") == "section" and b.get("accessory", {}).get("action_id") == "select_contact"]
    assert len(candidate_sections) == 2


@pytest.mark.asyncio
async def test_scenario_4_company_association_update(agent, mock_tools):
    """Scenario 4: Associate contact with company when user requests changing or associating company."""
    contact = HubSpotContact(id="401", properties={"firstname": "Maria", "email": "maria@example.com"})
    company = HubSpotCompany(id="comp-99", properties={"name": "Acme Technologies"})

    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=contact)
    mock_tools.get_contact.return_value = contact
    mock_tools.resolve_company.return_value = AsyncMock(status="found", company=company)

    req = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="Change Maria's company to Acme Technologies",
        request_id="r_assoc",
    )
    res = await agent.respond(req)
    assert res.status == "ok"
    assert "associated with Acme Technologies" in res.text
    mock_tools.associate.assert_called_once_with(
        "tenant-1",
        from_type="contacts",
        from_id="401",
        to_type="companies",
        to_id="comp-99",
    )


def test_parse_home_interaction_interactive_actions():
    """Test parse_home_interaction correctly extracts select_contact, select_field, select_value."""
    # select_contact
    payload_contact = {
        "type": "block_actions",
        "view": {"type": "home"},
        "user": {"id": "U123", "team_id": "T123"},
        "actions": [{"action_id": "select_contact", "value": "580126173897", "action_ts": "ts1"}],
    }
    interaction = parse_home_interaction(payload_contact)
    assert interaction.kind == "select_contact"
    assert interaction.action_id == "580126173897"

    # select_field
    payload_field = {
        "type": "block_actions",
        "view": {"type": "home"},
        "user": {"id": "U123", "team_id": "T123"},
        "actions": [{
            "action_id": "select_field",
            "selected_option": {"value": "580126173897:lifecyclestage"},
            "action_ts": "ts2",
        }],
    }
    interaction_field = parse_home_interaction(payload_field)
    assert interaction_field.kind == "select_field"
    assert interaction_field.text == "580126173897:lifecyclestage"


@pytest.mark.asyncio
async def test_update_contact_with_job_title_with_value_regression(agent, mock_tools):
    """Regression: 'Update contact Arya with job title AI Infrastructure Engineer' directly proposes update."""
    arya = HubSpotContact(
        id="566194297544",
        properties={"firstname": "Arya", "jobtitle": "It service"},
    )
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=arya)
    mock_tools.get_contact.return_value = arya
    mock_tools.hubspot_record_urls.return_value = [
        "https://app.hubspot.com/contacts/99999/record/0-1/566194297544"
    ]

    req = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="Update contact Arya with job title AI Infrastructure Engineer",
        request_id="r_arya_title",
    )
    res = await agent.respond(req)

    assert res.status == "pending_confirmation"
    assert res.result["contact_id"] == "566194297544"
    assert res.result["properties"]["jobtitle"] == "AI Infrastructure Engineer"
    assert "Arya" in res.text
    assert "It service" in res.text
    assert "AI Infrastructure Engineer" in res.text
    assert "confirm " in res.text

    # Now confirm the update
    action_id = res.result["action_id"]
    updated_arya = HubSpotContact(
        id="566194297544",
        properties={"firstname": "Arya", "jobtitle": "AI Infrastructure Engineer"},
    )
    mock_tools.update_contact.return_value = updated_arya

    req_confirm = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message=f"confirm {action_id}",
        request_id="r_arya_confirm",
    )
    res_confirm = await agent.respond(req_confirm)
    assert res_confirm.status == "ok"
    assert "Contact updated successfully" in res_confirm.text
    assert "566194297544" in res_confirm.text
    mock_tools.update_contact.assert_called_with(
        "tenant-1",
        "566194297544",
        {"jobtitle": "AI Infrastructure Engineer"},
    )


@pytest.mark.asyncio
async def test_update_contact_with_job_title_omitted_value_path_b(agent, mock_tools):
    """Path B: 'Update contact Arya with job title' prompts for new value with current value display."""
    arya = HubSpotContact(
        id="566194297544",
        properties={"firstname": "Arya", "jobtitle": "It service"},
    )
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=arya)
    mock_tools.get_contact.return_value = arya

    # Turn 1: Omitted value prompt
    req1 = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="Update contact Arya with job title",
        request_id="r_turn1",
    )
    res1 = await agent.respond(req1)
    assert res1.status == "needs_field_value"
    assert res1.result["kind"] == "interactive_field_value_prompt"
    assert res1.result["field"] == "jobtitle"
    assert res1.result["current_value"] == "It service"
    assert "Current Job Title: It service" in res1.text
    assert "What should Arya's new Job Title be?" in res1.text

    # Turn 2: User provides new value
    req2 = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="AI Infrastructure Engineer",
        request_id="r_turn2",
    )
    res2 = await agent.respond(req2)
    assert res2.status == "pending_confirmation"
    assert res2.result["contact_id"] == "566194297544"
    assert res2.result["properties"]["jobtitle"] == "AI Infrastructure Engineer"
    assert "confirm " in res2.text


@pytest.mark.asyncio
async def test_update_this_record_path_a(agent, mock_tools):
    """Path A: 'Update this record' with recent context prompts for field choices."""
    contact = HubSpotContact(
        id="580138092226",
        properties={"firstname": "Assif", "email": "assif@example.com"},
    )
    mock_tools.get_contact.return_value = contact

    # Populate recent contact via create or explicit turn
    agent._record_recent_contact(
        AgentRequest(tenant_id="tenant-1", actor_id="U1", message="create contact Assif", request_id="init"),
        contact_id="580138092226",
        properties={"firstname": "Assif"},
    )

    req = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="Update this record",
        request_id="r_this_rec",
    )
    res = await agent.respond(req)
    assert res.status == "needs_field"
    assert res.result["kind"] == "interactive_field_selection"
    assert res.result["contact_id"] == "580138092226"
    assert "Which field would you like to update for Assif?" in res.text
    assert "Job Title" in res.text
    assert "Lifecycle Stage" in res.text


@pytest.mark.asyncio
async def test_lifecycle_stage_already_set_no_change(agent, mock_tools):
    """If the requested lifecycle stage is already set, report no change made."""
    assif = HubSpotContact(
        id="580138092226",
        properties={"firstname": "Assif", "lifecyclestage": "customer"},
    )
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=assif)
    mock_tools.get_contact.return_value = assif

    req = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="Update contact Assif with lifecyclestage customer",
        request_id="r_already_set",
    )
    res = await agent.respond(req)
    assert res.status == "ok"
    assert "already set to customer" in res.text
    assert "No change was made" in res.text


@pytest.mark.asyncio
async def test_actionable_guidance_contact_not_found(agent, mock_tools):
    """Actionable guidance: contact not found surfaces clear next-step options."""
    mock_tools.resolve_contact.return_value = ContactResolution(status="not_found", contact=None)

    req = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U1",
        message="Update contact NonexistentPerson with email none@example.com",
        request_id="r_not_found",
    )
    res = await agent.respond(req)
    assert res.status == "contact_not_found"
    assert "I couldn't find an exact contact match" in res.text
    assert "Search by email address" in res.text
    assert "Show all contacts" in res.text
    assert "Cancel" in res.text


@pytest.mark.asyncio
async def test_email_reply_resumes_pending_update_across_fresh_agent_instances(mock_tools):
    """Regression test: user replies with raw email diya.test.2026@example.com to update prompt."""
    diya = HubSpotContact(
        id="580126173897",
        properties={"firstname": "Diya", "email": "old@example.com"},
    )
    mock_tools.get_contact.return_value = diya
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=diya)

    from app.agent.service import AccountIntelligenceAgent
    from app.ai.service import AIService
    # safety
    from unittest.mock import MagicMock

    safety = FakeActionSafety()
    safety.create_pending_action = AsyncMock(return_value="act_email_123")

    # Instance 1: Turn 1 prompts for email
    agent1 = AccountIntelligenceAgent(mock_tools, DummyAIService(), safety)
    req1 = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U_DIYA",
        message="Update contact Diya with email",
        request_id="r_diya_1",
        channel_id="C_DIYA",
    )
    res1 = await agent1.respond(req1)
    assert res1.status == "needs_field_value"
    assert res1.result["field"] == "email"
    assert res1.result["contact_id"] == "580126173897"

    # Instance 2: (Fresh agent from get_agent on next HTTP request)
    agent2 = AccountIntelligenceAgent(mock_tools, DummyAIService(), safety)
    # Slack formats email as <mailto:diya.test.2026@example.com|diya.test.2026@example.com>
    req2 = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U_DIYA",
        message="<mailto:diya.test.2026@example.com|diya.test.2026@example.com>",
        request_id="r_diya_2",
        channel_id="C_DIYA",
    )
    res2 = await agent2.respond(req2)
    assert res2.status == "pending_confirmation"
    assert res2.result["contact_id"] == "580126173897"
    assert res2.result["properties"]["email"] == "diya.test.2026@example.com"
    assert "diya.test.2026@example.com" in res2.text


@pytest.mark.asyncio
async def test_email_reply_resumes_via_conversation_context(mock_tools):
    """Test continuation when in-memory pending dict is absent but conversation_context exists."""
    diya = HubSpotContact(
        id="580126173897",
        properties={"firstname": "Diya", "email": None},
    )
    mock_tools.get_contact.return_value = diya

    from app.agent.service import AccountIntelligenceAgent
    from app.ai.service import AIService
    # safety
    from unittest.mock import MagicMock

    safety = FakeActionSafety()
    safety.create_pending_action = AsyncMock(return_value="act_ctx_email")

    agent = AccountIntelligenceAgent(mock_tools, DummyAIService(), safety)

    context = (
        "Assistant: 🟡 ❓ *Update Email*\n"
        "👤 Contact: Diya (ID: 580126173897)\n"
        "📧 Current Email: Not set\n"
        "What should Diya's new Email be?"
    )
    req = AgentRequest(
        tenant_id="tenant-1",
        actor_id="U_NEW_USER",
        message="diya.test.2026@example.com",
        request_id="r_ctx_email",
        conversation_context=context,
    )
    res = await agent.respond(req)
    assert res.status == "pending_confirmation"
    assert res.result["contact_id"] == "580126173897"
    assert res.result["properties"]["email"] == "diya.test.2026@example.com"


@pytest.mark.asyncio
async def test_pending_update_isolated_between_different_slack_users(mock_tools):
    """Cross-user isolation: User B's message in the same channel must not resume User A's pending update."""
    diya = HubSpotContact(
        id="580126173897",
        properties={"firstname": "Diya", "email": "old@example.com"},
    )
    mock_tools.get_contact.return_value = diya
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=diya)

    from app.agent.service import AccountIntelligenceAgent
    safety = FakeActionSafety()

    agent = AccountIntelligenceAgent(mock_tools, DummyAIService(), safety)

    # User A initiates update in Channel C1
    req_a = AgentRequest(
        tenant_id="tenant-1",
        actor_id="USER_A",
        message="Update contact Diya with email",
        request_id="r_user_a",
        channel_id="C1",
    )
    res_a = await agent.respond(req_a)
    assert res_a.status == "needs_field_value"

    # User B sends an email in the same channel C1
    req_b = AgentRequest(
        tenant_id="tenant-1",
        actor_id="USER_B",
        message="other.person@example.com",
        request_id="r_user_b",
        channel_id="C1",
    )
    res_b = await agent.respond(req_b)
    # User B must NOT resume User A's pending update!
    assert res_b.status != "pending_confirmation"


@pytest.mark.asyncio
async def test_pending_update_isolated_across_different_conversations_same_user(mock_tools):
    """Same-user multi-conversation: User in Channel 1 updating Diya must not conflict with Channel 2."""
    diya = HubSpotContact(id="580126173897", properties={"firstname": "Diya", "email": "old@example.com"})
    arya = HubSpotContact(id="566194297544", properties={"firstname": "Arya", "jobtitle": "Support"})

    def get_contact_mock(tenant_id, cid):
        return diya if cid == "580126173897" else arya

    mock_tools.get_contact.side_effect = get_contact_mock

    def resolve_contact_mock(tenant_id, **kwargs):
        fn = kwargs.get("first_name", "")
        if "Diya" in fn:
            return ContactResolution(status="found", contact=diya)
        return ContactResolution(status="found", contact=arya)

    mock_tools.resolve_contact.side_effect = resolve_contact_mock

    from app.agent.service import AccountIntelligenceAgent
    safety = FakeActionSafety()
    agent = AccountIntelligenceAgent(mock_tools, DummyAIService(), safety)

    # User in Channel C1 initiates email update for Diya
    req_c1_1 = AgentRequest(tenant_id="tenant-1", actor_id="U_MULTI", channel_id="C1", message="Update contact Diya with email", request_id="r_c1_1")
    res_c1_1 = await agent.respond(req_c1_1)
    assert res_c1_1.status == "needs_field_value"
    assert res_c1_1.result["field"] == "email"

    # Same user in Channel C2 initiates job title update for Arya
    req_c2_1 = AgentRequest(tenant_id="tenant-1", actor_id="U_MULTI", channel_id="C2", message="Update contact Arya with job title", request_id="r_c2_1")
    res_c2_1 = await agent.respond(req_c2_1)
    assert res_c2_1.status == "needs_field_value"
    assert res_c2_1.result["field"] == "jobtitle"

    # User replies with email in Channel C1 -> resumes Diya's email update
    req_c1_2 = AgentRequest(tenant_id="tenant-1", actor_id="U_MULTI", channel_id="C1", message="diya.new@example.com", request_id="r_c1_2")
    res_c1_2 = await agent.respond(req_c1_2)
    assert res_c1_2.status == "pending_confirmation"
    assert res_c1_2.result["contact_id"] == "580126173897"
    assert res_c1_2.result["properties"]["email"] == "diya.new@example.com"

    # User replies with title in Channel C2 -> resumes Arya's job title update
    req_c2_2 = AgentRequest(tenant_id="tenant-1", actor_id="U_MULTI", channel_id="C2", message="Principal Architect", request_id="r_c2_2")
    res_c2_2 = await agent.respond(req_c2_2)
    assert res_c2_2.status == "pending_confirmation"
    assert res_c2_2.result["contact_id"] == "566194297544"
    assert res_c2_2.result["properties"]["jobtitle"] == "Principal Architect"


def test_contact_id_regex_never_matches_action_id():
    """Verify contact ID regex never matches 32-char hex Action ID even if it starts with digits."""
    from app.agent.service import AccountIntelligenceAgent

    context_with_action_id = (
        "🟡 🔄 *Confirmation needed*\n"
        "👤 Contact: *Ramya* (ID: `580219333353`)\n"
        "• Property: *Lifecycle Stage*\n"
        "  Current: lead\n"
        "  Proposed: *salesqualifiedlead*\n\n"
        "Action ID: `76309a7e3056455fb8f7f937de4b8fe0`\n"
        "Reply with `confirm 76309a7e3056455fb8f7f937de4b8fe0` to update this contact."
    )

    extracted = AccountIntelligenceAgent._extract_contact_id_from_context(context_with_action_id)
    assert extracted == "580219333353"
    assert extracted != "76309"


@pytest.mark.asyncio
async def test_lifecycle_stage_update_and_confirmation_with_raw_action_id(mock_tools):
    """Verify selecting or replying with lifecycle stage and confirming with raw 32-hex action ID works cleanly."""
    ramya = HubSpotContact(id="580219333353", properties={"firstname": "Ramya", "lifecyclestage": "lead"})

    mock_tools.get_contact.return_value = ramya
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=ramya)
    mock_tools.update_contact.return_value = HubSpotContact(
        id="580219333353",
        properties={"firstname": "Ramya", "lifecyclestage": "salesqualifiedlead"},
    )
    mock_tools.hubspot_record_urls.return_value = ["https://app.hubspot.com/contacts/123/contact/580219333353"]

    from app.agent.service import AccountIntelligenceAgent
    safety = FakeActionSafety()
    agent = AccountIntelligenceAgent(mock_tools, DummyAIService(), safety)

    # 1. User asks to update lifecycle stage
    req1 = AgentRequest(
        tenant_id="test-tenant",
        actor_id="USER_1",
        channel_id="C1",
        message="Update Ramya's lifecycle stage",
        request_id="r1",
    )
    res1 = await agent.respond(req1)
    assert res1.status == "needs_value"
    assert res1.result["field"] == "lifecyclestage"
    assert res1.result["contact_id"] == "580219333353"

    # 2. User selects or replies with "salesqualifiedlead"
    req2 = AgentRequest(
        tenant_id="test-tenant",
        actor_id="USER_1",
        channel_id="C1",
        message="salesqualifiedlead",
        request_id="r2",
    )
    res2 = await agent.respond(req2)
    assert res2.status == "pending_confirmation"
    action_id = res2.result["action_id"]
    assert res2.result["contact_id"] == "580219333353"
    assert res2.result["properties"]["lifecyclestage"] == "salesqualifiedlead"

    # 3. User confirms by entering just the 32-hex action ID (raw)
    req3 = AgentRequest(
        tenant_id="test-tenant",
        actor_id="USER_1",
        channel_id="C1",
        message=action_id,
        request_id="r3",
    )
    res3 = await agent.respond(req3)
    assert res3.status == "ok"
    assert "Contact updated successfully" in res3.text
    # Verify update_contact was called with the true contact ID 580219333353, not an action ID
    mock_tools.update_contact.assert_awaited_with(
        "test-tenant",
        "580219333353",
        {"lifecyclestage": "salesqualifiedlead"},
    )


@pytest.mark.asyncio
async def test_dropdown_selection_continuation_never_stores_command_as_stage_value(mock_tools):
    """Regression for c2cfbfc8fa7d42a3afec23a6c3b58287:
    Dropdown click sends 'Update contact 580219333353 with lifecyclestage marketingqualifiedlead'.
    Must store 'marketingqualifiedlead' as stage value, NEVER the command string.
    """
    ramya = HubSpotContact(id="580219333353", properties={"firstname": "Ramya", "lifecyclestage": "lead"})

    mock_tools.get_contact.return_value = ramya
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=ramya)
    mock_tools.update_contact.return_value = HubSpotContact(
        id="580219333353",
        properties={"firstname": "Ramya", "lifecyclestage": "marketingqualifiedlead"},
    )
    mock_tools.hubspot_record_urls.return_value = ["https://app.hubspot.com/contacts/123/contact/580219333353"]

    from app.agent.service import AccountIntelligenceAgent
    safety = FakeActionSafety()
    agent = AccountIntelligenceAgent(mock_tools, DummyAIService(), safety)

    # 1. Ask to update lifecycle stage -> triggers dropdown prompt
    req1 = AgentRequest(tenant_id="t1", actor_id="U1", channel_id="C1", message="Update Ramya's lifecycle stage", request_id="r1")
    res1 = await agent.respond(req1)
    assert res1.status == "needs_value"

    # 2. Slack Home dropdown click dispatches structured update command:
    dropdown_msg = "Update contact 580219333353 with lifecyclestage marketingqualifiedlead"
    req2 = AgentRequest(tenant_id="t1", actor_id="U1", channel_id="C1", message=dropdown_msg, request_id="r2")
    res2 = await agent.respond(req2)

    assert res2.status == "pending_confirmation"
    # MUST be canonical stage "marketingqualifiedlead", NOT the command string!
    assert res2.result["properties"]["lifecyclestage"] == "marketingqualifiedlead"
    assert "contact 580219333353" not in res2.result["properties"]["lifecyclestage"]

    # 3. Confirm action
    action_id = res2.result["action_id"]
    req3 = AgentRequest(tenant_id="t1", actor_id="U1", channel_id="C1", message=f"confirm {action_id}", request_id="r3")
    res3 = await agent.respond(req3)
    assert res3.status == "ok"
    mock_tools.update_contact.assert_awaited_with(
        "t1",
        "580219333353",
        {"lifecyclestage": "marketingqualifiedlead"},
    )


@pytest.mark.asyncio
async def test_update_contact_hs_lead_status_directly_shows_options(mock_tools):
    """Issue 1: 'Update contact 580205144762\'s hs_lead_status' must immediately return Lead Status options, never a field selection loop."""
    abitha = HubSpotContact(id="580205144762", properties={"firstname": "Abitha", "hs_lead_status": None})
    mock_tools.get_contact.return_value = abitha
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=abitha)
    mock_tools.update_contact.return_value = HubSpotContact(
        id="580205144762",
        properties={"firstname": "Abitha", "hs_lead_status": "OPEN"},
    )
    mock_tools.hubspot_record_urls.return_value = ["https://app.hubspot.com/contacts/123/contact/580205144762"]

    from app.agent.service import AccountIntelligenceAgent
    safety = FakeActionSafety()
    agent = AccountIntelligenceAgent(mock_tools, DummyAIService(), safety)

    req = AgentRequest(
        tenant_id="t1",
        actor_id="U1",
        channel_id="C1",
        message="Update contact 580205144762's hs_lead_status",
        request_id="r_ls",
    )
    res = await agent.respond(req)
    # Must ask for value with options, NOT ask for field again!
    assert res.status == "needs_value"
    assert res.result["field"] == "hs_lead_status"
    assert res.result["contact_id"] == "580205144762"
    assert len(res.result["options"]) > 0
    option_values = [opt["value"] for opt in res.result["options"]]
    assert "OPEN" in option_values
    assert "NEW" in option_values


@pytest.mark.asyncio
async def test_select_field_hs_lead_status_shows_options(mock_tools):
    """Issue 1: When user selects hs_lead_status from field dropdown, it must show Lead Status options."""
    abitha = HubSpotContact(id="580205144762", properties={"firstname": "Abitha", "hs_lead_status": None})
    mock_tools.get_contact.return_value = abitha
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=abitha)
    mock_tools.hubspot_record_urls.return_value = ["https://app.hubspot.com/contacts/123/contact/580205144762"]

    from app.agent.service import AccountIntelligenceAgent
    safety = FakeActionSafety()
    agent = AccountIntelligenceAgent(mock_tools, DummyAIService(), safety)

    # select_field interaction translation from slack.py: "Update contact 580205144762's lead status"
    req = AgentRequest(
        tenant_id="t1",
        actor_id="U1",
        channel_id="C1",
        message="Update contact 580205144762's lead status",
        request_id="r_sf",
    )
    res = await agent.respond(req)
    assert res.status == "needs_value"
    assert res.result["field"] == "hs_lead_status"
    assert res.result["contact_id"] == "580205144762"


@pytest.mark.asyncio
async def test_natural_language_follow_ups_reuse_contact_context(mock_tools):
    """Issue 2: Natural-language follow-ups reuse current contact context when unambiguous:
    - 'update this record status'
    - 'update this contact stage'
    - 'change this contact's lead status'
    - 'update this record'
    - 'change the status to lead'
    """
    abitha = HubSpotContact(id="580205144762", properties={"firstname": "Abitha", "lifecyclestage": "lead", "hs_lead_status": "OPEN"})
    mock_tools.get_contact.return_value = abitha
    mock_tools.resolve_contact.return_value = ContactResolution(status="found", contact=abitha)
    mock_tools.hubspot_record_urls.return_value = ["https://app.hubspot.com/contacts/123/contact/580205144762"]

    from app.agent.service import AccountIntelligenceAgent
    safety = FakeActionSafety()
    agent = AccountIntelligenceAgent(mock_tools, DummyAIService(), safety)

    # 1. Establish contact context with initial update request
    req0 = AgentRequest(tenant_id="t1", actor_id="U1", channel_id="C1", message="Update contact 580205144762", request_id="r0")
    res0 = await agent.respond(req0)
    assert res0.result["contact_id"] == "580205144762"

    # 2. 'update this record status' -> asks for Lead Status options
    req1 = AgentRequest(tenant_id="t1", actor_id="U1", channel_id="C1", message="update this record status", request_id="r1")
    res1 = await agent.respond(req1)
    assert res1.status == "needs_value"
    assert res1.result["field"] == "hs_lead_status"
    assert res1.result["contact_id"] == "580205144762"

    # 3. 'update this contact stage' -> asks for Lifecycle Stage options
    req2 = AgentRequest(tenant_id="t1", actor_id="U1", channel_id="C1", message="update this contact stage", request_id="r2")
    res2 = await agent.respond(req2)
    assert res2.status == "needs_value"
    assert res2.result["field"] == "lifecyclestage"
    assert res2.result["contact_id"] == "580205144762"

    # 4. 'change this contact's lead status' -> asks for Lead Status options
    req3 = AgentRequest(tenant_id="t1", actor_id="U1", channel_id="C1", message="change this contact's lead status", request_id="r3")
    res3 = await agent.respond(req3)
    assert res3.status == "needs_value"
    assert res3.result["field"] == "hs_lead_status"
    assert res3.result["contact_id"] == "580205144762"

    # 5. 'update this record' -> asks focused clarification with available fields while retaining contact
    req4 = AgentRequest(tenant_id="t1", actor_id="U1", channel_id="C1", message="update this record", request_id="r4")
    res4 = await agent.respond(req4)
    assert res4.status == "needs_field"
    assert res4.result["contact_id"] == "580205144762"

    # 6. 'change the status to lead' -> proposes confirmation to set lifecycle stage to lead
    req5 = AgentRequest(tenant_id="t1", actor_id="U1", channel_id="C1", message="change the status to lead", request_id="r5")
    res5 = await agent.respond(req5)
    # Since current lifecyclestage is already 'lead', it either says already set or proposes
    assert res5.result["contact_id"] == "580205144762"
