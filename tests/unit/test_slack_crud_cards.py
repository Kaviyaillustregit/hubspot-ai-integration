"""Focused tests for consistent Slack Block Kit response cards for all HubSpot CRM mutations.

Tests all 9 combinations:
- Contact: Create, Update, Delete
- Company: Create, Update, Delete
- Deal: Create, Update, Delete
"""

import pytest

from app.agent.schemas import AgentResponse
from app.integrations.slack.home import render_response_blocks
from app.integrations.slack.response_formatting import format_slack_response, render_crud_card


@pytest.mark.parametrize(
    ("object_type", "expected_emoji", "expected_badge"),
    [
        ("contact", "👤", "Contact"),
        ("company", "🏢", "Company"),
        ("deal", "💼", "Deal"),
    ],
)
def test_crud_cards_create_renders_all_required_elements(object_type, expected_emoji, expected_badge):
    url = f"https://app.hubspot.com/contacts/123/record/0-1/{object_type}-1"
    response = AgentResponse(
        status="ok",
        text=f"{expected_badge} created successfully in HubSpot.",
        request_id=f"create-{object_type}",
        result={
            "kind": "crm_records",
            "title": f"{expected_badge} created",
            "operation": "create",
            "object_type": object_type,
            "name": f"New {expected_badge}",
            "email": "test@example.com" if object_type == "contact" else None,
            "domain": "example.com" if object_type == "company" else None,
            "amount": 50000 if object_type == "deal" else None,
            "closedate": "2026-12-15T00:00:00Z" if object_type == "deal" else None,
            "hubspot_url": url,
            "hubspot_owner_id": "98711688",
            "owner_name": "Sarah Connor",
        },
    )

    blocks = render_response_blocks(response)
    divider_blocks = [b for b in blocks if b.get("type") == "divider"]
    assert len(divider_blocks) >= 1, "Must contain a Slack divider above the record card"

    header_block = next(
        b for b in blocks
        if b.get("type") == "section" and b.get("text", {}).get("text", "").startswith(expected_emoji)
    )
    assert f"*<{url}|New {expected_badge}>* ({expected_badge})" in header_block["text"]["text"]
    assert "accessory" not in header_block

    field_block = next(b for b in blocks if b.get("type") == "section" and "fields" in b)
    accessory = field_block.get("accessory")
    assert accessory is not None, "CREATE card fields section must contain right-side accessory button"
    assert accessory["type"] == "button"
    assert accessory["text"]["text"] == "View in HubSpot ↗"
    assert accessory["url"] == url
    fields = field_block["fields"]
    assert len(fields) in {2, 4}, f"Fields must be balanced 2 or 4, got {len(fields)}"

    all_field_texts = " ".join(f["text"] for f in fields)
    assert "None" not in all_field_texts
    assert "98711688" not in all_field_texts, "Raw internal owner ID must not be displayed"
    if object_type == "deal":
        assert "$50,000" in all_field_texts, "Amount must be formatted with currency symbol"
        assert "2026-12-15" in all_field_texts, "Date must be formatted as YYYY-MM-DD"


@pytest.mark.parametrize(
    ("object_type", "expected_emoji", "expected_badge"),
    [
        ("contact", "👤", "Contact"),
        ("company", "🏢", "Company"),
        ("deal", "💼", "Deal"),
    ],
)
def test_crud_cards_update_shows_changes_and_view_button(object_type, expected_emoji, expected_badge):
    url = f"https://app.hubspot.com/contacts/123/record/0-1/{object_type}-2"
    response = AgentResponse(
        status="ok",
        text=f"✅ {expected_badge} Acme updated successfully in HubSpot.\n• Changes: Industry: AI, City: Boston",
        request_id=f"update-{object_type}",
        result={
            "kind": "crm_records",
            "title": f"{expected_badge} Updated",
            "operation": "update",
            "object_type": object_type,
            "name": f"Acme {expected_badge}",
            "changes": {"industry": "AI", "city": "Boston"},
            "hubspot_url": url,
        },
    )

    blocks = render_response_blocks(response)
    divider_blocks = [b for b in blocks if b.get("type") == "divider"]
    assert len(divider_blocks) >= 1

    header_block = next(
        b for b in blocks
        if b.get("type") == "section" and b.get("text", {}).get("text", "").startswith(expected_emoji)
    )
    assert f"*<{url}|Acme {expected_badge}>* ({expected_badge})" in header_block["text"]["text"]
    assert "accessory" not in header_block

    field_block = next(b for b in blocks if b.get("type") == "section" and "fields" in b)
    assert field_block.get("accessory", {}).get("text", {}).get("text") == "View in HubSpot ↗"
    fields = field_block["fields"]
    assert len(fields) in {2, 4}

    all_field_texts = " ".join(f["text"] for f in fields)
    assert "AI" in all_field_texts, "Changed property must be displayed"
    assert "Boston" in all_field_texts, "Changed property must be displayed"


@pytest.mark.parametrize(
    ("object_type", "expected_emoji", "expected_badge"),
    [
        ("contact", "👤", "Contact"),
        ("company", "🏢", "Company"),
        ("deal", "💼", "Deal"),
    ],
)
def test_crud_cards_delete_omits_view_button_and_dead_link(object_type, expected_emoji, expected_badge):
    url = f"https://app.hubspot.com/contacts/123/record/0-1/{object_type}-3"
    response = AgentResponse(
        status="ok",
        text=f"✅ {expected_badge} OldCorp archived in HubSpot.\n• {expected_badge} ID: `998877`",
        request_id=f"delete-{object_type}",
        result={
            "kind": "crm_records",
            "title": f"{expected_badge} Archived",
            "operation": "delete",
            "object_type": object_type,
            "name": f"OldCorp {expected_badge}",
            "hubspot_url": url,
        },
    )

    formatted = format_slack_response(response)
    blocks = formatted.messages[0]

    header_block = next(
        b for b in blocks
        if b.get("type") == "section" and b.get("text", {}).get("text", "").startswith(expected_emoji)
    )
    header_text = header_block["text"]["text"]
    assert f"{expected_emoji} *OldCorp {expected_badge}* ({expected_badge})" in header_text
    assert url not in header_text, "Deleted record header must not contain a broken link"

    assert "accessory" not in header_block, "DELETE card must NOT contain a View in HubSpot button"

    for b in blocks:
        if b.get("type") == "section":
            btn = b.get("accessory")
            if btn and btn.get("type") == "button":
                assert btn.get("text", {}).get("text") != "View in HubSpot ↗"

    field_block = next(b for b in blocks if b.get("type") == "section" and "fields" in b)
    fields = field_block["fields"]
    assert len(fields) in {2, 4}
    all_field_texts = " ".join(f["text"] for f in fields)
    assert "Archived" in all_field_texts or "Deleted" in all_field_texts
    assert "998877" not in all_field_texts, "Raw ID must not be displayed in fields"
    assert "None" not in all_field_texts


def test_shared_render_crud_card_direct_invocation():
    card = render_crud_card(
        "deal",
        "update",
        {
            "name": "Big Enterprise Deal",
            "hubspot_url": "https://app.hubspot.com/contacts/123/record/0-3/456",
            "changes": {"amount": "120000", "stage": "Closed Won"},
        },
    )
    assert len(card) == 3
    assert card[0]["type"] == "divider"
    assert "💼 *<https://app.hubspot.com/contacts/123/record/0-3/456|Big Enterprise Deal>* (Deal)" in card[1]["text"]["text"]
    assert "accessory" not in card[1]
    assert card[2]["accessory"]["text"]["text"] == "View in HubSpot ↗"
    field_texts = " ".join(f["text"] for f in card[2]["fields"])
    assert "$120,000" in field_texts
    assert "Closed Won" in field_texts
