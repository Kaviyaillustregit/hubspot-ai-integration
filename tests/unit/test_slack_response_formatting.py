import asyncio
import json

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

    class AllowAuthorization:
        async def is_authorized(self, *, tenant_id, slack_team_id, slack_user_id):
            return (tenant_id, slack_team_id, slack_user_id) == ("tenant-1", "T1", "U1")

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

    asyncio.run(
        _respond(Agent(), Client(), AllowAuthorization(), payload, "tenant-1", "request-1")
    )

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
        ("Deal Details", "deal", "Deal details"),
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
    sections = [block for block in blocks if block.get("type") == "section"]
    section_text = "\n".join(block["text"]["text"] for block in sections)
    channel = format_slack_response(response)
    channel_encoded = json.dumps(channel.messages, ensure_ascii=False)

    assert any(
        block.get("type") == "header" and header in block["text"]["text"]
        for block in blocks
    )
    assert all("fields" not in block for block in sections)
    assert "*Name:* Cloud Migration Project" in section_text
    assert "*Owner ID:* 98711688" in section_text
    assert "*Stage:* Qualified To Buy" in section_text
    assert "*Amount:* 250,000" in section_text
    assert "*Close Date:* Dec 10, 2026" in section_text
    assert "Owner ID" in encoded
    assert "Ada" not in encoded
    assert "Dec 10, 2026" in encoded
    assert f"<{url}|View in HubSpot>" in encoded
    assert encoded.count(url) == 1
    assert "&lt;https://app.hubspot.com" not in encoded
    assert channel.fallback_text == response.text
    assert channel_encoded.count(url) == 1
    assert any(block.get("type") == "header" for page in channel.messages for block in page)


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
                {"group": "Qualified", "count": 3, "sum_amount": "125000"},
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
    assert "*Qualified*" in section_text
    assert "*Qualified*: 3 deals" in section_text
    assert "*Total amount:* 125,000" in section_text
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
    assert rendered.count("*Total amount:* 125,000") == 1
    assert "*Maximum amount:* 125,000" in rendered


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
    long_chunks = [
        block["text"]["text"]
        for block in blocks
        if block.get("type") == "section" and isinstance(block.get("text"), dict)
    ]

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
