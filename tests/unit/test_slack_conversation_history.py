from types import SimpleNamespace

import pytest

from app.agent.schemas import AgentResponse
from app.db.history_safety import (
    migration_database_url,
    validate_history_test_database,
    validate_production_history_migration_target,
    validate_production_migration_revision,
)
from app.integrations.slack.home import (
    SUBMIT_ACTION,
    VIEW_FULL_RESPONSE_ACTION,
    _compact_assistant_response,
    _full_history_response_blocks,
    _history_preview_blocks,
    _split_history_response,
    build_home_view,
    parse_home_interaction,
    render_response_blocks,
)
from app.repositories.slack_conversation import SlackConversationRepository
from app.services.slack_conversation import response_render_data


def test_history_database_guard_accepts_only_dedicated_local_target():
    validate_history_test_database(
        "postgresql+asyncpg://hubspot_history_test:local-pass@localhost:5432/"
        "hubspot_ai_history_test"
    )
    for url in (
        "postgresql+asyncpg://app:app@localhost:5432/hubspot_ai",
        "postgresql+asyncpg://hubspot_history_test:pass@db.example:5432/hubspot_ai_history_test",
        "sqlite+aiosqlite:///:memory:",
    ):
        with pytest.raises(RuntimeError):
            validate_history_test_database(url)


def test_history_migration_target_fails_closed_for_shared_database(monkeypatch):
    monkeypatch.delenv("HISTORY_TEST_MODE", raising=False)
    with pytest.raises(RuntimeError, match="shared database hubspot_ai"):
        migration_database_url("postgresql+asyncpg://app:app@localhost:5432/hubspot_ai")

    monkeypatch.setenv("HISTORY_TEST_MODE", "1")
    monkeypatch.setenv(
        "HISTORY_TEST_DATABASE_URL",
        "postgresql+asyncpg://hubspot_history_test:local-pass@localhost:5432/"
        "hubspot_ai_history_test",
    )
    assert migration_database_url("postgresql://ignored/ignored") == (
        "postgresql+asyncpg://hubspot_history_test:local-pass@localhost:5432/"
        "hubspot_ai_history_test"
    )
    monkeypatch.setenv(
        "HISTORY_TEST_DATABASE_URL",
        "postgresql+asyncpg://app:app@localhost:5432/hubspot_ai",
    )
    with pytest.raises(RuntimeError):
        migration_database_url("postgresql://ignored/ignored")


def test_production_history_migration_requires_explicit_exact_target(monkeypatch):
    monkeypatch.delenv("HISTORY_TEST_MODE", raising=False)
    monkeypatch.delenv("HUBSPOT_PRODUCTION_MIGRATION_MODE", raising=False)
    production_url = "postgresql+asyncpg://app:secret@postgres:5432/hubspot_ai"
    with pytest.raises(RuntimeError, match="shared database"):
        migration_database_url(production_url)

    validate_production_history_migration_target(production_url, authorized=True)
    validate_production_migration_revision("7a91c4d2e650", "7a91c4d2e650")
    for requested, authorized_revision in (
        ("head", "7a91c4d2e650"),
        ("7a91c4d2e650", "head"),
        ("6aa7c8eb17c4", "6aa7c8eb17c4"),
    ):
        with pytest.raises(RuntimeError, match="limited to revision"):
            validate_production_migration_revision(requested, authorized_revision)
    monkeypatch.setenv("HUBSPOT_PRODUCTION_MIGRATION_MODE", "1")
    assert migration_database_url(production_url) == production_url

    for unsafe_url in (
        "postgresql+asyncpg://app:secret@localhost:5432/hubspot_ai",
        "postgresql+asyncpg://app:secret@postgres:5433/hubspot_ai",
        "postgresql+asyncpg://other:secret@postgres:5432/hubspot_ai",
        "postgresql+asyncpg://app:secret@postgres:5432/other_db",
        "sqlite+aiosqlite:///:memory:",
    ):
        with pytest.raises(RuntimeError):
            validate_production_history_migration_target(unsafe_url, authorized=True)

    monkeypatch.setenv("HISTORY_TEST_MODE", "1")
    with pytest.raises(RuntimeError, match="cannot be enabled together"):
        migration_database_url(production_url)


def test_history_renderer_orders_exchanges_and_preserves_latest_result_blocks():
    history = [
        SimpleNamespace(
            delivery_id=f"delivery-{index}",
            question=f"Question {index}",
            response_status="ok",
            response_text=f"Answer {index}",
            render_data={
                "result": (
                    {
                        "kind": "deals",
                        "table": {
                            "columns": [{"key": "dealname", "label": "Deal name"}],
                            "rows": [{"dealname": "Latest renewal", "view_url": "https://app.hubspot.com/deals/1"}],
                        },
                    }
                    if index == 4
                    else {}
                ),
                "cards": [],
            },
        )
        for index in range(5)
    ]

    blocks = build_home_view(history=history, recent=[]) ["blocks"]
    serialized = str(blocks)
    question_positions = [serialized.index(f"Question {index}") for index in range(5)]
    assert question_positions == sorted(question_positions)
    assert "Latest renewal" in serialized and "View in HubSpot" in serialized
    assert len(blocks) <= 100
    input_index = next(i for i, block in enumerate(blocks) if block["type"] == "input")
    activity_index = next(
        i for i, block in enumerate(blocks) if "Recent CRM Activity" in str(block)
    )
    assert input_index < activity_index
    assert any(
        action.get("action_id") == SUBMIT_ACTION
        for block in blocks if block["type"] == "actions"
        for action in block["elements"]
    )


def test_current_and_historical_answers_normalize_links_and_markdown_consistently():
    url = "https://app.hubspot.com/contacts/123/record/0-3/456"
    answer = (
        "Cloud Migration Project is owned by 98711688. "
        f"&lt;[{url}|Open]({url}) in HubSpot&gt;\n"
        r"\- \*Deal Name\* - \*Contract Sent\* - USD 500000"
    )
    response = AgentResponse(status="ok", text=answer, request_id="current")
    history = [
        SimpleNamespace(
            delivery_id="historical",
            question="Who owns the Cloud Migration Project deal?",
            response_status="ok",
            response_text=answer,
            render_data={},
        )
    ]

    current_text = "\n".join(
        block.get("text", {}).get("text", "")
        for block in render_response_blocks(response)
        if block.get("type") == "section"
    )
    home_blocks = build_home_view(
        history=history,
        response=AgentResponse(status="ok", text="A separate current answer.", request_id="new"),
        recent=[],
    )["blocks"]
    historical_text = "\n".join(
        block.get("text", {}).get("text", "")
        for block in home_blocks
        if block.get("type") == "section"
    )

    assert f"<{url}|Open in HubSpot>" in current_text
    assert f"<{url}|Open in HubSpot>" in historical_text
    assert current_text.count(url) == historical_text.count(url) == 1
    for rendered in (current_text, historical_text):
        assert "&lt;" not in rendered and "&gt;" not in rendered
        assert "&#x20;" not in rendered
        assert r"\*Deal Name\*" not in rendered
        assert "*Deal Name*" in rendered


def test_historical_response_text_is_not_truncated_and_uses_valid_section_chunks():
    answer = "Start " + ("x" * 7200) + " Finish"

    blocks = _compact_assistant_response(answer)
    chunks = [
        block["text"]["text"]
        for block in blocks
        if block.get("type") == "section"
    ]

    assert "".join(chunks) == answer
    assert all(len(chunk) <= 3000 for chunk in chunks)


def test_empty_history_shows_clean_state_and_keeps_existing_controls():
    blocks = build_home_view(history=[], recent=[]) ["blocks"]
    assert "Your questions and answers will appear here." in str(blocks)
    assert any(block["type"] == "input" for block in blocks)
    assert len(blocks) <= 100


def test_short_history_preview_stays_concise_without_full_response_button():
    exchange = SimpleNamespace(
        delivery_id="1712345678.0001",
        question="Who owns the deal?",
        response_status="ok",
        response_text="Maya Chen owns the Cloud Migration Project.",
        render_data={},
    )

    blocks = _history_preview_blocks(exchange)

    assert "Maya Chen owns the Cloud Migration Project." in str(blocks)
    assert not any(
        element.get("action_id") == VIEW_FULL_RESPONSE_ACTION
        for block in blocks
        for element in block.get("elements", [])
    )


@pytest.mark.parametrize(
    ("field", "label", "prefix"),
    [
        ("dealname", "Deal name", "Deal"),
        ("email", "Email", "person"),
        ("name", "Company", "Company"),
    ],
)
def test_listing_preview_shows_saved_count_two_records_and_opaque_button_value(
    field, label, prefix
):
    rows = [
        {
            field: f"{prefix} {i}",
            "domain": f"record{i}.example",
            "view_url": f"https://app.hubspot.com/records/1/{i}",
        }
        for i in range(1, 5)
    ]
    exchange = SimpleNamespace(
        delivery_id="delivery-123",
        question=f"List {prefix.lower()} records",
        response_status="ok",
        response_text="Four CRM records were found.\n" + "\n".join(row[field] for row in rows),
        render_data={"result": {"table": {"columns": [
            {"key": field, "label": label}, {"key": "domain", "label": "Domain"},
        ], "rows": rows}}},
    )

    blocks = _history_preview_blocks(exchange)
    rendered = str(blocks)
    button = next(
        element for block in blocks for element in block.get("elements", [])
        if element.get("action_id") == VIEW_FULL_RESPONSE_ACTION
    )

    assert "4 records in the saved result" in rendered
    assert f"{prefix} 1" in rendered and f"{prefix} 2" in rendered
    assert f"{prefix} 3" not in rendered and f"{prefix} 4" not in rendered
    assert "<https://app.hubspot.com/records/1/1|View in HubSpot>" in rendered
    assert button["value"] == "delivery-123:0"
    assert exchange.response_text not in button["value"]


def test_grouped_history_preview_uses_saved_group_metrics():
    exchange = SimpleNamespace(
        delivery_id="delivery-grouped",
        question="Group deals by stage",
        response_status="ok",
        response_text="Pipeline summary available.",
        render_data={"result": {
            "plan": {"group_by": "dealstage", "aggregations": ["count", "sum_amount"]},
            "table": {"columns": [
                {"key": "dealstage", "label": "Stage"},
                {"key": "count", "label": "Deals"},
                {"key": "sum_amount", "label": "Total amount"},
            ], "rows": [
                {"dealstage": "qualifiedtobuy", "count": 4, "sum_amount": 120000},
                {"dealstage": "closedwon", "count": 2, "sum_amount": 90000},
            ]},
        }},
    )

    rendered = str(_history_preview_blocks(exchange))

    assert "qualifiedtobuy" in rendered and "120,000" in rendered
    assert "closedwon" in rendered and "90,000" in rendered


def test_full_response_paginates_without_splitting_slack_links_or_urls():
    link = "<https://app.hubspot.com/contacts/1/record/0-1/42|Open in HubSpot>"
    text = ("CRM listing row. " * 150) + link + (" More rows." * 400)
    chunks = _split_history_response(text)
    exchange = SimpleNamespace(
        delivery_id="delivery-long",
        question="List records",
        response_text=text,
    )

    assert all(len(chunk) <= 3000 for chunk in chunks)
    assert "".join(chunks) == text
    rendered_pages = [
        _full_history_response_blocks(exchange, page)
        for page in range((len(chunks) + 1) // 2)
    ]
    assert len(rendered_pages) > 1
    assert any(link in str(page) for page in rendered_pages)
    for page in rendered_pages[:-1]:
        assert any(
            isinstance(element.get("text"), dict)
            and element["text"].get("text") == "Next"
            for block in page
            for element in block.get("elements", [])
        )


def test_full_response_action_parser_accepts_only_bounded_history_references():
    valid = parse_home_interaction({
        "type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"},
        "view": {"type": "home", "blocks": []},
        "actions": [{"action_id": VIEW_FULL_RESPONSE_ACTION, "value": "1712345678.0001:2"}],
    })
    invalid = parse_home_interaction({
        "type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"},
        "view": {"type": "home", "blocks": []},
        "actions": [{"action_id": VIEW_FULL_RESPONSE_ACTION, "value": "other-user-secret:999999"}],
    })

    assert valid is not None
    assert valid.kind == "view_full_response"
    assert valid.history_delivery_id == "1712345678.0001"
    assert valid.history_page == 2
    assert invalid is None


@pytest.mark.asyncio
async def test_owned_history_lookup_filters_tenant_workspace_user_and_delivery():
    class Result:
        def scalar_one_or_none(self):
            return None

    class Session:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return Result()

    session = Session()
    result = await SlackConversationRepository(session).get_owned(
        tenant_id="tenant-a",
        team_id="T1",
        user_id="U1",
        delivery_id="1712345678.0001",
    )

    sql = str(session.statement)
    values = set(session.statement.compile().params.values())
    assert result is None
    assert "slack_conversation_exchanges.tenant_id" in sql
    assert "slack_conversation_exchanges.slack_team_id" in sql
    assert "slack_conversation_exchanges.slack_user_id" in sql
    assert "slack_conversation_exchanges.delivery_id" in sql
    assert {"tenant-a", "T1", "U1", "1712345678.0001"} <= values


def test_old_pending_confirmation_never_renders_an_actionable_button():
    history = [
        SimpleNamespace(
            delivery_id="old-delivery",
            question="Create a deal",
            response_status="pending_confirmation",
            response_text="Please confirm.",
            render_data={
                "result": {
                    "kind": "pending_confirmation",
                    "action_type": "create_deal",
                },
                "cards": [],
            },
        )
    ]
    blocks = build_home_view(history=history, recent=[]) ["blocks"]
    assert "This confirmation is no longer active." in str(blocks)
    assert not any(block["type"] == "actions" and any(
        element.get("action_id") == "home_confirm" for element in block["elements"]
    ) for block in blocks)


def test_response_storage_keeps_renderer_data_but_drops_sensitive_controls_and_metadata():
    response = AgentResponse(
        status="ok",
        text="A deal was found.",
        request_id="private-request-id",
        tools_used=["private-tool"],
        result={
            "kind": "deals",
            "table": {"columns": [], "rows": []},
            "hubspot_url": "https://app.hubspot.com/deals/1",
            "access_token": "must-not-persist",
            "action_id": "must-not-persist",
        },
    )
    data = response_render_data(response)
    assert data["result"]["table"] == {"columns": [], "rows": []}
    assert "hubspot_url" in data["result"]
    assert "access_token" not in data["result"]
    assert "action_id" not in data["result"]
    assert "request_id" not in data and "tools_used" not in data
