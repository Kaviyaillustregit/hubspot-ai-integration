"""Unit tests for individual Slack-user-to-HubSpot-owner mapping during contact creation and owner display."""

import json
from unittest.mock import AsyncMock

import pytest

from app.agent.extraction import ValidatedExtraction
from app.agent.operations import CRMOperations
from app.agent.schemas import AgentRequest, AgentResponse, CRMIntentExtraction
from app.agent.service import AccountIntelligenceAgent, _ContactDraft
from app.agent.tools import HubSpotToolRegistry
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.models import (
    HubSpotCompany,
    HubSpotContact,
    HubSpotDeal,
    HubSpotPipeline,
    HubSpotPipelineStage,
)
from app.integrations.hubspot.owners import HubSpotOwner
from app.integrations.slack.home import render_response_blocks
from app.services.action_safety import ActionSafetyService
from app.services.slack_owner_mapping import (
    clear_slack_user_email_registry,
    resolve_slack_user_hubspot_owner,
)


@pytest.fixture(autouse=True)
def clean_registry():
    clear_slack_user_email_registry()
    yield
    clear_slack_user_email_registry()


@pytest.fixture
def sample_owners():
    return [
        HubSpotOwner(id="owner_amit_101", firstName="Amit", lastName="Kumar", email="amit@company.com"),
        HubSpotOwner(id="owner_kaviya_102", firstName="Kaviya", lastName="Murugan", email="kaviya@company.com"),
        HubSpotOwner(id="owner_vikas_103", firstName="Vikas", lastName="Kumar", email="vikas@company.com"),
    ]


@pytest.fixture
def mock_tools(sample_owners):
    tools = AsyncMock(spec=HubSpotToolRegistry)
    tools.owner_lookup_available = True
    tools.list_owners.return_value = list(sample_owners)
    tools.hubspot_record_urls.return_value = ["https://app.hubspot.com/contacts/tenant-a/contact/12345"]
    return tools


@pytest.fixture
def mock_safety():
    safety = AsyncMock(spec=ActionSafetyService)
    claim = AsyncMock()
    claim.action_id = "action-claim-1"
    safety.start_direct_action = AsyncMock(return_value=claim)
    safety.claim_action = AsyncMock(return_value=claim)
    safety.complete_action = AsyncMock()
    safety.fail_action = AsyncMock()
    return safety


@pytest.fixture
def agent(mock_tools, mock_safety):
    ai = AsyncMock()
    return AccountIntelligenceAgent(mock_tools, ai, mock_safety)


@pytest.mark.asyncio
async def test_amit_slack_account_creates_contact_assigned_to_amit(agent, mock_tools):
    """Amit's Slack account creates contact assigned to Amit's HubSpot owner ID."""
    created = HubSpotContact(
        id="c-amit-1",
        properties={"firstname": "John", "email": "john@test.com", "hubspot_owner_id": "owner_amit_101"},
    )
    mock_tools.create_contact.return_value = created

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_AMIT",
        actor_email="amit@company.com",
        message="Create contact John with email john@test.com",
        request_id="req-amit-1",
    )
    res = await agent._create_contact_directly(
        req,
        _ContactDraft(properties={"firstname": "John", "email": "john@test.com"}, company_name=None),
    )

    assert res.status == "ok"
    assert res.result["owner_id"] == "owner_amit_101"
    assert res.result["owner_name"] == "Amit Kumar"

    call_args = mock_tools.create_contact.call_args[0]
    assert call_args[1]["hubspot_owner_id"] == "owner_amit_101"

    blocks = render_response_blocks(res)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "👤 Owner: Amit Kumar" in encoded


@pytest.mark.asyncio
async def test_kaviya_slack_account_creates_contact_assigned_to_kaviya(agent, mock_tools):
    """Kaviya's Slack account creates contact assigned to Kaviya's HubSpot owner ID."""
    created = HubSpotContact(
        id="c-kaviya-1",
        properties={"firstname": "Sarah", "email": "sarah@test.com", "hubspot_owner_id": "owner_kaviya_102"},
    )
    mock_tools.create_contact.return_value = created

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_KAVIYA",
        actor_email="kaviya@company.com",
        message="Create contact Sarah with email sarah@test.com",
        request_id="req-kaviya-1",
    )
    res = await agent._create_contact_directly(
        req,
        _ContactDraft(properties={"firstname": "Sarah", "email": "sarah@test.com"}, company_name=None),
    )

    assert res.status == "ok"
    assert res.result["owner_id"] == "owner_kaviya_102"
    assert res.result["owner_name"] == "Kaviya Murugan"

    call_args = mock_tools.create_contact.call_args[0]
    assert call_args[1]["hubspot_owner_id"] == "owner_kaviya_102"

    blocks = render_response_blocks(res)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "👤 Owner: Kaviya Murugan" in encoded


@pytest.mark.asyncio
async def test_vikas_slack_account_creates_contact_assigned_to_vikas(agent, mock_tools):
    """Vikas's Slack account creates contact assigned to Vikas's HubSpot owner ID."""
    created = HubSpotContact(
        id="c-vikas-1",
        properties={"firstname": "David", "email": "david@test.com", "hubspot_owner_id": "owner_vikas_103"},
    )
    mock_tools.create_contact.return_value = created

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_VIKAS",
        actor_email="vikas@company.com",
        message="Create contact David with email david@test.com",
        request_id="req-vikas-1",
    )
    res = await agent._create_contact_directly(
        req,
        _ContactDraft(properties={"firstname": "David", "email": "david@test.com"}, company_name=None),
    )

    assert res.status == "ok"
    assert res.result["owner_id"] == "owner_vikas_103"
    assert res.result["owner_name"] == "Vikas Kumar"

    call_args = mock_tools.create_contact.call_args[0]
    assert call_args[1]["hubspot_owner_id"] == "owner_vikas_103"

    blocks = render_response_blocks(res)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "👤 Owner: Vikas Kumar" in encoded


@pytest.mark.asyncio
async def test_unmatched_slack_user_fails_safely_without_assigning_other_owner(agent, mock_tools, mock_safety):
    """Unmatched Slack user is never assigned another person's HubSpot account."""
    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_UNKNOWN",
        actor_email="stranger@otherdomain.com",
        message="Create contact Alex with email alex@test.com",
        request_id="req-unknown-1",
    )
    res = await agent._create_contact_directly(
        req,
        _ContactDraft(properties={"firstname": "Alex", "email": "alex@test.com"}, company_name=None),
    )

    assert res.status == "owner_mapping_unavailable"
    assert "HubSpot owner mapping is unavailable" in res.text
    assert "stranger@otherdomain.com" in res.text
    mock_tools.create_contact.assert_not_called()
    mock_safety.fail_action.assert_called_once()


@pytest.mark.asyncio
async def test_missing_email_fails_safely_without_creation(agent, mock_tools, mock_safety):
    """Missing Slack email returns mapping unavailable message and does not create contact."""
    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_NO_EMAIL",
        actor_email=None,
        message="Create contact Bob with email bob@test.com",
        request_id="req-no-email-1",
    )
    res = await agent._create_contact_directly(
        req,
        _ContactDraft(properties={"firstname": "Bob", "email": "bob@test.com"}, company_name=None),
    )

    assert res.status == "owner_mapping_unavailable"
    assert "Could not determine the Slack email address" in res.text
    mock_tools.create_contact.assert_not_called()
    mock_safety.fail_action.assert_called_once()


@pytest.mark.asyncio
async def test_owner_lookup_failure_fails_safely(agent, mock_tools, mock_safety):
    """Owner lookup failure (IntegrationError) fails safely without assigning arbitrary owner."""
    mock_tools.list_owners.side_effect = IntegrationError("HubSpot API 500 server error")

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_AMIT",
        actor_email="amit@company.com",
        message="Create contact Eve with email eve@test.com",
        request_id="req-lookup-err-1",
    )
    res = await agent._create_contact_directly(
        req,
        _ContactDraft(properties={"firstname": "Eve", "email": "eve@test.com"}, company_name=None),
    )

    assert res.status == "owner_mapping_unavailable"
    assert "Failed to retrieve HubSpot owners" in res.text
    mock_tools.create_contact.assert_not_called()
    mock_safety.fail_action.assert_called_once()


@pytest.mark.asyncio
async def test_owner_assignment_verified_against_saved_hubspot_contact(agent, mock_tools, mock_safety):
    """If HubSpot fails to persist the assigned owner, failure is reported and local properties are not fabricated."""
    saved_without_owner = HubSpotContact(
        id="c-unverified-1",
        properties={"firstname": "Frank", "email": "frank@test.com", "hubspot_owner_id": None},
    )
    mock_tools.create_contact.return_value = saved_without_owner
    mock_tools.get_contact.return_value = saved_without_owner

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_AMIT",
        actor_email="amit@company.com",
        message="Create contact Frank with email frank@test.com",
        request_id="req-verify-fail",
    )
    res = await agent._create_contact_directly(
        req,
        _ContactDraft(properties={"firstname": "Frank", "email": "frank@test.com"}, company_name=None),
    )

    assert res.status == "owner_assignment_failed"
    assert "could not be verified in HubSpot" in res.text
    mock_safety.fail_action.assert_called_once()


@pytest.mark.asyncio
async def test_tenant_isolation_in_owner_mapping(mock_tools):
    """Owner mapping is strictly tenant-specific: tenant B owner is not matched when user is in tenant A."""
    def list_owners_by_tenant(t_id):
        if t_id == "tenant-a":
            return [HubSpotOwner(id="kaviya_a", firstName="Kaviya", lastName="M", email="kaviya@company.com")]
        elif t_id == "tenant-b":
            return [HubSpotOwner(id="amit_b", firstName="Amit", lastName="K", email="amit@company.com")]
        return []

    mock_tools.list_owners.side_effect = list_owners_by_tenant

    req_amit_in_tenant_a = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_AMIT",
        actor_email="amit@company.com",
        message="Create contact in tenant A",
        request_id="req-tenant-iso",
    )
    owner, err = await resolve_slack_user_hubspot_owner(req_amit_in_tenant_a, mock_tools)
    assert owner is None
    assert "No HubSpot owner found with email 'amit@company.com' in workspace 'tenant-a'" in err

    req_amit_in_tenant_b = AgentRequest(
        tenant_id="tenant-b",
        actor_id="U_AMIT",
        actor_email="amit@company.com",
        message="Create contact in tenant B",
        request_id="req-tenant-iso-2",
    )
    owner_b, err_b = await resolve_slack_user_hubspot_owner(req_amit_in_tenant_b, mock_tools)
    assert owner_b is not None
    assert owner_b.id == "amit_b"
    assert err_b is None


@pytest.mark.asyncio
async def test_operations_execution_path_assigns_matched_owner(mock_tools, mock_safety):
    """Verify operations.py execution path also assigns matched owner and handles unmapped user safely."""
    ops = CRMOperations(mock_tools, mock_safety)

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_KAVIYA",
        actor_email="kaviya@company.com",
        message="Create company MegaCorp and contact Laura with email laura@megacorp.com",
        request_id="req-ops-create",
    )
    extraction = CRMIntentExtraction(
        intent="create_contact",
        company_action="create",
        company_name="MegaCorp",
        confidence=0.95,
    )
    validated = ValidatedExtraction(
        intent="create_contact",
        properties={"firstname": "Laura", "email": "laura@megacorp.com"},
        company_name="MegaCorp",
        contact_id=None,
    )

    created_company = AsyncMock()
    created_company.id = "comp-1"
    created_company.properties = {"name": "MegaCorp"}
    mock_tools.create_company.return_value = created_company

    created_contact = HubSpotContact(
        id="c-laura-1",
        properties={"firstname": "Laura", "email": "laura@megacorp.com", "hubspot_owner_id": "owner_kaviya_102"},
    )
    mock_tools.create_contact.return_value = created_contact
    mock_tools.resolve_contact.return_value = AsyncMock(contact=None)

    resp = await ops.respond(req, extraction, validated, "idemp-key-1")
    assert resp.status == "ok"
    assert "Laura" in resp.text
    call_args = mock_tools.create_contact.call_args[0]
    assert call_args[1]["hubspot_owner_id"] == "owner_kaviya_102"


@pytest.mark.asyncio
async def test_owner_name_display_three_states():
    """Verify three owner states: resolved name, unassigned, and owner not available."""
    columns = [
        {"key": "dealname", "label": "Deal name"},
        {"key": "company", "label": "Company"},
        {"key": "dealstage", "label": "Stage"},
        {"key": "closedate", "label": "Close Date"},
        {"key": "owner", "label": "Owner"},
    ]

    # State 1: Resolved owner
    r_resolved = AgentResponse(
        status="ok",
        text="Open deals",
        request_id="req-disp-1",
        result={
            "kind": "crm_card",
            "card": True,
            "title": "Open Deals",
            "table": {
                "columns": columns,
                "rows": [{
                    "dealname": "Deal 1",
                    "company": "Acme",
                    "dealstage": "Qualified To Buy",
                    "closedate": "2026-12-01",
                    "owner": "Amit Kumar",
                }],
            },
        },
    )
    enc_resolved = json.dumps(render_response_blocks(r_resolved), ensure_ascii=False)
    assert "*👤 Owner:* Amit Kumar" in enc_resolved

    # State 2: No owner assigned
    r_unassigned = AgentResponse(
        status="ok",
        text="Open deals",
        request_id="req-disp-2",
        result={
            "kind": "crm_card",
            "card": True,
            "title": "Open Deals",
            "table": {
                "columns": columns,
                "rows": [{
                    "dealname": "Deal 2",
                    "company": "Acme",
                    "dealstage": "Qualified To Buy",
                    "closedate": "2026-12-01",
                    "owner": None,
                }],
            },
        },
    )
    enc_unassigned = json.dumps(render_response_blocks(r_unassigned), ensure_ascii=False)
    assert "*👤 Owner:* Unassigned" in enc_unassigned

    # State 3: Owner ID exists but cannot be resolved
    r_unresolved = AgentResponse(
        status="ok",
        text="Open deals",
        request_id="req-disp-3",
        result={
            "kind": "crm_card",
            "card": True,
            "title": "Open Deals",
            "table": {
                "columns": columns,
                "rows": [{
                    "dealname": "Deal 3",
                    "company": "Acme",
                    "dealstage": "Qualified To Buy",
                    "closedate": "2026-12-01",
                    "owner": "999888777",
                }],
            },
        },
    )
    enc_unresolved = json.dumps(render_response_blocks(r_unresolved), ensure_ascii=False)
    assert "*👤 Owner:* Owner not available" in enc_unresolved
    assert "999888777" not in enc_unresolved


# ==============================================================================
# Company creation owner assignment tests
# ==============================================================================

@pytest.mark.asyncio
async def test_amit_creates_company_assigned_to_amit(mock_tools, mock_safety):
    """Amit's Slack account creates company assigned to Amit Kumar's HubSpot owner ID."""
    ops = CRMOperations(mock_tools, mock_safety)
    mock_tools.resolve_company.return_value = AsyncMock(status="not_found", company=None)
    mock_tools.create_company.side_effect = lambda t, props: HubSpotCompany(id="comp-amit-1", properties=dict(props))

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_AMIT",
        actor_email="amit@company.com",
        message="Create company Acme Corp",
        request_id="req-comp-amit",
    )
    extraction = CRMIntentExtraction(intent="create_company", company_action="create", company_name="Acme Corp", confidence=0.95)
    validated = ValidatedExtraction(intent="create_company", properties={}, company_name="Acme Corp", contact_id=None)

    resp = await ops.respond(req, extraction, validated, "idemp-comp-amit")
    assert resp.status == "ok"
    assert resp.result["owner_id"] == "owner_amit_101"
    assert resp.result["owner_name"] == "Amit Kumar"

    call_props = mock_tools.create_company.call_args[0][1]
    assert call_props["hubspot_owner_id"] == "owner_amit_101"

    blocks = render_response_blocks(resp)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "👤 Owner: Amit Kumar" in encoded


@pytest.mark.asyncio
async def test_kaviya_creates_company_assigned_to_kaviya(mock_tools, mock_safety):
    """Kaviya's Slack account creates company assigned to Kaviya Murugan's HubSpot owner ID."""
    ops = CRMOperations(mock_tools, mock_safety)
    mock_tools.resolve_company.return_value = AsyncMock(status="not_found", company=None)
    mock_tools.create_company.side_effect = lambda t, props: HubSpotCompany(id="comp-kaviya-1", properties=dict(props))

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_KAVIYA",
        actor_email="kaviya@company.com",
        message="Create company TechNova",
        request_id="req-comp-kaviya",
    )
    extraction = CRMIntentExtraction(intent="create_company", company_action="create", company_name="TechNova", confidence=0.95)
    validated = ValidatedExtraction(intent="create_company", properties={}, company_name="TechNova", contact_id=None)

    resp = await ops.respond(req, extraction, validated, "idemp-comp-kaviya")
    assert resp.status == "ok"
    assert resp.result["owner_id"] == "owner_kaviya_102"
    assert resp.result["owner_name"] == "Kaviya Murugan"

    call_props = mock_tools.create_company.call_args[0][1]
    assert call_props["hubspot_owner_id"] == "owner_kaviya_102"

    blocks = render_response_blocks(resp)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "👤 Owner: Kaviya Murugan" in encoded


@pytest.mark.asyncio
async def test_vikas_creates_company_assigned_to_vikas(mock_tools, mock_safety):
    """Vikas's Slack account creates company assigned to Vikas Kumar's HubSpot owner ID."""
    ops = CRMOperations(mock_tools, mock_safety)
    mock_tools.resolve_company.return_value = AsyncMock(status="not_found", company=None)
    mock_tools.create_company.side_effect = lambda t, props: HubSpotCompany(id="comp-vikas-1", properties=dict(props))

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_VIKAS",
        actor_email="vikas@company.com",
        message="Create company Global Logistics",
        request_id="req-comp-vikas",
    )
    extraction = CRMIntentExtraction(intent="create_company", company_action="create", company_name="Global Logistics", confidence=0.95)
    validated = ValidatedExtraction(intent="create_company", properties={}, company_name="Global Logistics", contact_id=None)

    resp = await ops.respond(req, extraction, validated, "idemp-comp-vikas")
    assert resp.status == "ok"
    assert resp.result["owner_id"] == "owner_vikas_103"
    assert resp.result["owner_name"] == "Vikas Kumar"

    call_props = mock_tools.create_company.call_args[0][1]
    assert call_props["hubspot_owner_id"] == "owner_vikas_103"

    blocks = render_response_blocks(resp)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "👤 Owner: Vikas Kumar" in encoded


@pytest.mark.asyncio
async def test_unmatched_user_creates_company_fails_safely(mock_tools, mock_safety):
    """Unmatched Slack user creating company fails safely without creating company or assigning default owner."""
    ops = CRMOperations(mock_tools, mock_safety)
    mock_tools.resolve_company.return_value = AsyncMock(status="not_found", company=None)

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_UNKNOWN",
        actor_email="unregistered@outside.com",
        message="Create company Rogue Co",
        request_id="req-comp-unknown",
    )
    extraction = CRMIntentExtraction(intent="create_company", company_action="create", company_name="Rogue Co", confidence=0.95)
    validated = ValidatedExtraction(intent="create_company", properties={}, company_name="Rogue Co", contact_id=None)

    resp = await ops.respond(req, extraction, validated, "idemp-comp-fail")
    assert resp.status == "owner_mapping_unavailable"
    assert "HubSpot owner mapping is unavailable" in resp.text
    mock_tools.create_company.assert_not_called()


@pytest.mark.asyncio
async def test_company_owner_persistence_verification_failed(mock_tools, mock_safety):
    """If HubSpot fails to persist assigned owner on company, failure is raised."""
    ops = CRMOperations(mock_tools, mock_safety)
    mock_tools.resolve_company.return_value = AsyncMock(status="not_found", company=None)
    mock_tools.create_company.return_value = HubSpotCompany(id="comp-no-owner", properties={"name": "NoOwner Co", "hubspot_owner_id": None})
    mock_tools.get_company.return_value = HubSpotCompany(id="comp-no-owner", properties={"name": "NoOwner Co", "hubspot_owner_id": None})

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_AMIT",
        actor_email="amit@company.com",
        message="Create company NoOwner Co",
        request_id="req-comp-verify-fail",
    )
    extraction = CRMIntentExtraction(intent="create_company", company_action="create", company_name="NoOwner Co", confidence=0.95)
    validated = ValidatedExtraction(intent="create_company", properties={}, company_name="NoOwner Co", contact_id=None)

    resp = await ops.respond(req, extraction, validated, "idemp-comp-verify-fail")
    assert resp.status == "owner_assignment_failed"
    assert "could not be verified in HubSpot" in resp.text


# ==============================================================================
# Deal creation owner assignment tests
# ==============================================================================

@pytest.mark.asyncio
async def test_amit_creates_deal_assigned_to_amit(mock_tools, mock_safety):
    """Amit's Slack account creates deal assigned to Amit Kumar's HubSpot owner ID."""
    ops = CRMOperations(mock_tools, mock_safety)
    mock_tools.resolve_deal.return_value = AsyncMock(status="not_found", deal=None)
    mock_tools.deal_pipelines.return_value = [
        HubSpotPipeline(id="default", label="Sales Pipeline", stages=[HubSpotPipelineStage(id="appointmentscheduled", label="Appointment Scheduled")])
    ]
    mock_tools.create_deal.side_effect = lambda t, props: HubSpotDeal(id="deal-amit-1", properties=dict(props))

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_AMIT",
        actor_email="amit@company.com",
        message="Create deal Enterprise Expansion for $50,000",
        request_id="req-deal-amit",
    )
    extraction = CRMIntentExtraction(intent="create_deal", deal_action="create", deal_name="Enterprise Expansion", confidence=0.95)
    validated = ValidatedExtraction(intent="create_deal", properties={}, company_name=None, deal_name="Enterprise Expansion", deal_amount="50000", contact_id=None)

    resp = await ops.respond(req, extraction, validated, "idemp-deal-amit")
    assert resp.status == "ok"
    assert resp.result["owner_id"] == "owner_amit_101"
    assert resp.result["owner_name"] == "Amit Kumar"

    call_props = mock_tools.create_deal.call_args[0][1]
    assert call_props["hubspot_owner_id"] == "owner_amit_101"

    blocks = render_response_blocks(resp)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "👤 Owner: Amit Kumar" in encoded


@pytest.mark.asyncio
async def test_kaviya_creates_deal_assigned_to_kaviya(mock_tools, mock_safety):
    """Kaviya's Slack account creates deal assigned to Kaviya Murugan's HubSpot owner ID."""
    ops = CRMOperations(mock_tools, mock_safety)
    mock_tools.resolve_deal.return_value = AsyncMock(status="not_found", deal=None)
    mock_tools.deal_pipelines.return_value = [
        HubSpotPipeline(id="default", label="Sales Pipeline", stages=[HubSpotPipelineStage(id="appointmentscheduled", label="Appointment Scheduled")])
    ]
    mock_tools.create_deal.side_effect = lambda t, props: HubSpotDeal(id="deal-kaviya-1", properties=dict(props))

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_KAVIYA",
        actor_email="kaviya@company.com",
        message="Create deal Q4 Upsell",
        request_id="req-deal-kaviya",
    )
    extraction = CRMIntentExtraction(intent="create_deal", deal_action="create", deal_name="Q4 Upsell", confidence=0.95)
    validated = ValidatedExtraction(intent="create_deal", properties={}, company_name=None, deal_name="Q4 Upsell", contact_id=None)

    resp = await ops.respond(req, extraction, validated, "idemp-deal-kaviya")
    assert resp.status == "ok"
    assert resp.result["owner_id"] == "owner_kaviya_102"
    assert resp.result["owner_name"] == "Kaviya Murugan"

    call_props = mock_tools.create_deal.call_args[0][1]
    assert call_props["hubspot_owner_id"] == "owner_kaviya_102"

    blocks = render_response_blocks(resp)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "👤 Owner: Kaviya Murugan" in encoded


@pytest.mark.asyncio
async def test_vikas_creates_deal_assigned_to_vikas(mock_tools, mock_safety):
    """Vikas's Slack account creates deal assigned to Vikas Kumar's HubSpot owner ID."""
    ops = CRMOperations(mock_tools, mock_safety)
    mock_tools.resolve_deal.return_value = AsyncMock(status="not_found", deal=None)
    mock_tools.deal_pipelines.return_value = [
        HubSpotPipeline(id="default", label="Sales Pipeline", stages=[HubSpotPipelineStage(id="appointmentscheduled", label="Appointment Scheduled")])
    ]
    mock_tools.create_deal.side_effect = lambda t, props: HubSpotDeal(id="deal-vikas-1", properties=dict(props))

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_VIKAS",
        actor_email="vikas@company.com",
        message="Create deal Global Freight Contract",
        request_id="req-deal-vikas",
    )
    extraction = CRMIntentExtraction(intent="create_deal", deal_action="create", deal_name="Global Freight Contract", confidence=0.95)
    validated = ValidatedExtraction(intent="create_deal", properties={}, company_name=None, deal_name="Global Freight Contract", contact_id=None)

    resp = await ops.respond(req, extraction, validated, "idemp-deal-vikas")
    assert resp.status == "ok"
    assert resp.result["owner_id"] == "owner_vikas_103"
    assert resp.result["owner_name"] == "Vikas Kumar"

    call_props = mock_tools.create_deal.call_args[0][1]
    assert call_props["hubspot_owner_id"] == "owner_vikas_103"

    blocks = render_response_blocks(resp)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "👤 Owner: Vikas Kumar" in encoded


@pytest.mark.asyncio
async def test_unmatched_user_creates_deal_fails_safely(mock_tools, mock_safety):
    """Unmatched Slack user creating deal fails safely without creating deal."""
    ops = CRMOperations(mock_tools, mock_safety)
    mock_tools.resolve_deal.return_value = AsyncMock(status="not_found", deal=None)
    mock_tools.deal_pipelines.return_value = [
        HubSpotPipeline(id="default", label="Sales Pipeline", stages=[HubSpotPipelineStage(id="appointmentscheduled", label="Appointment Scheduled")])
    ]

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_UNKNOWN",
        actor_email="unregistered@outside.com",
        message="Create deal Mystery Deal",
        request_id="req-deal-unknown",
    )
    extraction = CRMIntentExtraction(intent="create_deal", deal_action="create", deal_name="Mystery Deal", confidence=0.95)
    validated = ValidatedExtraction(intent="create_deal", properties={}, company_name=None, deal_name="Mystery Deal", contact_id=None)

    resp = await ops.respond(req, extraction, validated, "idemp-deal-fail")
    assert resp.status == "owner_mapping_unavailable"
    assert "HubSpot owner mapping is unavailable" in resp.text
    mock_tools.create_deal.assert_not_called()


@pytest.mark.asyncio
async def test_deal_owner_persistence_verification_failed(mock_tools, mock_safety):
    """If HubSpot fails to persist assigned owner on deal, failure is raised."""
    ops = CRMOperations(mock_tools, mock_safety)
    mock_tools.resolve_deal.return_value = AsyncMock(status="not_found", deal=None)
    mock_tools.deal_pipelines.return_value = [
        HubSpotPipeline(id="default", label="Sales Pipeline", stages=[HubSpotPipelineStage(id="appointmentscheduled", label="Appointment Scheduled")])
    ]
    mock_tools.create_deal.return_value = HubSpotDeal(id="deal-no-owner", properties={"dealname": "FailedOwner Deal", "hubspot_owner_id": None})
    mock_tools.get_deal.return_value = HubSpotDeal(id="deal-no-owner", properties={"dealname": "FailedOwner Deal", "hubspot_owner_id": None})

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_AMIT",
        actor_email="amit@company.com",
        message="Create deal FailedOwner Deal",
        request_id="req-deal-verify-fail",
    )
    extraction = CRMIntentExtraction(intent="create_deal", deal_action="create", deal_name="FailedOwner Deal", confidence=0.95)
    validated = ValidatedExtraction(intent="create_deal", properties={}, company_name=None, deal_name="FailedOwner Deal", contact_id=None)

    resp = await ops.respond(req, extraction, validated, "idemp-deal-verify-fail")
    assert resp.status == "owner_assignment_failed"
    assert "could not be verified in HubSpot" in resp.text


@pytest.mark.asyncio
async def test_combined_company_contact_deal_creation_shares_target_owner(mock_tools, mock_safety):
    """Creating Company, Contact, and Deal together assigns matched owner to all three records."""
    ops = CRMOperations(mock_tools, mock_safety)
    mock_tools.resolve_company.return_value = AsyncMock(status="not_found", company=None)
    mock_tools.resolve_contact.return_value = AsyncMock(contact=None)
    mock_tools.resolve_deal.return_value = AsyncMock(status="not_found", deal=None)
    mock_tools.deal_pipelines.return_value = [
        HubSpotPipeline(id="default", label="Sales Pipeline", stages=[HubSpotPipelineStage(id="appointmentscheduled", label="Appointment Scheduled")])
    ]
    mock_tools.create_company.side_effect = lambda t, props: HubSpotCompany(id="comp-combo", properties=dict(props))
    mock_tools.create_contact.side_effect = lambda t, props: HubSpotContact(id="cont-combo", properties=dict(props))
    mock_tools.create_deal.side_effect = lambda t, props: HubSpotDeal(id="deal-combo", properties=dict(props))

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_AMIT",
        actor_email="amit@company.com",
        message="Create Acme Co, add contact John Smith john@acme.com, and create deal Big Deal",
        request_id="req-combo",
    )
    extraction = CRMIntentExtraction(
        intent="multi_step",
        company_action="create",
        company_name="Acme Co",
        contact_action="create",
        deal_action="create",
        deal_name="Big Deal",
        confidence=0.95,
    )
    validated = ValidatedExtraction(
        intent="multi_step",
        company_name="Acme Co",
        properties={"firstname": "John", "lastname": "Smith", "email": "john@acme.com"},
        deal_name="Big Deal",
        contact_id=None,
    )

    resp = await ops.respond(req, extraction, validated, "idemp-combo")
    assert resp.status == "ok"
    assert resp.result["owner_id"] == "owner_amit_101"
    assert resp.result["owner_name"] == "Amit Kumar"

    assert mock_tools.create_company.call_args[0][1]["hubspot_owner_id"] == "owner_amit_101"
    assert mock_tools.create_contact.call_args[0][1]["hubspot_owner_id"] == "owner_amit_101"
    assert mock_tools.create_deal.call_args[0][1]["hubspot_owner_id"] == "owner_amit_101"


# ==============================================================================
# Detailed & simple company creation intent-routing tests
# ==============================================================================

@pytest.mark.asyncio
async def test_detailed_company_creation_extracts_all_fields_and_assigns_owner(agent, mock_tools):
    """Detailed company creation extracts phone, city, country, industry, assigns owner, and never asks for contact."""
    agent._ai_service.generate.side_effect = Exception("Use rule parser")
    mock_tools.resolve_company.return_value = AsyncMock(status="not_found", company=None)
    mock_tools.create_company.side_effect = lambda t, props: HubSpotCompany(id="comp-detailed-1", properties=dict(props))

    message = (
        "Create company named Owner Test Google Chennai with phone 3456765434, "
        "city Chennai, country India, and industry Software and Development."
    )
    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_AMIT",
        actor_email="amit@company.com",
        message=message,
        request_id="req-detailed-comp",
    )

    resp = await agent.respond(req)

    assert resp.status == "ok"
    assert "Which contact?" not in resp.text
    assert "Owner Test Google Chennai" in resp.text
    assert resp.result["owner_id"] == "owner_amit_101"
    assert resp.result["owner_name"] == "Amit Kumar"

    call_props = mock_tools.create_company.call_args[0][1]
    assert call_props["name"] == "Owner Test Google Chennai"
    assert call_props["phone"] == "3456765434"
    assert call_props["city"] == "Chennai"
    assert call_props["country"] == "India"
    assert call_props["industry"] == "Software and Development"
    assert call_props["hubspot_owner_id"] == "owner_amit_101"

    blocks = render_response_blocks(resp)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "👤 Owner: Amit Kumar" in encoded


@pytest.mark.asyncio
async def test_simple_company_creation_preserves_behavior(agent, mock_tools):
    """Simple company creation preserves existing working behavior."""
    agent._ai_service.generate.side_effect = Exception("Use rule parser")
    mock_tools.resolve_company.return_value = AsyncMock(status="not_found", company=None)
    mock_tools.create_company.side_effect = lambda t, props: HubSpotCompany(id="comp-simple-1", properties=dict(props))

    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_VIKAS",
        actor_email="vikas@company.com",
        message="create company name with Google",
        request_id="req-simple-comp",
    )

    resp = await agent.respond(req)

    assert resp.status == "ok"
    assert "Which contact?" not in resp.text
    assert resp.result["owner_id"] == "owner_vikas_103"
    assert resp.result["owner_name"] == "Vikas Kumar"

    call_props = mock_tools.create_company.call_args[0][1]
    assert "Google" in call_props["name"]
    assert call_props["hubspot_owner_id"] == "owner_vikas_103"


@pytest.mark.asyncio
async def test_ai_extracted_detailed_company_creation(agent, mock_tools):
    """When AI service returns create_company extraction with fields, company is created with properties and owner."""
    extracted = CRMIntentExtraction(
        intent="create_company",
        company_name="Owner Test Google Chennai",
        phone="3456765434",
        city="Chennai",
        country="India",
        industry="Software and Development",
        company_action="create",
        confidence=0.98,
    )
    agent._ai_service.generate.return_value = extracted
    mock_tools.resolve_company.return_value = AsyncMock(status="not_found", company=None)
    mock_tools.create_company.side_effect = lambda t, props: HubSpotCompany(id="comp-ai-1", properties=dict(props))

    message = (
        "Create company named Owner Test Google Chennai with phone 3456765434, "
        "city Chennai, country India, and industry Software and Development."
    )
    req = AgentRequest(
        tenant_id="tenant-a",
        actor_id="U_KAVIYA",
        actor_email="kaviya@company.com",
        message=message,
        request_id="req-ai-comp",
    )

    resp = await agent.respond(req)

    assert resp.status == "ok"
    assert "Which contact?" not in resp.text
    assert resp.result["owner_id"] == "owner_kaviya_102"
    assert resp.result["owner_name"] == "Kaviya Murugan"

    call_props = mock_tools.create_company.call_args[0][1]
    assert call_props["name"] == "Owner Test Google Chennai"
    assert call_props["phone"] == "3456765434"
    assert call_props["city"] == "Chennai"
    assert call_props["country"] == "India"
    assert call_props["industry"] == "Software and Development"
    assert call_props["hubspot_owner_id"] == "owner_kaviya_102"
