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
from app.integrations.hubspot.models import HubSpotContact
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
