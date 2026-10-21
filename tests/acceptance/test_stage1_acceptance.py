"""End-to-end Stage 1 acceptance coverage through signed Slack Home interactions."""

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from time import time
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.schemas import CRMIntentExtraction
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.provider import AIProvider
from app.ai.service import AIService
from app.api.app import create_app
from app.api.slack import (
    get_action_safety,
    get_agent,
    get_conversation_history,
    get_slack_client,
)
from app.core.config import Settings
from app.integrations.hubspot.deals import HubSpotDealsClient
from app.integrations.hubspot.oauth import StoredOAuthToken
from app.integrations.slack.home import REQUEST_INPUT_ACTION, SUBMIT_ACTION
from app.services.hubspot_deals import HubSpotDealsService

SIGNING_SECRET = "stage1-acceptance-signing-secret"
TOKEN = StoredOAuthToken(
    tenant_id="tenant-a",
    hubspot_account_id="portal-acceptance",
    encrypted_access_token="not-used",
    encrypted_refresh_token="not-used",
    expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    scopes=["crm.objects.deals.read"],
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
)

STAGES = [
    {"id": "s-new", "label": "Qualification", "displayOrder": 0, "metadata": {"isClosed": "false"}},
    {"id": "s-prop", "label": "Proposal", "displayOrder": 1, "metadata": {"isClosed": "false"}},
    {
        "id": "s-won",
        "label": "Closed Won",
        "displayOrder": 2,
        "metadata": {"isClosed": "true", "probability": "1.0"},
    },
    {
        "id": "s-lost",
        "label": "Closed Lost",
        "displayOrder": 3,
        "metadata": {"isClosed": "true", "probability": "0.0"},
    },
]


def _fixture_records() -> list[dict]:
    dates = [
        "2026-07-01",
        "2026-09-30",
        "2026-10-01",
        "2026-08-15",
        "2026-06-30",
        "2026-09-30T23:30:00Z",
        None,
    ]
    stages = ["s-new", "s-prop", "s-won", "s-lost"]
    pipelines = ["default", "services"]
    records = []
    for number in range(1, 131):
        records.append(
            {
                "id": str(number),
                "properties": {
                    "dealname": f"Acceptance Deal {number}",
                    "dealstage": stages[(number - 1) % len(stages)],
                    "pipeline": pipelines[(number - 1) % len(pipelines)],
                    "hubspot_owner_id": str(17 + (number - 1) % 3),
                    "amount": None if number % 11 == 0 else str(number * 100),
                    "deal_currency_code": "USD" if number % 2 else "INR",
                    "closedate": dates[(number - 1) % len(dates)],
                    "createdate": f"2026-{(number - 1) % 12 + 1:02d}-15T12:00:00Z",
                },
            }
        )
    return records


RECORDS = _fixture_records()


def _queries() -> list[tuple[str, str]]:
    groups = {
        "list": [
            "Show me all HubSpot deals",
            "List all deals",
            "Display every deal",
            "Find all HubSpot opportunities",
            "Retrieve all deals",
            "Get every deal",
            "Show all opportunities",
            "List every HubSpot deal",
            "What deals do I have",
            "Show me the deals",
        ],
        "latest": [
            (
                "Show me the latest 5 HubSpot deals with their deal names, amounts, stages, "
                "and closing dates"
            ),
            "List the newest 5 HubSpot deals",
            "Display the 5 most recent deals",
            "Get latest 5 opportunities",
            "Find the newest 5 deals",
            "Show the latest 5 HubSpot opportunities",
            "Give me the 5 most recent deals",
            "Retrieve newest 5 deals",
            "List the latest 5 opportunities",
            "Show me 5 latest HubSpot deals",
        ],
        "stage_counts": [
            "How many deals are in each stage",
            "Count deals by stage",
            "Show a count for every deal stage",
            "Give me the stage breakdown by deal count",
            "Number of deals per stage",
            "Group deals by stage and count them",
            "How many HubSpot opportunities are in each stage",
            "Show counts for every stage",
            "Count opportunities per deal stage",
            "Display deal totals for each stage",
        ],
        "q3": [
            "Show me all HubSpot deals closing in Q3 2026",
            "List all deals closing during Q3 2026",
            "Find HubSpot deals for Q3 2026 by close date",
            "Display deals closing in Q3 2026",
            "Get opportunities with a Q3 2026 closing date",
            "Show me deals closing in quarter Q3 2026",
            "Retrieve deals with close date in Q3 2026",
            "List opportunities closing during Q3 2026",
            "Which deals close in Q3 2026",
            "Show all HubSpot deals for Q3 2026 closing",
        ],
        "won": [
            "Show deals in the Closed Won stage",
            "List deals in Closed Won stage",
            "Find deals stage is Closed Won",
            "Display deals with stage equals Closed Won",
            "Get opportunities in Closed Won stage",
            "Show HubSpot deals where stage is Closed Won",
            "Retrieve deals with deal stage: Closed Won",
            "List opportunities stage is Closed Won",
            "Find all deals in the Closed Won stage",
            "Show every deal whose stage is Closed Won",
        ],
        "amount": [
            "Show deals with amount over 5000",
            "List deals over $5000",
            "Find deals whose amount is greater than 5000",
            "Display deals above $5,000",
            "Get deals with value over 5000",
            "Show opportunities with amount > 5000",
            "Retrieve deals greater than 5000",
            "List opportunities above $5,000",
            "Find deals with deal value over 5000",
            "Show all HubSpot deals over $5000",
        ],
        "pipeline": [
            "Show deals in the Sales pipeline",
            "List deals in Sales pipeline",
            "Find deals where pipeline is Sales",
            "Display HubSpot deals in Sales pipeline",
            "Get opportunities in the Sales pipeline",
            "Show all deals pipeline is Sales",
            "Retrieve deals from Sales pipeline",
            "List opportunities where pipeline equals Sales",
            "Find HubSpot deals in the Sales pipeline",
            "Show me every deal in Sales pipeline",
        ],
        "owner": [
            "Show deals where owner is 17",
            "List deals owner is 17",
            "Find deals with owner id 17",
            "Display HubSpot deals assigned to owner is 17",
            "Get opportunities where owner equals 17",
            "Show all deals with owner ID 17",
            "Retrieve deals owner is 17",
            "List opportunities assigned to owner 17",
            "Find deals whose owner is 17",
            "Show HubSpot deals with owner id is 17",
        ],
        "exact_date": [
            "Show deals closing on September 30, 2026",
            "List deals closing on 2026-09-30",
            "Find deals with close date September 30, 2026",
            "Display deals closing on Sep 30, 2026",
            "Get HubSpot deals closing September 30, 2026",
            "Show opportunities with close date 2026-09-30",
            "Retrieve deals closing on September 30 2026",
            "List opportunities closing Sep 30, 2026",
            "Which deals close on 2026-09-30",
            "Show all deals closing on September 30, 2026",
        ],
        "month_group": [
            "Group deals by month",
            "Show deals grouped by month",
            "Count deals per month",
            "How many deals close each month",
            "Display deals by month",
            "Break down deals by month",
            "Show monthly deal counts",
            "Group HubSpot deals by month",
            "List deals per month",
            "Show deal distribution each month",
        ],
    }
    return [(query + ".", category) for category, prompts in groups.items() for query in prompts]


CASES = _queries()
assert len(CASES) == 100


class _TokenProvider:
    async def get_access_token(self, tenant_id):
        assert tenant_id == "tenant-a"
        return "fixture-token", TOKEN


class _AIProvider(AIProvider):
    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(
                intent="crm_question",
                confidence=1.0,
                question=variables["message"],
                query="all_deals",
            )
        if prompt_name == "crm-intelligence/v1":
            from app.agent.crm_intelligence import plan_crm_query

            return plan_crm_query(variables["query"])
        raise AssertionError(prompt_name)


class _Companies:
    async def list_companies(self, context, **kwargs):
        from app.integrations.hubspot.models import HubSpotCompaniesPage

        return HubSpotCompaniesPage(results=[])

    async def search_companies(self, context, **kwargs):
        from app.integrations.hubspot.models import HubSpotCompaniesPage

        return HubSpotCompaniesPage(results=[])


class _Contacts:
    async def get_hubspot_account_id(self, context):
        return "portal-acceptance"


class _SlackClient:
    def __init__(self):
        self.views = []

    async def publish_home_view(self, user_id, view):
        self.views.append((user_id, view))

    async def post_message(self, channel, text, **kwargs):
        raise AssertionError("App Home acceptance must not post Slack messages")


class _Safety:
    async def recent_actions(self, *, tenant_id, actor_id, limit=5):
        assert tenant_id == "tenant-a" and actor_id == "U1"
        return []


class _History:
    def __init__(self):
        self.responses = []

    async def latest(self, **kwargs):
        return []

    async def save_completed(self, **kwargs):
        self.responses.append(kwargs["response"])
        return True


def _signed_post(client, query: str, sequence: int):
    payload = {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1", "team_id": "T1"},
        "view": {
            "type": "home",
            "blocks": [{"type": "input", "block_id": "home_request:fixture"}],
            "state": {
                "values": {
                    "home_request:fixture": {
                        REQUEST_INPUT_ACTION: {"type": "plain_text_input", "value": query}
                    }
                }
            },
        },
        "actions": [
            {"action_id": SUBMIT_ACTION, "value": "send", "action_ts": f"1792.{sequence:06d}"}
        ],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    timestamp = str(int(time()))
    digest = hmac.new(
        SIGNING_SECRET.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/api/v1/slack/interactions",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": f"v0={digest}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


@pytest.fixture(scope="module")
def acceptance_environment():
    records = RECORDS

    def hubspot_response(request: httpx.Request):
        if request.url.path.endswith("/crm/v3/pipelines/deals"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "default", "label": "Sales", "stages": STAGES},
                        {"id": "services", "label": "Services", "stages": STAGES},
                    ]
                },
            )
        if request.url.path.endswith("/crm/v3/objects/deals"):
            start = int(request.url.params.get("after", "0"))
            limit = int(request.url.params.get("limit", "100"))
            end = min(start + limit, len(records))
            page = {"results": records[start:end]}
            if end < len(records):
                page["paging"] = {"next": {"after": str(end)}}
            return httpx.Response(200, json=page)
        raise AssertionError(f"Unexpected HubSpot fixture request: {request.url.path}")

    transport = httpx.MockTransport(hubspot_response)
    async_client = httpx.AsyncClient(transport=transport)
    deals = HubSpotDealsService(HubSpotDealsClient(Settings(), async_client), _TokenProvider())
    agent = AccountIntelligenceAgent(
        HubSpotToolRegistry(_Companies(), _Contacts(), deals),
        AIService(_AIProvider()),
        object(),
    )
    slack = _SlackClient()
    history = _History()
    app = create_app(
        Settings(
            log_level="CRITICAL",
            database_url="postgresql+asyncpg://test:test@127.0.0.1:1/unused",
            slack_signing_secret=SIGNING_SECRET,
            slack_team_tenant_map='{"T1":"tenant-a"}',
        )
    )
    app.dependency_overrides[get_agent] = lambda: agent
    app.dependency_overrides[get_slack_client] = lambda: slack
    app.dependency_overrides[get_action_safety] = lambda: _Safety()
    app.dependency_overrides[get_conversation_history] = lambda: history
    with TestClient(app) as client:
        yield client, slack, history


def _independent_expected_ids(category: str) -> list[str]:
    def close_date(record):
        value = record["properties"]["closedate"]
        return (value or "")[:10]

    if category == "list":
        return [r["id"] for r in sorted(RECORDS, key=lambda r: close_date(r), reverse=True)]
    if category == "latest":
        return [r["id"] for r in sorted(RECORDS, key=lambda r: close_date(r), reverse=True)[:5]]
    if category == "q3":
        return [r["id"] for r in RECORDS if "2026-07-01" <= close_date(r) <= "2026-09-30"]
    if category == "won":
        return [r["id"] for r in RECORDS if r["properties"]["dealstage"] == "s-won"]
    if category == "amount":
        return [
            r["id"]
            for r in RECORDS
            if r["properties"]["amount"] and int(r["properties"]["amount"]) > 5000
        ]
    if category == "pipeline":
        return [r["id"] for r in RECORDS if r["properties"]["pipeline"] == "default"]
    if category == "owner":
        return [r["id"] for r in RECORDS if r["properties"]["hubspot_owner_id"] == "17"]
    if category == "exact_date":
        return [r["id"] for r in RECORDS if close_date(r) == "2026-09-30"]
    return []


@pytest.mark.parametrize("query,category", CASES)
def test_signed_slack_stage1_acceptance_cases(acceptance_environment, query, category):
    client, slack, history = acceptance_environment
    before = len(slack.views)
    response = _signed_post(client, query, before + 1)
    assert response.status_code == 200
    user_id, view = slack.views[-1]
    assert user_id == "U1"
    blocks_text = json.dumps(view["blocks"])
    assert history.responses[-1].result["kind"] == "crm_intelligence"
    assert view["type"] == "home"
    assert len(view["blocks"]) <= 100
    assert (
        "*Latest deals*" in blocks_text
        or "*Deals" in blocks_text
        or "*Here's what I found*" in blocks_text
    )
    assert "Acceptance Deal" in blocks_text or "deals" in blocks_text.lower()

    # The signed endpoint calls the real agent and its final Block Kit renderer.
    # Inspect the persisted response projection in the published Home view.
    expected_ids = _independent_expected_ids(category)
    if category in {"list", "latest"}:
        assert (
            ("showing 20 of 130 deals" in blocks_text.lower())
            if category == "list"
            else ("showing 5 of 130 deals" in blocks_text.lower())
        )
        if category == "latest":
            expected_ids = expected_ids[:5]
    elif category in {"stage_counts", "month_group"}:
        assert "130 deals" in blocks_text.lower()
        if category == "stage_counts":
            stage_ids = {
                "Qualification": "s-new",
                "Proposal": "s-prop",
                "Closed Won": "s-won",
                "Closed Lost": "s-lost",
            }
            for label, stage_id in stage_ids.items():
                count = sum(1 for row in RECORDS if row["properties"]["dealstage"] == stage_id)
                assert f"{label}*: {count} deals" in blocks_text
        else:
            months = {
                row["properties"]["closedate"][:7]
                for row in RECORDS
                if row["properties"]["closedate"]
            }
            for month in months:
                assert month in blocks_text
    else:
        assert (
            f"{len(expected_ids)} deal" in blocks_text.lower()
            or f"{len(expected_ids)} deals" in blocks_text.lower()
        )
    if category in {"list", "latest"}:
        for record_id in expected_ids[:8]:
            assert f"/record/0-3/{record_id}|View in HubSpot" in blocks_text, (
                history.responses[-1].result.get("summary"),
                blocks_text,
            )
        for record_id in expected_ids[:8] if category == "list" else expected_ids:
            assert len(re.findall(rf"Acceptance Deal {record_id}(?!\d)", blocks_text)) == 1
        assert "*Closing date:*" in blocks_text
        assert "USD " in blocks_text or "INR " in blocks_text


def test_acceptance_fixture_exercises_pagination_and_has_expected_categories():
    assert len(RECORDS) == 130
    assert len(CASES) == 100
    assert {category for _, category in CASES} == {
        "list",
        "latest",
        "stage_counts",
        "q3",
        "won",
        "amount",
        "pipeline",
        "owner",
        "exact_date",
        "month_group",
    }
