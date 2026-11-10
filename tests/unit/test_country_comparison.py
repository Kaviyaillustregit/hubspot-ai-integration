from datetime import UTC, datetime
import pytest

from app.agent.crm_intelligence import plan_crm_query
from app.agent.schemas import AgentRequest, CRMIntentExtraction
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.provider import AIProvider
from app.ai.service import AIService
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotContact,
    HubSpotContactsPage,
    HubSpotDealsPage,
)
from app.integrations.hubspot.properties import HubSpotPropertyDefinition
from app.integrations.slack.home import render_response_blocks


class MockProvider(AIProvider):
    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(
                intent="crm_question",
                confidence=1.0,
                question=variables["message"],
            )
        if prompt_name == "crm-intelligence/v1":
            return plan_crm_query(variables["query"])
        raise AssertionError(prompt_name)


class MockCompanies:
    async def list_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=[])

    async def search_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=[])


class MockContacts:
    def __init__(self, contacts=None):
        self.contacts = contacts or []

    async def get_hubspot_account_id(self, context):
        return "portal-123"

    async def list_contacts(self, context, **kwargs):
        offset = int(kwargs.get("after") or 0)
        chunk = self.contacts[offset : offset + 100]
        more = offset + 100 < len(self.contacts)
        return HubSpotContactsPage(
            results=chunk, next_after=str(offset + 100) if more else None
        )

    async def search_contacts(self, context, **kwargs):
        return HubSpotContactsPage(results=self.contacts)


class MockPropertiesService:
    async def list_properties(self, context, object_type):
        if object_type == "contacts":
            return [
                HubSpotPropertyDefinition(name="firstname", label="First Name"),
                HubSpotPropertyDefinition(name="lastname", label="Last Name"),
                HubSpotPropertyDefinition(name="email", label="Email"),
                HubSpotPropertyDefinition(name="jobtitle", label="Job Title"),
                HubSpotPropertyDefinition(name="city", label="City"),
                HubSpotPropertyDefinition(name="country", label="Country"),
                HubSpotPropertyDefinition(name="lifecyclestage", label="Lifecycle Stage"),
                HubSpotPropertyDefinition(name="hs_lead_status", label="Lead Status"),
            ]
        return []


class MockDeals:
    async def list_deals(self, context, **kwargs):
        return HubSpotDealsPage(results=[])

    async def list_pipelines(self, context):
        return []


def make_test_agent(contacts=None):
    return AccountIntelligenceAgent(
        HubSpotToolRegistry(
            MockCompanies(),
            MockContacts(contacts),
            MockDeals(),
            properties=MockPropertiesService(),
        ),
        AIService(MockProvider()),
        object(),
    )


def ask(message: str) -> AgentRequest:
    return AgentRequest(
        tenant_id="tenant-test",
        actor_id="user-test",
        channel_id="C123",
        message=message,
        request_id="r-1",
    )


def test_plan_country_comparison_query():
    plan = plan_crm_query("How many contacts do we have in India vs the US?")
    assert plan.object == "contact"
    assert plan.group_by == "country"
    assert plan.chart_requested is True
    assert plan.chart_type == "bar"
    assert "country" in plan.properties
    assert any(f.field == "country" and f.operator == "in" for f in plan.filters)


@pytest.mark.asyncio
async def test_agent_country_comparison_generates_chart_and_accurate_footer():
    # 50 India contacts and 32 US contacts = 82 total contacts
    contacts = [
        HubSpotContact(
            id=f"in-{i}",
            properties={"firstname": f"IndiaUser{i}", "country": "India"},
        )
        for i in range(50)
    ] + [
        HubSpotContact(
            id=f"us-{i}",
            properties={"firstname": f"USUser{i}", "country": "US"},
        )
        for i in range(32)
    ]
    agent = make_test_agent(contacts=contacts)
    response = await agent.respond(ask("How many contacts do we have in India vs the US?"))

    assert response.status == "ok"
    result = response.result

    # 1. Chart title must be "Contacts by Country", NOT "Contacts by Lifecycle Stage"
    assert result.get("chart_title") == "Contacts by Country"
    assert result.get("chart_type") == "bar"
    assert result.get("chart_url") is not None

    # 2. Separate India and US contact counts
    chart_data = result["chart"]["data"]
    labels = chart_data["labels"]
    values = chart_data["datasets"][0]["data"]
    assert "India" in labels
    assert "US" in labels
    assert values[labels.index("India")] == 50.0
    assert values[labels.index("US")] == 32.0

    # 3. Slack App Home blocks:
    blocks = render_response_blocks(response)
    blocks_str = str(blocks)
    assert "Contacts by Country" in blocks_str
    assert "India" in blocks_str
    assert "*50* contacts" in blocks_str
    assert "*32* contacts" in blocks_str
    # Total count footer must be 82 contacts, NOT deals
    assert "*82* contacts" in blocks_str
    assert "82 deals" not in blocks_str
    assert "Count Id: 82" not in blocks_str
