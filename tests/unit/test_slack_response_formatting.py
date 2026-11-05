import asyncio
import json
import re

import pytest

from app.agent.schemas import AgentResponse
from app.api.slack import _respond
from app.integrations.slack.home import CONFIRM_ACTION, render_response_blocks


from app.integrations.slack.response_formatting import (
    escape_slack_text,
    format_property_value,
    format_slack_response,
    normalize_slack_display_text,
)


def _has_field_pair(blocks: list[dict[str, object]], left: str, right: str) -> bool:
    for block in blocks:
        if block.get("type") != "section":
            continue
        fields = block.get("fields")
        if not isinstance(fields, list):
            continue
        texts = [field.get("text") for field in fields if isinstance(field, dict)]
        for idx in range(len(texts) - 1):
            if texts[idx] == left and texts[idx + 1] == right:
                return True
    return False



def test_channel_response_uses_panel_blocks_and_original_fallback_text():
    text = "Found 1 companies. Showing 1 records."
    response = AgentResponse(
        status="ok",
        text=text,
        request_id="request-1",
        result={
            "kind": "crm_intelligence",
            "table": {
                "columns": [{"key": "name", "label": "Company name"}],
                "rows": [{"name": "Northwind"}],
            },
        },
    )
    sent = []

    class Agent:
        async def respond(self, request):
            return response

    class Client:
        async def post_message(self, channel, message, blocks=None):
            sent.append((channel, message, blocks))

    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "event-1",
        "event": {
            "type": "message",
            "user": "U1",
            "channel": "C1",
            "text": "List companies",
        },
    }


    class Authorization:
        async def is_authorized(self, *, tenant_id, slack_team_id, slack_user_id):
            return True
    asyncio.run(_respond(Agent(), Client(), Authorization(), payload, "tenant-1", "request-1"))

    assert len(sent) == 1
    channel, fallback, blocks = sent[0]
    assert channel == "C1"
    assert fallback == text
    assert "Northwind" in json.dumps(blocks, ensure_ascii=False)


def test_table_keeps_requested_fields_and_missing_values():
    response = AgentResponse(
        status="ok",
        text="Found one company.",
        request_id="request-2",
        result={
            "kind": "crm_intelligence",
            "table": {
                "columns": [
                    {"key": "name", "label": "Company name"},
                    {"key": "industry", "label": "Industry"},
                    {"key": "city", "label": "City"},
                    {"key": "hubspot_owner_id", "label": "Owner"},
                ],
                "rows": [
                    {
                        "name": "Northwind",
                        "industry": "Software",
                        "city": None,
                        "hubspot_owner_id": "Ada Lovelace",
                    }
                ],
            },
        },
    )

    formatted = format_slack_response(response)
    rendered = json.dumps(formatted.messages, ensure_ascii=False)
    assert "Company name" in rendered
    assert "Industry" in rendered and "Software" in rendered
    assert "City" in rendered and "Not available" in rendered
    assert "Owner" in rendered and "Ada Lovelace" in rendered


def test_display_formats_known_amount_and_iso_date_fields_without_guessing_currency():
    assert format_property_value("amount", "12500.50") == "12,500.5"
    assert format_property_value("closedate", "2026-10-19") == "Oct 19, 2026"
    assert (
        format_property_value("custom_text", "12500.50")
        == "12500.50"
    )


def test_chart_and_app_home_keep_existing_image_block():
    response = AgentResponse(
        status="ok",
        text="Deals by stage",
        request_id="request-3",
        result={
            "kind": "deal_analysis",
            "chart_url": "https://quickchart.io/chart?c=encoded",
            "chart_title": "Deals by stage",
        },
    )

    channel = format_slack_response(response)
    home = render_response_blocks(response)
    assert any(block.get("type") == "image" for block in channel.messages[0])
    assert any(block.get("type") == "image" for block in home)


def test_confirmation_button_and_error_response_are_formatted():
    confirmation = AgentResponse(
        status="pending_confirmation",
        text="Please confirm the requested update.",
        request_id="request-4",
        result={
            "kind": "pending_confirmation",
            "action_id": "a" * 32,
            "action_type": "update_contact",
            "record_label": "Ava Kumar",
        },
    )
    confirmation_blocks = render_response_blocks(confirmation)
    action = next(block for block in confirmation_blocks if block.get("type") == "actions")
    button = action["elements"][0]
    assert button["action_id"] == CONFIRM_ACTION
    assert button["value"] == "a" * 32

    error = AgentResponse(
        status="unavailable",
        text="HubSpot is temporarily unavailable.",
        request_id="request-5",
    )
    formatted_error = format_slack_response(error)
    assert formatted_error.fallback_text == error.text
    assert formatted_error.response_type == "error_or_clarification"
    assert formatted_error.messages[0]


def test_hubspot_links_are_validated_and_duplicate_links_removed():
    valid = "https://app.hubspot.com/contacts/123/record/0-2/456"
    response = AgentResponse(status="ok", text="Details", request_id="request-6")
    formatted = format_slack_response(
        response,
        renderer=lambda _: [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"<{valid}|View in HubSpot> "
                        f"<{valid}|Open in HubSpot> "
                        "<http://app.hubspot.com/bad|Unsafe HubSpot link>"
                    ),
                },
            }
        ],
    )

    rendered = formatted.messages[0][0]["text"]["text"]
    assert rendered.count(valid) == 1
    assert "http://app.hubspot.com" not in rendered
    assert "Unsafe HubSpot link" in rendered


def test_narrative_hubspot_links_remain_clickable_once_and_invalid_markup_is_removed():
    url = "https://app.hubspot.com/contacts/123/record/0-2/456"
    response = AgentResponse(
        status="ok",
        text=f"Open this record: <{url}|Open in HubSpot>",
        request_id="narrative-link",
        result={"hubspot_url": url},
    )
    invalid = AgentResponse(
        status="ok",
        text="Open this record: <http://app.hubspot.com/bad|Open in HubSpot>",
        request_id="invalid-narrative-link",
    )

    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)
    invalid_rendered = json.dumps(render_response_blocks(invalid), ensure_ascii=False)

    assert rendered.count(url) == 1
    assert f"<{url}|Open in HubSpot>" in rendered
    assert "&lt;https://app.hubspot.com" not in rendered
    assert "http://app.hubspot.com/bad" not in invalid_rendered
    assert "HubSpot link unavailable" in invalid_rendered


def test_html_escaped_hubspot_markup_is_normalized_before_escape_and_deduplicated():
    url = "https://app.hubspot.com/contacts/123/record/0-2/456"
    response = AgentResponse(
        status="ok",
        text=(
            f"Open &lt;{url}?source=reply|Open in HubSpot&gt; or "
            f"&lt;{url}?source=card|View record&gt;."
        ),
        request_id="escaped-hubspot-link",
        result={"hubspot_url": url},
    )

    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)

    assert rendered.count("https://app.hubspot.com/contacts/123/record/0-2/456") == 1
    assert "&lt;https://app.hubspot.com" not in rendered
    assert "&amp;lt;https" not in rendered
    assert "&gt;" not in rendered


def test_nested_markdown_hubspot_link_is_repaired_and_keeps_the_record_url():
    url = "https://app.hubspot.com/contacts/123/record/0-3/456"
    text = f"&lt;[{url}|Open]({url}) in HubSpot&gt;"

    rendered = escape_slack_text(text)

    assert rendered == f"<{url}|Open in HubSpot>"
    assert rendered.count(url) == 1
    assert "&lt;" not in rendered and "&gt;" not in rendered


def test_backslash_escaped_hubspot_url_and_safe_entities_are_normalized():
    url = "https://app.hubspot.com/contacts/123/record/0-3/456"
    rendered = escape_slack_text(
        r"&lt;https\://app.hubspot.com/contacts/123/record/0-3/456|Open in HubSpot&gt;"
        "&#x20;for&#32;details"
    )

    assert f"<{url}|Open in HubSpot> for details" == rendered
    assert "&#x20;" not in rendered and "&#32;" not in rendered


def test_only_well_formed_escaped_markdown_is_unescaped():
    rendered = escape_slack_text(
        r"\- \*Deal Name\* - \*Contract Sent\* - USD 500000"
    )

    assert rendered == "- *Deal Name* - *Contract Sent* - USD 500000"
    assert normalize_slack_display_text(r"Keep C:\Users\A and \q literally") == (
        "Keep C:\\Users\\A and \\q literally"
    )
    assert normalize_slack_display_text("Sales Pipeline\\") == "Sales Pipeline\\"


def test_code_examples_preserve_literal_backslashes_entities_and_link_text():
    code = '```python\nprint(r"C:\\Users\\A &lt;value&gt;")\n```'

    rendered = escape_slack_text(code)

    assert r"C:\Users\A" in rendered
    assert "&amp;lt;value&amp;gt;" in rendered
    assert "<value>" not in rendered


def test_distinct_hubspot_records_remain_linked_and_same_record_links_deduplicate():
    first = "https://app.hubspot.com/contacts/123/record/0-1/456"
    second = "https://app.hubspot.com/contacts/123/record/0-1/789"
    rendered = escape_slack_text(
        f"<{first}?source=detail|First> <{first}?source=history|Again> <{second}|Second>"
    )

    assert rendered.count("<https://app.hubspot.com") == 2
    assert first in rendered and second in rendered
    assert "Again" not in rendered


def test_app_home_table_keeps_record_link_clickable_once():
    url = "https://app.hubspot.com/contacts/123/record/0-2/456"
    response = AgentResponse(
        status="ok",
        text="One company found.",
        request_id="request-9",
        result={
            "kind": "crm_intelligence",
            "table": {
                "columns": [
                    {"key": "name", "label": "Company name"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [{"name": "Northwind", "view_url": url}],
            },
        },
    )

    blocks = render_response_blocks(response)
    rendered = json.dumps(blocks, ensure_ascii=False)
    assert f"<{url}|View in HubSpot>" in rendered
    assert rendered.count(url) == 1


def test_deal_listing_keeps_distinct_record_links_and_removes_narrative_duplicates():
    first = "https://app.hubspot.com/contacts/123/record/0-3/456"
    second = "https://app.hubspot.com/contacts/123/record/0-3/789"
    response = AgentResponse(
        status="ok",
        text=(
            f"Renewal A: <{first}|Open in HubSpot>. "
            f"Renewal B: <{second}|Open in HubSpot>."
        ),
        request_id="deal-list-links",
        result={
            "kind": "crm_intelligence",
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {"dealname": "Renewal A", "view_url": first},
                    {"dealname": "Renewal B", "view_url": second},
                ],
            },
        },
    )

    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)

    assert rendered.count(first) == 1
    assert rendered.count(second) == 1
    assert f"<{first}|View in HubSpot>" in rendered
    assert f"<{second}|View in HubSpot>" in rendered


@pytest.mark.parametrize(
    ("title", "kind", "header"),
    [
        ("Contact Details", "contact", "Contact details"),
        ("Company Details", "company", "Company details"),
    ],
)
def test_individual_record_details_render_as_compact_single_column_cards(title, kind, header):
    url = "https://app.hubspot.com/contacts/123/record/0-3/456"
    response = AgentResponse(
        status="ok",
        text=f"Cloud Migration Project is owned by 98711688. <{url}|Open in HubSpot>",
        request_id=f"{kind}-details",
        result={
            "kind": "crm_records",
            "title": title,
            "table": {
                "columns": [
                    {"key": "name", "label": "Name"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "stage", "label": "Stage"},
                    {"key": "amount", "label": "Amount"},
                    {"key": "close_date", "label": "Close Date"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "name": "Cloud Migration Project",
                        "owner": "98711688",
                        "stage": "Qualified To Buy",
                        "amount": "250000",
                        "close_date": "2026-12-10",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks, ensure_ascii=False)
    detail_sections = [
        block for block in blocks
        if block.get("type") == "section" and ("text" in block or "fields" in block)
    ]
    channel = format_slack_response(response)
    channel_encoded = json.dumps(channel.messages, ensure_ascii=False)

    assert any(
        block.get("type") == "header" and header in block["text"]["text"]
        for block in blocks
    )
    assert detail_sections
    if kind == "contact":
        assert "*\U0001F464 Name:* Cloud Migration Project" in encoded
        assert any(x in encoded for x in ("*\U0001F454 Owner:* Owner not available", "*\U0001F454 Owner:* Unassigned"))
        assert "*\U0001F4B0 Amount:* *250,000*" in encoded
        assert "*\U0001F4C5 Close Date:* Dec 10, 2026" in encoded
    else:  # company
        assert "*\U0001F3E2 Name:* Cloud Migration Project" in encoded
        assert any(x in encoded for x in ("*\U0001F464 Owner:* Owner not available", "*\U0001F464 Owner:* Unassigned"))
        assert "*\U0001F4B0 Amount:* *250,000*" in encoded
        assert "*\U0001F4C5 Close Date:* Dec 10, 2026" in encoded
    assert "98711688" not in encoded
    assert "Ada" not in encoded
    assert "Dec 10, 2026" in encoded
    assert f"<{url}|View in HubSpot>" in encoded
    assert encoded.count(url) == 1
    assert "&lt;https://app.hubspot.com" not in encoded
    assert channel.fallback_text == response.text
    assert channel_encoded.count(url) == 1
    assert any(block.get("type") == "header" for page in channel.messages for block in page)


def test_single_deal_detail_renders_as_crm_record_card():
    """Verify single deal detail responses route through _render_crm_record_card."""
    url = "https://app.hubspot.com/contacts/123/record/0-3/456"
    response = AgentResponse(
        status="ok",
        text=f"Cloud Migration Project is owned by 98711688. <{url}|Open in HubSpot>",
        request_id="deal-details-check",
        result={
            "kind": "crm_records",
            "title": "Deal Details",
            "table": {
                "columns": [
                    {"key": "name", "label": "Name"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "stage", "label": "Stage Name"},
                    {"key": "amount", "label": "Amount"},
                    {"key": "close_date", "label": "Close Date"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "name": "Cloud Migration Project",
                        "owner": "98711688",
                        "stage": "Qualified To Buy",
                        "amount": "250000",
                        "close_date": "2026-12-10",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    divider_indices = [i for i, b in enumerate(blocks) if b.get("type") == "divider"]
    assert len(divider_indices) == 1

    header_block = blocks[divider_indices[0] + 1]
    header_text = header_block["text"]["text"]
    assert "💼" in header_text
    assert f"<{url}|Cloud Migration Project>" in header_text
    assert "`Deal`" in header_text
    assert "*💰 Amount:* $250,000" in header_text
    assert header_block["accessory"]["text"]["text"] == "View in HubSpot ↗"
    assert header_block["accessory"]["url"] == url

    fields_block = blocks[divider_indices[0] + 2]
    fields = fields_block["fields"]
    assert len(fields) == 4
    assert fields[0]["text"] == "*🏢 Company:* Not available"
    assert fields[1]["text"] == "*🟡 Stage:* Qualified To Buy"
    assert fields[2]["text"] == "*📅 Close Date:* 2026-12-10"
    assert fields[3]["text"] in ("*👤 Owner:* Owner not available", "*👤 Owner:* Unassigned")


def test_operations_deal_list_renders_as_crm_record_cards():
    """Verify deal list with kind='crm_records' and title='Open Deals' renders as Deal Cards."""
    url1 = "https://app.hubspot.com/contacts/123/record/0-3/111"
    url2 = "https://app.hubspot.com/contacts/123/record/0-3/222"
    response = AgentResponse(
        status="ok",
        text="Found 2 open deals.",
        request_id="open-deals-list",
        result={
            "kind": "crm_records",
            "title": "Open Deals",
            "message": "Found 2 open deals.",
            "table": {
                "columns": [
                    {"key": "name", "label": "Name"},
                    {"key": "company", "label": "Account/Company"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "stage", "label": "Stage Name"},
                    {"key": "amount", "label": "Amount"},
                    {"key": "probability", "label": "Probability"},
                    {"key": "close_date", "label": "Close Date"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "name": "Acme Renewal",
                        "company": "Acme Corp",
                        "owner": "Sarah Wilson",
                        "stage": "Qualified To Buy",
                        "amount": "$100,000",
                        "probability": "80%",
                        "close_date": "2026-11-20",
                        "view_url": url1,
                    },
                    {
                        "name": "Beta Expansion",
                        "company": "",
                        "owner": "",
                        "stage": "Prospecting",
                        "amount": "$50,000",
                        "probability": "20%",
                        "close_date": "2026-12-01",
                        "view_url": url2,
                    },
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    divider_indices = [i for i, b in enumerate(blocks) if b.get("type") == "divider"]
    assert len(divider_indices) == 2

    # Card 1
    h1 = blocks[divider_indices[0] + 1]
    assert "💼" in h1["text"]["text"]
    assert f"<{url1}|Acme Renewal>" in h1["text"]["text"]
    assert "`Deal`" in h1["text"]["text"]
    assert "*💰 Amount:* $100,000" in h1["text"]["text"]
    assert h1["accessory"]["text"]["text"] == "View in HubSpot ↗"

    f1 = blocks[divider_indices[0] + 2]["fields"]
    assert len(f1) == 4
    assert f1[0]["text"] == "*🏢 Company:* Acme Corp"
    assert f1[1]["text"] == "*🟡 Stage:* Qualified To Buy"
    assert f1[2]["text"] == "*📅 Close Date:* 2026-11-20"
    assert f1[3]["text"] == "*👤 Owner:* Sarah Wilson"

    # Card 2
    h2 = blocks[divider_indices[1] + 1]
    assert f"<{url2}|Beta Expansion>" in h2["text"]["text"]
    assert "*💰 Amount:* $50,000" in h2["text"]["text"]

    f2 = blocks[divider_indices[1] + 2]["fields"]
    assert len(f2) == 4
    assert f2[0]["text"] == "*🏢 Company:* Not available"
    assert f2[1]["text"] == "*⚪ Stage:* Prospecting"
    assert f2[2]["text"] == "*📅 Close Date:* 2026-12-01"
    assert f2[3]["text"] == "*👤 Owner:* Unassigned"


def test_contact_record_details_render_as_compact_two_column_pairs():
    url = "https://app.hubspot.com/contacts/123/record/0-1/999"
    response = AgentResponse(
        status="ok",
        text=f"Contact details. <{url}|Open in HubSpot>",
        request_id="contact-details-two-column",
        result={
            "kind": "crm_records",
            "title": "Contact Details",
            "table": {
                "columns": [
                    {"key": "name", "label": "Name"},
                    {"key": "email", "label": "Email"},
                    {"key": "phone", "label": "Phone"},
                    {"key": "company", "label": "Company"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "name": "A. Example",
                        "email": "a@example.com",
                        "phone": "+1 555 0100",
                        "company": "Northwind",
                        "owner": "123456",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks, ensure_ascii=False)

    assert _has_field_pair(
        blocks,
        "*\U0001F464 Name:* A. Example",
        "*\U0001F4E7 Email:* a@example.com",
    )
    assert _has_field_pair(
        blocks,
        "*\U0001F4DE Phone:* +1 555 0100",
        "*\U0001F3E2 Company:* Northwind",
    )
    assert any(x in encoded for x in ("*\U0001F454 Owner:* Owner not available", "*\U0001F454 Owner:* Unassigned"))
    assert "123456" not in encoded
    assert f"<{url}|View in HubSpot>" in encoded
    assert encoded.count(url) == 1


def test_company_record_details_render_as_compact_two_column_pairs():
    url = "https://app.hubspot.com/contacts/123/record/0-2/321"
    response = AgentResponse(
        status="ok",
        text=f"Company details. <{url}|Open in HubSpot>",
        request_id="company-details-two-column",
        result={
            "kind": "crm_records",
            "title": "Company Details",
            "table": {
                "columns": [
                    {"key": "name", "label": "Company name"},
                    {"key": "domain", "label": "Domain"},
                    {"key": "industry", "label": "Industry"},
                    {"key": "city", "label": "City"},
                    {"key": "employees", "label": "Employees"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "name": "Northwind",
                        "domain": "northwind.example",
                        "industry": "Software",
                        "city": "Seattle",
                        "employees": "120",
                        "owner": "654321",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks, ensure_ascii=False)

    assert _has_field_pair(
        blocks,
        "*\U0001F3E2 Company name:* Northwind",
        "*\U0001F310 Domain:* northwind.example",
    )
    assert _has_field_pair(
        blocks,
        "*\U0001F3ED Industry:* Software",
        "*\U0001F4CD City:* Seattle",
    )
    assert _has_field_pair(
        blocks,
        "*\U0001F465 Employees:* 120",
        "*\U0001F464 Owner:* Owner not available",
    ) or _has_field_pair(
        blocks,
        "*\U0001F465 Employees:* 120",
        "*\U0001F464 Owner:* Unassigned",
    )
    assert "654321" not in encoded
    assert f"<{url}|View in HubSpot>" in encoded
    assert encoded.count(url) == 1


def test_listing_rows_use_readable_sections_and_preserve_all_requested_columns():
    columns = [{"key": f"property_{index}", "label": f"Property {index}"} for index in range(12)]
    row = {f"property_{index}": f"value_{index}" for index in range(12)}
    response = AgentResponse(
        status="ok",
        text="Found one company.",
        request_id="listing-fields",
        result={
            "kind": "crm_intelligence",
            "table": {"columns": columns, "rows": [row, row]},
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks)

    assert any(block.get("type") == "section" for block in blocks)
    assert sum(block.get("type") == "divider" for block in blocks) == 1
    assert all(f"value_{index}" in encoded for index in range(12))


def test_grouped_analytics_render_structured_metrics_without_recalculating():
    response = AgentResponse(
        status="ok",
        text="Deals grouped by stage: 2 groups.",
        request_id="grouped-metrics",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "deal", "group_by": "dealstage", "aggregations": []},
            "chart_url": "https://quickchart.io/chart?c=grouped",
            "chart_title": "Deals by stage",
            "summary": {"matched_records": 5},
            "rows": [
                {"group": "Qualified To Buy", "count": 3, "sum_amount": "125000"},
                {"group": "Closed Won", "count": 2, "sum_amount": "80000"},
            ],
        },
    )

    formatted = format_slack_response(response, surface="home")
    blocks = formatted.messages[0]
    section_text = "\n".join(
        block["text"]["text"]
        for block in blocks
        if block.get("type") == "section"
    )

    assert formatted.response_type == "chart"
    assert "*\U0001F7E1 Qualified To Buy*" in section_text
    assert "*\U0001F7E1 Qualified To Buy*: *3* deals" in section_text
    assert "*Total amount:* *125,000*" in section_text
    assert not any("fields" in block for block in blocks)
    assert sum(block.get("type") == "divider" for block in blocks) == 1
    assert any(block.get("type") == "image" for block in blocks)


def test_grouped_summary_removes_only_duplicate_group_and_metric_aliases():
    response = AgentResponse(
        status="ok",
        text="Stage totals.",
        request_id="grouped-aliases",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "deal", "group_by": "dealstage"},
            "rows": [
                {
                    "group": "Qualified",
                    "label": "Qualified",
                    "count": 3,
                    "sum_amount": "125000",
                    "sum_amount_value": "125000",
                    "max_amount": "125000",
                }
            ],
        },
    )

    blocks = render_response_blocks(response)
    rendered = "\n".join(
        block["text"]["text"] for block in blocks if block.get("type") == "section"
    )

    assert rendered.count("Qualified") == 1
    assert rendered.count("*Total amount:* *125,000*") == 1
    assert "*Maximum amount:* *125,000*" in rendered


def test_grouped_summary_deduplicates_legacy_total_amount_alias():
    response = AgentResponse(
        status="ok",
        text="Deals by pipeline.",
        request_id="grouped-total-alias",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "deal", "group_by": "pipeline"},
            "rows": [{
                "label": "Sales Pipeline", "count": 26,
                "sum_amount": "819000", "total_amount": "819000",
            }],
        },
    )

    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)
    assert rendered.count("Total amount:") == 1




@pytest.mark.parametrize(
    ("stage", "expected_dot"),
    [
        ("Appointment Scheduled", "\U0001F7E1"),
        ("Qualified To Buy", "\U0001F7E1"),
        ("Presentation Scheduled", "\U0001F535"),
        ("Decision Maker Bought-In", "\U0001F535"),
        ("Contract Sent", "\U0001F7E1"),
        ("Closed Won", "\U0001F7E2"),
        ("Closed Lost", "\U0001F534"),
        ("Prospecting", "\u26AA"),
        ("Qualification", "\U0001F535"),
        ("Needs Analysis", "\U0001F7E1"),
        ("Value Proposition", "\U0001F7E3"),
        ("Id. Decision Makers", "\U0001F7E4"),
        ("Proposal/Price Quote", "\U0001F7E0"),
        ("Negotiation/Review", "\U0001F7E0"),
    ],
)
def test_deal_stage_indicator_mapping_is_explicit_and_conservative(stage, expected_dot):
    response = AgentResponse(
        status="ok",
        text="Found one deal.",
        request_id=f"stage-dot-{stage}",
        result={
            "kind": "crm_intelligence",
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "stage", "label": "Deal stage"},
                ],
                "rows": [{"dealname": "Deal 1", "stage": stage}],
            },
        },
    )

    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)
    assert f"*{expected_dot} Deal stage:* {stage}" in rendered


def test_deal_stage_indicator_is_case_insensitive_without_partial_matching():
    response = AgentResponse(
        status="ok",
        text="Found one deal.",
        request_id="stage-dot-case",
        result={
            "kind": "crm_intelligence",
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "stage", "label": "Deal stage"},
                ],
                "rows": [{"dealname": "Deal 1", "stage": "cLoSeD wOn"}],
            },
        },
    )

    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)
    assert "*\U0001F7E2 Deal stage:* cLoSeD wOn" in rendered

    injection = AgentResponse(
        status="ok",
        text="Found one deal.",
        request_id="stage-dot-injection",
        result={
            "kind": "crm_intelligence",
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "stage", "label": "Deal stage"},
                ],
                "rows": [{"dealname": "Deal 1", "stage": "Closed Won*"}],
            },
        },
    )

    injected = json.dumps(render_response_blocks(injection), ensure_ascii=False)
    assert "\U0001F7E2 Closed Won" not in injected


def test_deal_stage_indicator_does_not_apply_to_non_deal_records_or_grouped_values():
    contact = AgentResponse(
        status="ok",
        text="Found one contact.",
        request_id="stage-dot-contact",
        result={
            "kind": "crm_intelligence",
            "table": {
                "columns": [
                    {"key": "name", "label": "Contact name"},
                    {"key": "stage", "label": "Lifecycle stage"},
                ],
                "rows": [{"name": "Ava", "stage": "Closed Won"}],
            },
        },
    )
    rendered = json.dumps(render_response_blocks(contact), ensure_ascii=False)
    assert "\U0001F7E2 Closed Won" not in rendered

    grouped = AgentResponse(
        status="ok",
        text="Deals grouped by stage.",
        request_id="stage-dot-grouped",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "deal", "group_by": "dealstage"},
            "rows": [{"group": "Closed Won", "count": 1}],
        },
    )
    grouped_rendered = json.dumps(render_response_blocks(grouped), ensure_ascii=False)
    assert "*\U0001F7E2 Closed Won*" in grouped_rendered
    assert "\U0001F534 Closed Won" not in grouped_rendered


def test_grouped_deal_stage_summary_only_decorates_exact_known_stages():
    response = AgentResponse(
        status="ok",
        text="Deals grouped by stage.",
        request_id="grouped-stage-exact",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "deal", "group_by": "dealstage"},
            "rows": [{"group": "Qualified", "count": 2}],
        },
    )

    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)
    assert "*Qualified*" in rendered
    assert "\U0001F7E1 Qualified" not in rendered
    assert "\U0001F535 Qualified" not in rendered


def test_deal_stage_indicator_is_not_duplicated_when_already_decorated():
    response = AgentResponse(
        status="ok",
        text="Found one deal.",
        request_id="stage-dot-dup",
        result={
            "kind": "crm_intelligence",
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "stage", "label": "Deal stage"},
                ],
                "rows": [{"dealname": "Deal 1", "stage": "\U0001F535 Qualified To Buy"}],
            },
        },
    )

    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)
    assert "*\U0001F535 Deal stage:* Qualified To Buy" in rendered
    assert "\U0001F535 \U0001F535" not in rendered


def test_amount_emphasis_does_not_double_bold_existing_bold_values():
    response = AgentResponse(
        status="ok",
        text="Found one deal.",
        request_id="amount-double-bold",
        result={
            "kind": "crm_intelligence",
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "amount", "label": "Amount"},
                ],
                "rows": [{"dealname": "Deal 1", "amount": "*250000*"}],
            },
        },
    )

    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)
    assert "*\U0001F4B0 Amount:* *250000*" in rendered
    assert "**250000**" not in rendered

def test_grouped_details_table_renders_requested_fields_and_record_links():
    url = "https://app.hubspot.com/contacts/42/record/0-3/17"
    response = AgentResponse(
        status="ok", text="Showing 1 of 1 deal records.", request_id="grouped-detail",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "deal", "group_by": "hubspot_owner_id"},
            "rows": [{"label": "Ada Lovelace", "count": 1}],
            "details_table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "amount", "label": "Amount"},
                    {"key": "stage", "label": "Deal stage"},
                    {"key": "closedate", "label": "Closing date"},
                    {"key": "hubspot_owner_id", "label": "Owner"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [{
                    "dealname": "Renewal", "amount": "12500", "stage": "Qualified",
                    "closedate": "2026-10-19", "hubspot_owner_id": "Ada Lovelace",
                    "view_url": url,
                }],
            },
        },
    )

    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)
    for expected in ("Renewal", "12,500", "Qualified", "Oct 19, 2026", "Ada Lovelace", url):
        assert expected in rendered


def test_grouped_detail_pagination_notice_points_to_next_page():
    response = AgentResponse(
        status="ok", text="Deals by owner.", request_id="grouped-detail-page",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "deal", "group_by": "hubspot_owner_id"},
            "summary": {
                "details_shown": 20, "details_total": 26, "details_limit": 20,
                "details_offset": 0, "details_has_more": True,
            },
            "rows": [{"label": "Ada", "count": 26}],
            "details_table": {"columns": [{"key": "dealname", "label": "Deal name"}],
                              "rows": [{"dealname": "Deal 1"}]},
        },
    )

    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)
    assert "Showing 20 of 26 deal records. Ask for page 2" in rendered


def test_unassigned_owner_is_not_reported_as_an_unresolved_property():
    response = AgentResponse(
        status="ok", text="Found one deal.", request_id="unassigned-owner",
        result={
            "kind": "crm_intelligence",
            "table": {
                "columns": [{"key": "hubspot_owner_id", "label": "Owner"}],
                "rows": [{"hubspot_owner_id": ""}],
            },
        },
    )
    rendered = json.dumps(render_response_blocks(response), ensure_ascii=False)
    assert "*Owner:* Unassigned" in rendered


@pytest.mark.parametrize(
    ("group_by", "expected"), [("pipeline", "Pipeline"), ("dealstage", "Pipeline stage")]
)
def test_funnel_chart_category_label_matches_grouping_dimension(group_by, expected):
    from app.agent.crm_intelligence import CRMQueryPlan, build_chart_payload

    chart = build_chart_payload(
        CRMQueryPlan(object="deal", group_by=group_by, chart_type="funnel"),
        [{"label": "Sales", "count": 3}],
    )["chart"]
    assert chart["options"]["scales"]["y"]["title"]["text"] == expected
    assert chart["options"]["plugins"]["legend"]["display"] is False


def test_empty_results_and_general_conversation_stay_clear_and_compact():
    empty = AgentResponse(
        status="ok",
        text="No companies matched your request.",
        request_id="empty-results",
        result={
            "kind": "crm_records",
            "title": "Company Details",
            "table": {"columns": [{"key": "name", "label": "Name"}], "rows": []},
        },
    )
    conversation = AgentResponse(
        status="ok", text="Good morning!", request_id="conversation"
    )

    empty_blocks = render_response_blocks(empty)
    conversation_blocks = render_response_blocks(conversation)

    assert any("No records to display." in json.dumps(block) for block in empty_blocks)
    assert not any(block.get("type") == "fields" for block in conversation_blocks)
    assert len(conversation_blocks) <= 3


def test_channel_pages_preserve_all_blocks_within_slack_limit():
    response = AgentResponse(status="ok", text="Full fallback", request_id="request-7")
    formatted = format_slack_response(
        response,
        renderer=lambda _: [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"row {i}"}}
            for i in range(107)
        ],
    )

    assert formatted.fallback_text == "Full fallback"
    assert len(formatted.messages) == 3
    assert all(len(page) <= 50 for page in formatted.messages)
    assert sum(len(page) for page in formatted.messages) == 107
    assert formatted.messages[-1][-1]["text"]["text"] == "row 106"


def test_section_fields_obey_limits_and_long_values_are_preserved():
    response = AgentResponse(status="ok", text="Fallback", request_id="field-limits")
    long_value = "x" * 6200
    fields = [
        {"type": "mrkdwn", "text": f"*Field {index}*\nvalue {index}"}
        for index in range(11)
    ]
    fields.append({"type": "mrkdwn", "text": f"*Long field*\n{long_value}"})

    formatted = format_slack_response(
        response,
        renderer=lambda _: [{"type": "section", "fields": fields}],
    )
    blocks = formatted.messages[0]
    field_blocks = [
        block for block in blocks if block.get("type") == "section" and "fields" in block
    ]
    long_chunks: list[str] = []
    for block in blocks:
        if block.get("type") != "section":
            continue
        if isinstance(block.get("text"), dict):
            long_chunks.append(block["text"]["text"])
        if isinstance(block.get("fields"), list):
            long_chunks.extend(
                field.get("text")
                for field in block["fields"]
                if isinstance(field, dict) and isinstance(field.get("text"), str)
            )

    assert [len(block["fields"]) for block in field_blocks] == [10, 1]
    assert all(
        len(field["text"]) <= 2000 for block in field_blocks for field in block["fields"]
    )
    reconstructed = "".join(
        chunk for chunk in long_chunks if "Long field" in chunk or chunk.startswith("x")
    )
    assert reconstructed == f"*Long field*\n{long_value}"


def test_oversized_sections_are_split_without_losing_text():
    response = AgentResponse(status="ok", text="fallback", request_id="request-8")
    body = "x" * 6200
    formatted = format_slack_response(
        response,
        renderer=lambda _: [
            {"type": "section", "text": {"type": "mrkdwn", "text": body}}
        ],
    )

    chunks = [block["text"]["text"] for block in formatted.messages[0]]
    assert all(len(chunk) <= 3000 for chunk in chunks)
    assert "".join(chunks) == body


def test_grouped_dealstage_summary_renders_stage_emojis_regression():
    """Regression: _grouped_result_table NameError for `plan` prevented
    grouped dealstage summaries from rendering their stage emojis at all."""
    import json
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    response = AgentResponse(
        status="ok",
        text="Deals grouped by stage.",
        request_id="regression-grouped-stage",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "deal", "group_by": "dealstage"},
            "rows": [
                {"group": "Closed Won", "count": 2, "sum_amount": "80000"},
                {"group": "Qualified To Buy", "count": 3, "sum_amount": "125000"},
            ],
        },
    )

    blocks = render_response_blocks(response)
    rendered = json.dumps(blocks, ensure_ascii=False)

    # Grouped dealstage summaries must show the stage emoji as the row heading.
    assert "*\U0001F7E2 Closed Won*" in rendered  # green dot for Closed Won
    assert "*\U0001F7E1 Qualified To Buy*" in rendered  # yellow dot for Qualified To Buy
    # No stage should appear without its decorator when it is a known stage.
    assert "* Closed Won*" not in rendered.replace("*\U0001F7E2 Closed Won*", "")


def test_contact_and_company_field_labels_have_emoji_prefixes_regression():
    """Regression: contact and company field labels must carry their emoji
    prefixes (e.g. \U0001F464 for name on a contact record)."""
    import json
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    contact_response = AgentResponse(
        status="ok",
        text="Contact details.",
        request_id="regression-contact-emoji",
        result={
            "kind": "crm_records",
            "title": "Contact Details",
            "table": {
                "columns": [
                    {"key": "name", "label": "Name"},
                    {"key": "email", "label": "Email"},
                    {"key": "phone", "label": "Phone"},
                ],
                "rows": [{"name": "Ava Kumar", "email": "ava@example.com", "phone": "+1 555 0100"}],
            },
        },
    )

    contact_blocks = render_response_blocks(contact_response)
    contact_text = json.dumps(contact_blocks, ensure_ascii=False)
    # Name field on a contact card must carry the person emoji.
    assert "*\U0001F464 Name:* Ava Kumar" in contact_text
    # Email field on a contact card must carry the envelope emoji.
    assert "*\U0001F4E7 Email:* ava@example.com" in contact_text
    # Phone field on a contact card must carry the phone emoji.
    assert "*\U0001F4DE Phone:* +1 555 0100" in contact_text

    company_response = AgentResponse(
        status="ok",
        text="Company details.",
        request_id="regression-company-emoji",
        result={
            "kind": "crm_records",
            "title": "Company Details",
            "table": {
                "columns": [
                    {"key": "name", "label": "Name"},
                    {"key": "domain", "label": "Domain"},
                    {"key": "industry", "label": "Industry"},
                    {"key": "city", "label": "City"},
                ],
                "rows": [
                    {
                        "name": "Northwind",
                        "domain": "northwind.com",
                        "industry": "Software",
                        "city": "Seattle",
                    }
                ],
            },
        },
    )

    company_blocks = render_response_blocks(company_response)
    company_text = json.dumps(company_blocks, ensure_ascii=False)
    # Company name field must carry the office building emoji.
    assert "*\U0001F3E2 Name:* Northwind" in company_text
    # Domain field must carry the globe emoji.
    assert "*\U0001F310 Domain:* northwind.com" in company_text
    # Industry field must carry the factory emoji.
    assert "*\U0001F3ED Industry:* Software" in company_text
    # City field must carry the pin emoji.
    assert "*\U0001F4CD City:* Seattle" in company_text


def test_slack_edge_emoji_image_links_do_not_leak_into_deal_stage_text_regression():
    """Regression: leaked slack-edge emoji links such as [:large_yellow_circle:](url)
    must render as native unicode emojis and not leak image URLs."""
    import json
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    leaked_stage = "[:large_yellow_circle:](https://a.slack-edge.com/production-standard-emoji-assets/14.0/google-large/1f7e1.png) Qualified To Buy"
    response = AgentResponse(
        status="ok",
        text=f"Deal stage: {leaked_stage}",
        request_id="regression-slack-edge-link",
        result={
            "kind": "crm_records",
            "title": "Deal Details",
            "table": {
                "columns": [
                    {"key": "name", "label": "Deal name"},
                    {"key": "stage", "label": "Deal stage"},
                    {"key": "amount", "label": "Amount"},
                ],
                "rows": [
                    {
                        "name": "Acme expansion",
                        "stage": leaked_stage,
                        "amount": "850000",
                        "view_url": "https://app.hubspot.com/deals/123",
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    rendered = json.dumps(blocks, ensure_ascii=False)
    assert "slack-edge.com" not in rendered
    assert "\U0001F7E1" in rendered
    assert "*\U0001F7E1 Deal stage:* Qualified To Buy" in rendered
    assert "Acme expansion" in rendered
    assert "*💰 Amount:* $850,000" in rendered
    assert "View in HubSpot ↗" in rendered


def test_grouped_deal_stage_summary_all_expected_colors_regression():
    """Regression: all 7 standard pipeline stages must render with their exact expected emoji dot:
    🟡 Appointment Scheduled
    🟡 Qualified To Buy
    🔵 Presentation Scheduled
    🔵 Decision Maker Bought-In
    🟡 Contract Sent
    🟢 Closed Won
    🔴 Closed Lost
    """
    import json
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    rows = [
        {"group": "Appointment Scheduled", "count": 5, "sum_amount": "819000"},
        {"group": "Qualified To Buy", "count": 4, "sum_amount": "1707000"},
        {"group": "Presentation Scheduled", "count": 3, "sum_amount": "146000"},
        {"group": "Decision Maker Bought-In", "count": 3, "sum_amount": "430000"},
        {"group": "Contract Sent", "count": 4, "sum_amount": "1300000"},
        {"group": "Closed Won", "count": 4, "sum_amount": "477000"},
        {"group": "Closed Lost", "count": 4, "sum_amount": "980000"},
    ]
    response = AgentResponse(
        status="ok",
        text="Pipeline breakdown",
        request_id="regression-all-stages",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "deal", "group_by": "dealstage"},
            "rows": rows,
        },
    )

    blocks = render_response_blocks(response)
    rendered = json.dumps(blocks, ensure_ascii=False)

    assert "*\U0001F7E1 Appointment Scheduled*: *5* deals | *Total amount:* *819,000*" in rendered
    assert "*\U0001F7E1 Qualified To Buy*: *4* deals | *Total amount:* *1,707,000*" in rendered
    assert "*\U0001F535 Presentation Scheduled*: *3* deals | *Total amount:* *146,000*" in rendered
    assert "*\U0001F535 Decision Maker Bought-In*: *3* deals | *Total amount:* *430,000*" in rendered
    assert "*\U0001F7E1 Contract Sent*: *4* deals | *Total amount:* *1,300,000*" in rendered
    assert "*\U0001F7E2 Closed Won*: *4* deals | *Total amount:* *477,000*" in rendered
    assert "*\U0001F534 Closed Lost*: *4* deals | *Total amount:* *980,000*" in rendered


def test_contact_compact_two_column_layout_all_pairs():
    """Verify contact records render in a compact 2-column grid matching Section 3 A."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    url = "https://app.hubspot.com/contacts/123/record/0-1/888"
    response = AgentResponse(
        status="ok",
        text="Contact details",
        request_id="contact-full-grid",
        result={
            "kind": "crm_records",
            "title": "Contact Details",
            "table": {
                "columns": [
                    {"key": "firstname", "label": "First name"},
                    {"key": "email", "label": "Email"},
                    {"key": "lastname", "label": "Last name"},
                    {"key": "phone", "label": "Phone"},
                    {"key": "jobtitle", "label": "Job title"},
                    {"key": "city", "label": "City"},
                    {"key": "state", "label": "State/Region"},
                    {"key": "industry", "label": "Industry"},
                    {"key": "lifecyclestage", "label": "Lifecycle stage"},
                    {"key": "hs_lead_status", "label": "Lead status"},
                    {"key": "owner", "label": "HubSpot owner"},
                    {"key": "createdate", "label": "Create date"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "firstname": "Sarah",
                        "email": "sarah@example.com",
                        "lastname": "Wilson",
                        "phone": "+1 555 0199",
                        "jobtitle": "VP Engineering",
                        "city": "Austin",
                        "state": "Texas",
                        "industry": "Software",
                        "lifecyclestage": "customer",
                        "hs_lead_status": "OPEN",
                        "owner": "169090866",
                        "createdate": "2026-10-07T07:05:11Z",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    # Check all 6 canonical pairs in intended Left | Right columns
    assert _has_field_pair(
        blocks,
        "*👤 First name:* Sarah",
        "*📧 Email:* sarah@example.com",
    )
    assert _has_field_pair(
        blocks,
        "*👤 Last name:* Wilson",
        "*📞 Phone:* +1 555 0199",
    )
    assert _has_field_pair(
        blocks,
        "*Job title:* VP Engineering",
        "*City:* Austin",
    )
    assert _has_field_pair(
        blocks,
        "*State/Region:* Texas",
        "*Industry:* Software",
    )
    assert _has_field_pair(
        blocks,
        "*Lifecycle stage:* customer",
        "*Lead status:* OPEN",
    )
    assert _has_field_pair(
        blocks,
        "*👔 HubSpot owner:* Owner not available",
        "*📅 Create date:* Oct 7, 2026 at 07:05:11 +0000",
    )
    # Link is clickable and present once
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "169090866" not in encoded
    assert f"<{url}|View in HubSpot>" in encoded


def test_contact_compact_two_column_partial_fields_and_fallback():
    """Verify contacts with partial fields (e.g. Image 1) pair remaining fields without vertical stacking."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    url = "https://app.hubspot.com/contacts/123/record/0-1/999"
    response = AgentResponse(
        status="ok",
        text="Here's what I found",
        request_id="contact-partial-grid",
        result={
            "kind": "crm_records",
            "table": {
                "columns": [
                    {"key": "firstname", "label": "First name"},
                    {"key": "email", "label": "Email"},
                    {"key": "lastname", "label": "Last name"},
                    {"key": "createdate", "label": "Create date"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "firstname": "Sarah",
                        "email": None,
                        "lastname": "Wilson",
                        "createdate": "2026-10-07T07:05:11Z",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    # First name and Email (with Not available fallback) are paired
    assert _has_field_pair(
        blocks,
        "*👤 First name:* Sarah",
        "*📧 Email:* Not available",
    )
    # Last name and Create date are paired together into row 2
    assert _has_field_pair(
        blocks,
        "*👤 Last name:* Wilson",
        "*📅 Create date:* Oct 7, 2026 at 07:05:11 +0000",
    )
    # Total section blocks: 1 for heading, 1 with 4 fields, 1 for link
    field_sections = [b for b in blocks if b.get("type") == "section" and "fields" in b]
    assert len(field_sections) == 1
    assert len(field_sections[0]["fields"]) == 4


def test_company_compact_two_column_layout_all_pairs():
    """Verify company records render in a compact 2-column grid matching Section 3 B (Image 2)."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    url = "https://app.hubspot.com/contacts/123/record/0-2/555"
    response = AgentResponse(
        status="ok",
        text="Company listing",
        request_id="company-full-grid",
        result={
            "kind": "crm_records",
            "table": {
                "columns": [
                    {"key": "name", "label": "Company name"},
                    {"key": "domain", "label": "Company domain"},
                    {"key": "industry", "label": "Industry"},
                    {"key": "city", "label": "City"},
                    {"key": "phone", "label": "Phone"},
                    {"key": "lifecyclestage", "label": "Lifecycle stage"},
                    {"key": "hs_lead_status", "label": "Lead status"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "name": "GreenPeak AI",
                        "domain": None,
                        "industry": None,
                        "city": "Hyderabad",
                        "phone": "23853",
                        "lifecyclestage": "customer",
                        "hs_lead_status": None,
                        "owner": "169090866",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    # Row 1: Company name | Company domain
    assert _has_field_pair(
        blocks,
        "*🏢 Company name:* GreenPeak AI",
        "*🌐 Company domain:* Not available",
    )
    # Row 2: Industry | City
    assert _has_field_pair(
        blocks,
        "*🏭 Industry:* Not available",
        "*📍 City:* Hyderabad",
    )
    # Row 3: Phone | Lifecycle stage
    assert _has_field_pair(
        blocks,
        "*Phone:* 23853",
        "*Lifecycle stage:* customer",
    )
    # Row 4: Lead status | Owner ID
    assert _has_field_pair(
        blocks,
        "*Lead status:* Not available",
        "*👤 Owner:* Owner not available",
    )
    assert "169090866" not in json.dumps(blocks, ensure_ascii=False)
    # All 8 fields in a single compact section block
    field_sections = [b for b in blocks if b.get("type") == "section" and "fields" in b]
    assert len(field_sections) == 1
    assert len(field_sections[0]["fields"]) == 8


def test_deal_compact_two_column_layout_with_prominent_title():
    """Verify deal records render with prominent title and 2-column grid matching Section 3 C (Image 4)."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    url = "https://app.hubspot.com/contacts/123/record/0-3/777"
    response = AgentResponse(
        status="ok",
        text="Deal details",
        request_id="deal-prominent-grid",
        result={
            "kind": "crm_records",
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "company", "label": "Account"},
                    {"key": "dealstage", "label": "Stage"},
                    {"key": "closedate", "label": "Close Date"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "dealname": "GlobalHolding 1725955822 Opportunity",
                        "company": "GlobalHolding 1725955822",
                        "dealstage": "prospecting",
                        "closedate": "2026-12-31",
                        "owner": "Abhishek Kumar",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks, ensure_ascii=False)

    # 1. Prominent deal title on its own line
    text_sections = [
        b.get("text", {}).get("text", "")
        for b in blocks
        if b.get("type") == "section" and "text" in b
    ]
    assert any("GlobalHolding 1725955822 Opportunity" in t for t in text_sections)

    # 2. Account | Stage paired
    assert _has_field_pair(
        blocks,
        "*🏢 Account:* GlobalHolding 1725955822",
        "*⚪ Stage:* prospecting",
    )

    # 3. Close Date | Owner paired
    assert _has_field_pair(
        blocks,
        "*📅 Close Date:* Dec 31, 2026",
        "*👤 Owner:* Abhishek Kumar",
    )

    # 4. View in HubSpot link clickable
    assert f"<{url}|View in HubSpot>" in encoded


def test_deal_card_structure_header_accessory_button_and_paired_fields():
    """Verify card structure: emoji, hyperlinked title, badge, accessory button, 4 fields."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    url = "https://app.hubspot.com/contacts/123/record/0-3/999"
    response = AgentResponse(
        status="ok",
        text="Deal listing",
        request_id="deal-card-check",
        result={
            "kind": "crm_card",
            "card": True,
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "company", "label": "Company"},
                    {"key": "dealstage", "label": "Stage"},
                    {"key": "closedate", "label": "Close Date"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "dealname": "Enterprise Renewal",
                        "company": "Acme Corp",
                        "dealstage": "Qualified To Buy",
                        "closedate": "2026-11-15T10:00:00Z",
                        "owner": "Sarah Wilson",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks, ensure_ascii=False)

    # 1. Divider above card
    divider_indices = [i for i, b in enumerate(blocks) if b.get("type") == "divider"]
    assert len(divider_indices) >= 1

    # 2. Header with emoji, hyperlinked title, badge, and accessory button
    header_block = blocks[divider_indices[0] + 1]
    assert header_block["type"] == "section"
    assert "💼" in header_block["text"]["text"]
    assert f"<{url}|Enterprise Renewal>" in header_block["text"]["text"]
    assert "`Deal`" in header_block["text"]["text"]
    assert header_block["accessory"]["type"] == "button"
    assert header_block["accessory"]["text"]["text"] == "View in HubSpot ↗"
    assert header_block["accessory"]["url"] == url

    # 3. Fields in section block (strictly 4)
    fields_block = blocks[divider_indices[0] + 2]
    assert fields_block["type"] == "section"
    fields = fields_block["fields"]
    assert len(fields) == 4
    # Company | Stage
    assert fields[0]["text"] == "*🏢 Company:* Acme Corp"
    assert fields[1]["text"] == "*🟡 Stage:* Qualified To Buy"
    # Close Date | Owner
    assert fields[2]["text"] == "*📅 Close Date:* 2026-11-15"
    assert fields[3]["text"] == "*👤 Owner:* Sarah Wilson"


def test_card_resolves_owner_and_company_never_raw_ids():
    """Verify owner and company names resolve when available, and unresolved IDs are labeled cleanly without misleading values."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    response_resolved = AgentResponse(
        status="ok",
        text="Deal with resolved names",
        request_id="resolved-check",
        result={
            "kind": "crm_card",
            "card": True,
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "company", "label": "Company"},
                    {"key": "dealstage", "label": "Stage"},
                    {"key": "closedate", "label": "Close Date"},
                    {"key": "owner", "label": "Owner"},
                ],
                "rows": [
                    {
                        "dealname": "Named Deal",
                        "company": "00100",
                        "company_name": "Acme Corp",
                        "dealstage": "prospecting",
                        "closedate": "2026-12-31",
                        "owner": "169090866",
                        "owner_name": "Abhishek Kumar",
                    }
                ],
            },
        },
    )

    blocks_resolved = render_response_blocks(response_resolved)
    encoded_resolved = json.dumps(blocks_resolved, ensure_ascii=False)
    assert "*🏢 Company:* Acme Corp" in encoded_resolved
    assert "*👤 Owner:* Abhishek Kumar" in encoded_resolved

    response_unresolved = AgentResponse(
        status="ok",
        text="Deal with raw IDs",
        request_id="raw-ids-check",
        result={
            "kind": "crm_card",
            "card": True,
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "company", "label": "Company"},
                    {"key": "dealstage", "label": "Stage"},
                    {"key": "closedate", "label": "Close Date"},
                    {"key": "owner", "label": "Owner"},
                ],
                "rows": [
                    {
                        "dealname": "Raw ID Deal",
                        "company": "00100",  # raw numeric ID
                        "dealstage": "prospecting",
                        "closedate": "2026-12-31",
                        "owner": "169090866",  # raw numeric ID
                    }
                ],
            },
        },
    )

    blocks_unresolved = render_response_blocks(response_unresolved)
    encoded_unresolved = json.dumps(blocks_unresolved, ensure_ascii=False)

    assert "*🏢 Company:* Not available" in encoded_unresolved
    assert "*👤 Owner:* Owner not available" in encoded_unresolved
    assert "00100" not in encoded_unresolved
    assert "169090866" not in encoded_unresolved

    response_missing = AgentResponse(
        status="ok",
        text="Deal with missing fields",
        request_id="missing-check",
        result={
            "kind": "crm_card",
            "card": True,
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "company", "label": "Company"},
                    {"key": "dealstage", "label": "Stage"},
                    {"key": "closedate", "label": "Close Date"},
                    {"key": "owner", "label": "Owner"},
                ],
                "rows": [
                    {
                        "dealname": "Missing Deal",
                        "company": None,
                        "dealstage": "prospecting",
                        "closedate": "2026-12-31",
                        "owner": None,
                    }
                ],
            },
        },
    )

    blocks_missing = render_response_blocks(response_missing)
    encoded_missing = json.dumps(blocks_missing, ensure_ascii=False)
    assert "*🏢 Company:* Not available" in encoded_missing
    assert "*👤 Owner:* Unassigned" in encoded_missing


def test_card_monetary_amount_formatted_with_dollar_sign():
    """Verify monetary amounts are formatted with $ and commas."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    response = AgentResponse(
        status="ok",
        text="Deal with amount",
        request_id="amount-dollar-check",
        result={
            "kind": "crm_card",
            "card": True,
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "amount", "label": "Amount"},
                    {"key": "dealstage", "label": "Stage"},
                    {"key": "closedate", "label": "Close Date"},
                ],
                "rows": [
                    {
                        "dealname": "High Value Deal",
                        "amount": 850000,
                        "dealstage": "Qualified To Buy",
                        "closedate": "2026-12-31",
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks, ensure_ascii=False)

    assert "$850,000" in encoded
    assert "*💰 Amount:* $850,000" in encoded


def test_card_date_formatted_as_yyyy_mm_dd():
    """Verify dates are formatted as YYYY-MM-DD from various formats."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    response = AgentResponse(
        status="ok",
        text="Contact date test",
        request_id="date-format-check",
        result={
            "kind": "crm_card",
            "card": True,
            "table": {
                "columns": [
                    {"key": "firstname", "label": "First name"},
                    {"key": "lastname", "label": "Last name"},
                    {"key": "email", "label": "Email"},
                    {"key": "createdate", "label": "Create date"},
                ],
                "rows": [
                    {
                        "firstname": "Victor",
                        "lastname": "Hall",
                        "email": "victor@example.com",
                        "createdate": "Oct 7, 2026 at 05:44:11 +0000",
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks, ensure_ascii=False)

    assert "2026-10-07" in encoded
    assert "*📅 Create date:* 2026-10-07" in encoded
    assert "Oct 7, 2026 at 05:44:11" not in encoded


def test_card_divider_present_above_each_card():
    """Verify dividers appear correctly above each card in a multi-record table."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    url1 = "https://app.hubspot.com/contacts/123/record/0-1/1"
    url2 = "https://app.hubspot.com/contacts/123/record/0-1/2"
    response = AgentResponse(
        status="ok",
        text="2 contacts found.",
        request_id="multi-card-dividers",
        result={
            "kind": "crm_card",
            "card": True,
            "table": {
                "columns": [
                    {"key": "firstname", "label": "First name"},
                    {"key": "lastname", "label": "Last name"},
                    {"key": "email", "label": "Email"},
                    {"key": "phone", "label": "Phone"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "firstname": "Contact",
                        "lastname": "One",
                        "email": "one@example.com",
                        "phone": "111",
                        "view_url": url1,
                    },
                    {
                        "firstname": "Contact",
                        "lastname": "Two",
                        "email": "two@example.com",
                        "phone": "222",
                        "view_url": url2,
                    },
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    dividers = [i for i, b in enumerate(blocks) if b.get("type") == "divider"]

    # Exactly 2 cards -> 2 dividers, one before each card
    assert len(dividers) == 2
    # Immediately after each divider is the card header section
    assert blocks[dividers[0] + 1]["type"] == "section"
    assert "Contact One" in blocks[dividers[0] + 1]["text"]["text"]
    assert blocks[dividers[1] + 1]["type"] == "section"
    assert "Contact Two" in blocks[dividers[1] + 1]["text"]["text"]


def test_deal_always_shows_company_field_never_substitutes_amount():
    """Verify company field is always shown (using Not available when missing) and never substituted by Amount."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    response = AgentResponse(
        status="ok",
        text="Deal without company",
        request_id="deal-no-comp",
        result={
            "kind": "crm_card",
            "card": True,
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "amount", "label": "Amount"},
                    {"key": "dealstage", "label": "Stage"},
                    {"key": "closedate", "label": "Close Date"},
                ],
                "rows": [
                    {
                        "dealname": "Standalone Deal",
                        "amount": 850000,
                        "dealstage": "Qualified To Buy",
                        "closedate": "2026-12-31",
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks, ensure_ascii=False)
    # Field 1 is Company (Not available), NEVER substituted by Amount
    assert "*🏢 Company:* Not available" in encoded
    # Amount is preserved as its own requested column
    assert "*💰 Amount:* $850,000" in encoded


def test_preserves_user_requested_columns_and_dynamic_pairs():
    """Verify arbitrary user-requested columns (probability, custom fields) are preserved in card sections."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    response = AgentResponse(
        status="ok",
        text="Deal with extra columns",
        request_id="deal-extra-cols",
        result={
            "kind": "crm_card",
            "card": True,
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "company", "label": "Company"},
                    {"key": "dealstage", "label": "Stage"},
                    {"key": "closedate", "label": "Close Date"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "amount", "label": "Amount"},
                    {"key": "hs_deal_stage_probability", "label": "Probability"},
                ],
                "rows": [
                    {
                        "dealname": "Expansion Deal",
                        "company": "Acme Corp",
                        "dealstage": "Negotiation",
                        "closedate": "2026-11-30",
                        "owner": "Alice Smith",
                        "amount": 125000,
                        "hs_deal_stage_probability": "0.8",
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "*🏢 Company:* Acme Corp" in encoded
    assert "*👤 Owner:* Alice Smith" in encoded
    assert "*💰 Amount:* $125,000" in encoded
    assert "Probability" in encoded and "0.8" in encoded


def test_explicit_card_flag_preempts_legacy_single_record_detail():
    """Verify single-record detail query with explicit card flag uses _render_crm_record_card."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    url = "https://app.hubspot.com/contacts/123/record/0-3/456"
    response = AgentResponse(
        status="ok",
        text="Single deal details",
        request_id="single-card-preempt",
        result={
            "kind": "crm_card",
            "card": True,
            "title": "Deal details",
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "company", "label": "Company"},
                    {"key": "dealstage", "label": "Stage"},
                    {"key": "closedate", "label": "Close Date"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "dealname": "Single Record Deal",
                        "company": "Acme Corp",
                        "dealstage": "Qualified To Buy",
                        "closedate": "2026-12-31",
                        "owner": "Sarah Wilson",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks, ensure_ascii=False)

    # Uses the new CRM card renderer with accessory button and divider
    assert "View in HubSpot ↗" in encoded
    assert f"<{url}|Single Record Deal>" in encoded
    assert "`Deal`" in encoded
    assert "*🏢 Company:* Acme Corp" in encoded
    assert "*🟡 Stage:* Qualified To Buy" in encoded
    # Legacy header is not present
    assert not any(b.get("type") == "header" and "Deal details" in b.get("text", {}).get("text", "") for b in blocks)


def test_normal_crm_intelligence_query_activates_deal_cards_with_header_amount_and_four_fields():
    """Verify normal CRM query without artificial card=True flag activates deal cards."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    url1 = "https://app.hubspot.com/contacts/123/record/0-3/101"
    url2 = "https://app.hubspot.com/contacts/123/record/0-3/102"
    raw_table_text = (
        "Found 2 deals. Showing 2 records.\n"
        "| Deal name | Amount | Stage | Close Date | Owner |\n"
        "| Renewal | 850000 | Qualified | 2026-11-20 | Sarah |\n"
    )
    response = AgentResponse(
        status="ok",
        text=raw_table_text,
        request_id="top-deals-check",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "deal", "limit": 2},
            "summary": {"matched_records": 2, "returned_records": 2},
            "pagination": {"offset": 0, "limit": 2, "has_more": False},
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "amount", "label": "Amount"},
                    {"key": "company", "label": "Company"},
                    {"key": "dealstage", "label": "Stage"},
                    {"key": "closedate", "label": "Close Date"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "dealname": "AI Transformation Project",
                        "amount": 850000,
                        "company": "Acme Corp",
                        "dealstage": "Qualified To Buy",
                        "closedate": "2026-11-20",
                        "owner": "Sarah Wilson",
                        "view_url": url1,
                    },
                    {
                        "dealname": "Cloud Upgrade",
                        "amount": 125000,
                        "company": None,
                        "dealstage": "Contract Sent",
                        "closedate": "2026-12-31",
                        "owner": None,
                        "view_url": url2,
                    },
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    encoded = json.dumps(blocks, ensure_ascii=False)

    # Summary text is clean without raw markdown table
    assert blocks[1]["text"]["text"] == "Found 2 deals. Showing 2 records."
    assert "| Deal name |" not in encoded

    # Dividers
    dividers = [i for i, b in enumerate(blocks) if b.get("type") == "divider"]
    assert len(dividers) == 2

    # Card 1
    h1 = blocks[dividers[0] + 1]
    assert "💼" in h1["text"]["text"]
    assert f"<{url1}|AI Transformation Project>" in h1["text"]["text"]
    assert "`Deal`" in h1["text"]["text"]
    assert "*💰 Amount:* $850,000" in h1["text"]["text"]
    assert h1["accessory"]["text"]["text"] == "View in HubSpot ↗"
    assert h1["accessory"]["url"] == url1

    f1 = blocks[dividers[0] + 2]["fields"]
    assert len(f1) == 4
    assert f1[0]["text"] == "*🏢 Company:* Acme Corp"
    assert f1[1]["text"] == "*🟡 Stage:* Qualified To Buy"
    assert f1[2]["text"] == "*📅 Close Date:* 2026-11-20"
    assert f1[3]["text"] == "*👤 Owner:* Sarah Wilson"

    # Card 2
    h2 = blocks[dividers[1] + 1]
    assert f"<{url2}|Cloud Upgrade>" in h2["text"]["text"]
    assert "*💰 Amount:* $125,000" in h2["text"]["text"]

    f2 = blocks[dividers[1] + 2]["fields"]
    assert len(f2) == 4
    assert f2[0]["text"] == "*🏢 Company:* Not available"
    assert f2[1]["text"] == "*🟡 Stage:* Contract Sent"
    assert f2[2]["text"] == "*📅 Close Date:* 2026-12-31"
    assert f2[3]["text"] == "*👤 Owner:* Unassigned"


def test_normal_crm_intelligence_query_activates_contact_cards():
    """Verify contact list queries like 'show top 3 contact' activate Contact Cards."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    url = "https://app.hubspot.com/contacts/123/record/0-1/301"
    response = AgentResponse(
        status="ok",
        text="Found 71 contacts. Showing 1 records. Use offset 1 to view the next page.\n| Table |",
        request_id="contacts-card-check",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "contact", "limit": 1},
            "summary": {"matched_records": 71, "returned_records": 1},
            "pagination": {"offset": 0, "limit": 1, "has_more": True},
            "table": {
                "columns": [
                    {"key": "firstname", "label": "First name"},
                    {"key": "lastname", "label": "Last name"},
                    {"key": "email", "label": "Email"},
                    {"key": "phone", "label": "Phone"},
                    {"key": "createdate", "label": "Create date"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "firstname": "Maria",
                        "lastname": "Johnson",
                        "email": "maria@example.com",
                        "phone": "+1 555 1234",
                        "createdate": "Sep 17, 2026 at 15:14:04 +0000",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    dividers = [i for i, b in enumerate(blocks) if b.get("type") == "divider"]
    assert len(dividers) == 1

    header = blocks[dividers[0] + 1]
    assert "👤" in header["text"]["text"]
    assert f"<{url}|Maria Johnson>" in header["text"]["text"]
    assert "`Contact`" in header["text"]["text"]
    assert header["accessory"]["text"]["text"] == "View in HubSpot ↗"
    assert header["accessory"]["url"] == url

    fields = blocks[dividers[0] + 2]["fields"]
    assert len(fields) >= 4
    assert "*📅 Create date:* 2026-09-17" in json.dumps(fields, ensure_ascii=False)


def test_normal_crm_intelligence_query_activates_company_cards():
    """Verify company list queries activate Company Cards."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    url = "https://app.hubspot.com/contacts/123/record/0-2/501"
    response = AgentResponse(
        status="ok",
        text="Found 22 companies. Showing 1 records.",
        request_id="company-card-check",
        result={
            "kind": "crm_intelligence",
            "plan": {"object": "company", "limit": 1},
            "summary": {"matched_records": 22, "returned_records": 1},
            "pagination": {"offset": 0, "limit": 1, "has_more": True},
            "table": {
                "columns": [
                    {"key": "name", "label": "Company name"},
                    {"key": "domain", "label": "Company domain"},
                    {"key": "industry", "label": "Industry"},
                    {"key": "city", "label": "City"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "name": "Global Holding",
                        "domain": "globalholding.com",
                        "industry": "Finance",
                        "city": "New York",
                        "owner": "Abhishek Kumar",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    dividers = [i for i, b in enumerate(blocks) if b.get("type") == "divider"]
    assert len(dividers) == 1

    header = blocks[dividers[0] + 1]
    assert "🏢" in header["text"]["text"]
    assert f"<{url}|Global Holding>" in header["text"]["text"]
    assert "`Company`" in header["text"]["text"]
    assert header["accessory"]["text"]["text"] == "View in HubSpot ↗"
    assert header["accessory"]["url"] == url

    fields = blocks[dividers[0] + 2]["fields"]
    assert len(fields) == 4
    assert fields[0]["text"] == "*🌐 Company domain:* globalholding.com"
    assert fields[1]["text"] == "*🏭 Industry:* Finance"
    assert fields[2]["text"] == "*📍 City:* New York"
    assert fields[3]["text"] == "*👤 Owner:* Abhishek Kumar"


def test_zero_deal_amount_is_formatted_as_dollar_zero():
    """Verify deal with 0 amount renders as $0 instead of being treated as missing."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks

    url = "https://app.hubspot.com/contacts/123/record/0-3/701"
    response = AgentResponse(
        status="ok",
        text="Deal with zero amount",
        request_id="zero-deal-amount",
        result={
            "kind": "crm_card",
            "card": True,
            "table": {
                "columns": [
                    {"key": "dealname", "label": "Deal name"},
                    {"key": "amount", "label": "Amount"},
                    {"key": "company", "label": "Company"},
                    {"key": "dealstage", "label": "Stage"},
                    {"key": "closedate", "label": "Close Date"},
                    {"key": "owner", "label": "Owner"},
                    {"key": "view_url", "label": "View in HubSpot"},
                ],
                "rows": [
                    {
                        "dealname": "Pro Bono Project",
                        "amount": 0,
                        "company": "Nonprofit Org",
                        "dealstage": "Closed Won",
                        "closedate": "2026-11-01",
                        "owner": "Sarah",
                        "view_url": url,
                    }
                ],
            },
        },
    )

    blocks = render_response_blocks(response)
    header = [b for b in blocks if b.get("type") == "section" and "Pro Bono Project" in b.get("text", {}).get("text", "")][0]
    assert "*💰 Amount:* $0" in header["text"]["text"]



@pytest.mark.asyncio
async def test_contact_creation_assigns_default_owner_and_displays_owner_name():
    """Verify default owner assignment on contact creation and owner display in Slack metadata."""
    from unittest.mock import AsyncMock
    from app.agent.service import AccountIntelligenceAgent, _ContactDraft
    from app.agent.schemas import AgentRequest
    from app.integrations.hubspot.models import HubSpotContact
    from app.integrations.hubspot.owners import HubSpotOwner
    from app.integrations.slack.home import render_response_blocks

    mock_tools = AsyncMock()
    mock_ai = AsyncMock()
    mock_safety = AsyncMock()
    mock_claim = AsyncMock()
    mock_claim.action_id = "act-123"
    mock_safety.start_direct_action = AsyncMock(return_value=mock_claim)
    mock_safety.complete_action = AsyncMock()

    agent = AccountIntelligenceAgent(mock_tools, mock_ai, mock_safety)

    mock_tools.list_owners.return_value = [
        HubSpotOwner(id="998877", firstName="Alex", lastName="Rivera", email="alex@company.com")
    ]
    created = HubSpotContact(
        id="contact-12345",
        properties={"firstname": "Jordan", "email": "jordan@example.com", "hubspot_owner_id": "998877"},
    )
    mock_tools.create_contact.return_value = created
    mock_tools.hubspot_record_urls.return_value = [
        "https://app.hubspot.com/contacts/42/record/0-1/contact-12345"
    ]

    req = AgentRequest(
        tenant_id="tenant-test",
        actor_id="U1",
        actor_email="alex@company.com",
        message="Create contact Jordan with email jordan@example.com",
        request_id="req-create-owner",
    )
    res = await agent._create_contact_directly(
        req,
        _ContactDraft(properties={"firstname": "Jordan", "email": "jordan@example.com"}, company_name=None),
    )

    assert res.status == "ok"
    assert res.result["owner_id"] == "998877"
    assert res.result["owner_name"] == "Alex Rivera"

    call_args = mock_tools.create_contact.call_args[0]
    assert call_args[1].get("hubspot_owner_id") == "998877"

    blocks = render_response_blocks(res)
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert "👤 Owner: Alex Rivera" in encoded
    assert "HubSpot ID" not in encoded
    assert "Contact ID" not in encoded
    # Visible text sections do not expose raw ID
    for block in blocks:
        if block.get("type") in ("section", "context"):
            text_field = block.get("text", {})
            if isinstance(text_field, dict):
                t = text_field.get("text", "")
                if "View in HubSpot" not in t:
                    assert "contact-12345" not in t
    # But direct record link is preserved
    assert f"<{mock_tools.hubspot_record_urls.return_value[0]}|View in HubSpot>" in encoded


@pytest.mark.asyncio
async def test_hubspot_owner_service_caching_and_tenant_isolation():
    """Verify HubSpotOwnersService caches per-tenant and maintains tenant isolation."""
    from unittest.mock import AsyncMock
    from app.services.hubspot_owners import HubSpotOwnersService
    from app.integrations.hubspot.owners import HubSpotOwner
    from app.integrations.hubspot.context import TenantContext

    mock_client = AsyncMock()
    mock_token_provider = AsyncMock()
    token_a = AsyncMock(hubspot_account_id="acc-a", scopes=["crm.objects.owners.read"])
    token_b = AsyncMock(hubspot_account_id="acc-b", scopes=["crm.objects.owners.read"])
    mock_token_provider.get_access_token.side_effect = lambda t: (
        ("tok-a", token_a) if t == "tenant-a" else ("tok-b", token_b)
    )

    owner_a = [HubSpotOwner(id="1", firstName="Owner", lastName="A")]
    owner_b = [HubSpotOwner(id="2", firstName="Owner", lastName="B")]
    mock_client.list_owners.side_effect = lambda ctx, tok: (
        owner_a if ctx.tenant_id == "tenant-a" else owner_b
    )

    svc = HubSpotOwnersService(mock_client, mock_token_provider, cache_ttl_seconds=60.0)

    ctx_a = TenantContext(tenant_id="tenant-a", hubspot_account_id="acc-a", credential_reference="ref")
    res_a1 = await svc.list_owners(ctx_a)
    assert res_a1[0].display_name == "Owner A"
    assert mock_client.list_owners.call_count == 1

    res_a2 = await svc.list_owners(ctx_a)
    assert res_a2[0].display_name == "Owner A"
    assert mock_client.list_owners.call_count == 1

    ctx_b = TenantContext(tenant_id="tenant-b", hubspot_account_id="acc-b", credential_reference="ref")
    res_b = await svc.list_owners(ctx_b)
    assert res_b[0].display_name == "Owner B"
    assert mock_client.list_owners.call_count == 2


def test_sensitive_record_ids_hidden_from_all_slack_responses():
    """Verify raw record IDs are stripped from all user-facing Slack blocks and fallback text."""
    from app.agent.schemas import AgentResponse
    from app.integrations.slack.home import render_response_blocks
    from app.integrations.slack.response_formatting import format_slack_response

    res_needs_field = AgentResponse(
        status="needs_field",
        text="Which field would you like to update?",
        request_id="req-nf",
        result={
            "kind": "interactive_field_selection",
            "contact_id": "contact_987654",
            "contact_name": "Taylor",
            "email": "taylor@test.com",
            "fields": [{"label": "Phone", "value": "phone"}],
        },
    )
    blocks_nf = render_response_blocks(res_needs_field)
    encoded_nf = json.dumps(blocks_nf, ensure_ascii=False)
    assert "HubSpot ID" not in encoded_nf
    select = blocks_nf[2]["elements"][0]
    assert select["options"][0]["value"] == "contact_987654:phone"

    res_crud = AgentResponse(
        status="ok",
        text="Contact created successfully in HubSpot.\n• Contact ID: `5551234`\n• Email: test@example.com",
        request_id="req-fb",
    )
    formatted = format_slack_response(res_crud)
    assert "5551234" not in formatted.fallback_text
    assert "Contact ID" not in formatted.fallback_text
    assert "test@example.com" in formatted.fallback_text
