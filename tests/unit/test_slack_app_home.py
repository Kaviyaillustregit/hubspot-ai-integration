import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import unquote, urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.crm_query import DealFilterSpec, detect_deal_filter
from app.agent.schemas import AgentResponse
from app.api.app import create_app
from app.api.slack import (
    get_action_safety,
    get_agent,
    get_conversation_history,
    get_slack_authorization,
    get_slack_client,
)
from app.core.config import Settings
from app.integrations.errors import IntegrationError
from app.integrations.slack.events import parse_app_home_opened
from app.integrations.slack.home import (
    CONFIRM_ACTION,
    QUICK_ACTION_PREFIX,
    REQUEST_INPUT_ACTION,
    SUBMIT_ACTION,
    VIEW_FULL_RESPONSE_ACTION,
    build_home_view,
    parse_home_interaction,
    render_response_blocks,
)
from app.integrations.slack.http_client import SlackWebApiClient
from app.integrations.slack.welcome_banner import (
    BANNER_ALT_TEXT,
    BANNER_ASSET_PATH,
    build_welcome_banner_block,
    build_welcome_banner_url,
)
from app.services.action_safety import RecentAction
from app.services.quickchart import QuickChartService

ACTION_ID = "0123456789abcdef0123456789abcdef"


def all_text(blocks: list[dict]) -> str:
    return json.dumps(blocks, ensure_ascii=False)


def blocks_of_type(view: dict, block_type: str) -> list[dict]:
    return [block for block in view["blocks"] if block["type"] == block_type]


# --- Home view rendering -------------------------------------------------------------


def test_home_view_has_compact_branding_input_send_and_quick_actions():
    view = build_home_view(recent=[])
    blocks = view["blocks"]
    assert view["type"] == "home"
    assert len(blocks) <= 100
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "HubSpot AI Assistant"
    assert not blocks_of_type(view, "card")
    assert not blocks_of_type(view, "image")
    text = all_text(blocks)
    assert "Copilot" not in text
    assert "Ask about deals and pipeline" in text
    assert "Recent CRM Activity" in text
    assert "No CRM actions yet" in text

    [request_input] = blocks_of_type(view, "input")
    assert request_input["label"]["text"] == "Message HubSpot AI"
    assert request_input["element"]["action_id"] == REQUEST_INPUT_ACTION
    assert request_input["element"]["placeholder"]["text"] == (
        "Ask about deals, contacts, companies, or your pipeline..."
    )
    quick_buttons = [
        element
        for block in blocks_of_type(view, "actions")
        for element in block["elements"]
        if element["action_id"].startswith(QUICK_ACTION_PREFIX)
    ]
    assert [button["action_id"] for button in quick_buttons] == [
        f"{QUICK_ACTION_PREFIX}{key}"
        for key in ("create_contact", "find_contact", "find_company", "account_overview", "search_crm")
    ]
    assert [button["value"] for button in quick_buttons] == [
        "Create a contact named ", "Find a contact named ", "Tell me about the company ",
        "Give me an account overview of ", "Search HubSpot for ",
    ]
    assert [button["text"]["text"] for button in quick_buttons] == [
        "Create contact", "Find contact", "Find company", "Account overview", "Search CRM"
    ]
    assert all(button["text"]["emoji"] is False for button in quick_buttons)
    [send] = [
        element for block in blocks_of_type(view, "actions") for element in block["elements"]
        if element["action_id"] == SUBMIT_ACTION
    ]
    assert send["value"] == "send"
    input_index = blocks.index(request_input)
    activity_index = next(i for i, block in enumerate(blocks) if "Recent CRM Activity" in all_text([block]))
    assert input_index < activity_index


def test_welcome_banner_is_first_and_preserves_existing_home_blocks():
    baseline = build_home_view(recent=[])["blocks"]
    image_url = build_welcome_banner_url("https://assistant.example.test/")

    view = build_home_view(recent=[], banner_image_url=image_url)

    assert view["blocks"][0] == {
        "type": "image",
        "image_url": "https://assistant.example.test/assistant/static/hubspot-ai-welcome.png",
        "alt_text": BANNER_ALT_TEXT,
    }
    def stable_block_ids(blocks):
        return [
            {**block, "block_id": "<generated>"}
            if block.get("type") == "input"
            else block
            for block in blocks
        ]

    assert stable_block_ids(view["blocks"][1:]) == stable_block_ids(baseline)
    assert view["blocks"][0]["alt_text"].startswith("Welcome to HubSpot AI Assistant")


def test_welcome_banner_url_requires_an_absolute_https_base():
    assert build_welcome_banner_url("http://localhost:8000") is None
    assert build_welcome_banner_url("https://user:pass@example.test") is None
    assert build_welcome_banner_block("/assistant/static/hubspot-ai-welcome.png") is None


def test_welcome_banner_png_is_bundled_for_the_existing_static_route():
    assert BANNER_ASSET_PATH.is_file()
    assert BANNER_ASSET_PATH.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

def test_quick_action_draft_prefills_a_fresh_input_block():
    first = blocks_of_type(build_home_view(draft="Create a contact named "), "input")[0]
    second = blocks_of_type(build_home_view(draft="Create a contact named "), "input")[0]

    assert first["element"]["initial_value"] == "Create a contact named "
    assert first["block_id"] != second["block_id"]


def test_working_state_shows_user_and_assistant_together():
    blocks = build_home_view(request_text="Tell me about <ABC>", working=True)["blocks"]
    user_index = next(
        i for i, block in enumerate(blocks)
        if block["type"] == "rich_text" and block["elements"][0]["type"] == "rich_text_quote"
    )
    quote = blocks[user_index]["elements"][0]
    assert quote["type"] == "rich_text_quote"
    assert quote["elements"] == [{"type": "text", "text": "Tell me about <ABC>"}]
    assistant = blocks[user_index + 1]
    assert assistant["type"] == "rich_text"
    assert "HubSpot AI Assistant" in all_text([assistant])
    assert blocks[user_index + 2]["text"]["text"] == "Working..."
    input_index = next(i for i, block in enumerate(blocks) if block["type"] == "input")
    assert user_index < input_index

def test_contact_created_renders_professional_fields():
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text="Contact Angel John was created successfully in HubSpot.",
            request_id="r",
            result={
                "kind": "contact_created",
                "contact_id": "565238436562",
                "name": "Angel John",
                "email": "angel@example.com",
                "company_name": "ABC <!channel>",
                "hubspot_url": "https://app.hubspot.com/contacts/42/record/0-1/565238436562",
            },
        )
    )

    assert blocks[0]["text"]["text"] == "*Contact created*"
    assert blocks[1]["text"]["text"] == (
        "Angel John was successfully added to HubSpot. Associated with ABC &lt;!channel&gt;."
    )
    assert blocks[2]["elements"][0]["text"] == (
        "angel@example.com  ·  HubSpot ID `565238436562`"
    )
    assert blocks[3]["text"]["text"] == (
        "<https://app.hubspot.com/contacts/42/record/0-1/565238436562|View in HubSpot>"
    )


@pytest.mark.parametrize(
    ("kind", "title", "record_url"),
    [
        (
            "company",
            "Company created",
            "https://app.hubspot.com/contacts/42/record/0-2/company-1",
        ),
        (
            "deal",
            "Deal created",
            "https://app.hubspot.com/contacts/42/record/0-3/deal-1",
        ),
    ],
)
def test_created_crm_record_cards_render_existing_hubspot_links(kind, title, record_url):
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text=f"{title}.",
            request_id="r",
            result={"kind": "crm_records", "title": "CRM Updated"},
            cards=[
                {
                    "kind": kind,
                    "title": title,
                    "name": "New record",
                    "hubspot_url": record_url,
                }
            ],
        )
    )

    assert blocks[-1]["text"]["text"] == f"<{record_url}|View in HubSpot>"


def test_contact_detail_card_renders_field_emojis():
    url = "https://app.hubspot.com/contacts/42/record/0-1/1"
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text="Contact details.",
            request_id="contact-card-emojis",
            result={
                "kind": "crm_intelligence",
                "title": "Contact details",
                "hubspot_url": url,
                "table": {
                    "columns": [
                        {"key": "name", "label": "Contact name"},
                        {"key": "email", "label": "Email"},
                        {"key": "phone", "label": "Phone"},
                        {"key": "company", "label": "Company"},
                        {"key": "owner", "label": "Owner"},
                    ],
                    "rows": [{
                        "name": "Ava",
                        "email": "ava@example.com",
                        "phone": "555-000",
                        "company": "Acme",
                        "owner": "Pat",
                        "view_url": url,
                    }],
                },
            },
        )
    )

    section_text = "\n".join(block["text"]["text"] for block in blocks if block.get("type") == "section")
    assert "*\U0001F464 Contact name:* Ava" in section_text
    assert "*\U0001F4E7 Email:* ava@example.com" in section_text
    assert "*\U0001F4DE Phone:* 555-000" in section_text
    assert "*\U0001F3E2 Company:* Acme" in section_text
    assert "*\U0001F454 Owner:* Pat" in section_text


def test_company_detail_card_renders_field_emojis():
    url = "https://app.hubspot.com/contacts/42/record/0-2/company-1"
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text="Company details.",
            request_id="company-card-emojis",
            result={
                "kind": "crm_intelligence",
                "title": "Company details",
                "hubspot_url": url,
                "table": {
                    "columns": [
                        {"key": "name", "label": "Company name"},
                        {"key": "domain", "label": "Domain"},
                        {"key": "industry", "label": "Industry"},
                        {"key": "city", "label": "City"},
                        {"key": "employees", "label": "Employees"},
                        {"key": "owner", "label": "Owner"},
                    ],
                    "rows": [{
                        "name": "Acme",
                        "domain": "acme.com",
                        "industry": "Manufacturing",
                        "city": "Boston",
                        "employees": "100",
                        "owner": "Alex",
                        "view_url": url,
                    }],
                },
            },
        )
    )

    section_text = "\n".join(block["text"]["text"] for block in blocks if block.get("type") == "section")
    assert "*\U0001F3E2 Company name:* Acme" in section_text
    assert "*\U0001F310 Domain:* acme.com" in section_text
    assert "*\U0001F3ED Industry:* Manufacturing" in section_text
    assert "*\U0001F4CD City:* Boston" in section_text
    assert "*\U0001F465 Employees:* 100" in section_text
    assert "*\U0001F464 Owner:* Alex" in section_text




def test_single_record_result_renders_existing_hubspot_link():
    record_url = "https://app.hubspot.com/contacts/42/record/0-2/company-1"
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text="Company updated.",
            request_id="r",
            result={"kind": "crm_records", "hubspot_url": record_url},
        )
    )

    assert blocks[-1]["text"]["text"] == f"<{record_url}|View in HubSpot>"


def test_deal_chart_result_renders_image_block():
    chart_url = "https://quickchart.io/chart?c=%7B%22type%22%3A%22bar%22%7D"
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text="Found 2 open deals.",
            request_id="r",
            result={
                "kind": "deal_chart",
                "chart_url": chart_url,
                "chart_title": "Top 2 deals by value",
            },
        )
    )

    assert blocks[0]["text"]["text"] == "*Here's what I found*"
    assert blocks[1]["text"]["text"] == "Found 2 open deals."
    assert blocks[2]["type"] == "image"
    assert blocks[2]["image_url"] == chart_url


def test_quickchart_service_generates_chart_for_deals_from_real_data():
    deals = [
        SimpleNamespace(id="deal-1", properties={"dealname": "Alpha", "amount": "120000"}),
        SimpleNamespace(id="deal-2", properties={"dealname": "Beta", "amount": "60000"}),
    ]

    assert QuickChartService.detect_chart_type("Show me the top 2 highest-value deals as a bar chart") == "bar"
    chart_url = QuickChartService.chart_for_deals(
        deals,
        title="Top deals by value",
        chart_type="bar",
        limit=2,
    )

    assert chart_url is not None
    assert chart_url.startswith("https://quickchart.io/chart?")
    assert "c=" in chart_url
    assert "%22Alpha%22" in chart_url


def test_bar_chart_renders_full_labels_zero_baseline_and_currency_ticks():
    deals = [
        SimpleNamespace(id="deal-1", properties={"dealname": "Regional Expansion North America 2025", "amount": "3500000"}),
        SimpleNamespace(id="deal-2", properties={"dealname": "Global Services Renewal 2025", "amount": "2500000"}),
        SimpleNamespace(id="deal-3", properties={"dealname": "Enterprise Upsell West Region", "amount": "1500000"}),
        SimpleNamespace(id="deal-4", properties={"dealname": "Customer Success Add-On Bundle", "amount": "500000"}),
        SimpleNamespace(id="deal-5", properties={"dealname": "Consulting Retainer 2025", "amount": "600000"}),
    ]

    chart_url = QuickChartService.chart_for_deals(
        deals,
        title="Top deals by value",
        chart_type="bar",
        limit=5,
    )

    assert chart_url is not None
    decoded = unquote(chart_url)
    assert '"beginAtZero":true' in decoded
    assert '"maxRotation":45' in decoded
    assert "toLocaleString('en-US')" in decoded
    assert "Regional Expansion North America 2025" in decoded
    assert "Customer Success Add-On Bundle" in decoded


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Show me the top 5 deals as a doughnut chart", "doughnut"),
        ("Show me a pie chart for open deals", "pie"),
        ("Show me the sales pipeline as a funnel chart", "funnel"),
        ("Show me the pipeline chart", "funnel"),
    ],
)
def test_quickchart_service_detects_supported_chart_types(message, expected):
    assert QuickChartService.detect_chart_type(message) == expected


def test_quickchart_service_generates_pipeline_chart_from_real_stage_data():
    deals = [
        SimpleNamespace(id="deal-1", properties={"dealname": "Alpha", "amount": "120000", "dealstage": "qualified"}),
        SimpleNamespace(id="deal-2", properties={"dealname": "Beta", "amount": "60000", "dealstage": "qualified"}),
        SimpleNamespace(id="deal-3", properties={"dealname": "Gamma", "amount": "20000", "dealstage": "proposal"}),
    ]

    chart_url = QuickChartService.chart_for_deals(
        deals,
        title="Sales pipeline",
        chart_type="funnel",
        limit=10,
        pipeline_stage_map={"qualified": "Qualified", "proposal": "Proposal"},
        pipeline_stage_order=["qualified", "proposal", "closedwon"],
    )

    assert chart_url is not None
    decoded = unquote(chart_url)
    assert '"type":"bar"' in decoded
    assert '"indexAxis":"y"' in decoded
    assert "Qualified" in decoded
    assert "Proposal" in decoded
    assert QuickChartService.chart_summary_for_deals(
        deals,
        chart_type="funnel",
        pipeline_stage_map={"qualified": "Qualified", "proposal": "Proposal"},
        pipeline_stage_order=["qualified", "proposal", "closedwon"],
    ) == (
        "3 deals are in the pipeline with a total value of $200,000.00. "
        "The largest stage is Qualified at $180,000.00."
    )


@pytest.mark.parametrize(
    ("message", "expected_type"),
    [
        ("Show me the distribution of deals by pipeline stage as a doughnut chart.", "doughnut"),
        ("Show my sales pipeline as a funnel chart with stage-wise colors and summary.", "funnel"),
        ("Show the deal values by stage as a pie chart.", "pie"),
    ],
)
def test_deal_chart_messages_are_not_misread_as_pipeline_filters(message, expected_type):
    assert QuickChartService.detect_chart_type(message) == expected_type
    assert detect_deal_filter(message) is None


def test_deal_filter_spec_ignores_visualization_and_sort_metadata():
    spec = DealFilterSpec(
        **{
            "status": "open",
            "sort_by": "amount",
            "sort_order": "desc",
            "visualization": "bar",
            "limit": 3,
            "sort": {"property": "amount", "direction": "desc"},
        }
    )

    assert spec.limit == 3
    assert spec.sort is not None
    assert spec.sort.property == "amount"
    assert spec.sort.direction == "desc"
    assert spec.status == "open"
    assert spec.model_dump(exclude_none=True) == {
        "conditions": [],
        "stages": [],
        "status": "open",
        "unassigned": False,
        "sort": {"property": "amount", "direction": "desc"},
        "limit": 3,
        "unsupported": [],
    }


def test_top_three_highest_value_open_deals_still_uses_normal_deal_filter_flow():
    message = "Show me the top 3 highest-value open deals."
    flt = detect_deal_filter(message)
    assert flt is not None
    assert flt.limit == 3
    assert flt.sort is not None
    assert flt.sort.direction == "desc"
    assert not flt.stages


def test_chart_summary_renders_under_image_block():
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text="Found 2 open deals.",
            request_id="r",
            result={
                "kind": "deal_chart",
                "chart_url": "https://quickchart.io/chart?c=%7B%22type%22%3A%22bar%22%7D",
                "chart_title": "Sales pipeline",
                "chart_summary": "2 deals total $180,000.00 in value.",
            },
        )
    )

    text = all_text(blocks)
    assert "Found 2 open deals." in text
    assert "2 deals total $180,000.00 in value." in text


def test_pending_confirmation_renders_confirm_button_instead_of_typed_instructions():
    blocks = render_response_blocks(
        AgentResponse(
            status="pending_confirmation",
            text=(
                "I found a request to delete this HubSpot contact:\n• Contact ID: 123\n\n"
                f"Action ID: `{ACTION_ID}`\n"
                f"Reply with `confirm {ACTION_ID}` to delete this contact."
            ),
            request_id="r",
            result={
                "kind": "pending_confirmation",
                "action_id": ACTION_ID,
                "action_type": "delete_contact",
                "contact_id": "123",
            },
        )
    )

    text = all_text(blocks)
    assert "*Confirmation needed*" in text
    assert "Reply with" not in text
    [actions] = [block for block in blocks if block["type"] == "actions"]
    button = actions["elements"][0]
    assert button["action_id"] == CONFIRM_ACTION
    assert button["value"] == ACTION_ID
    assert button["style"] == "danger"
    assert button["confirm"]["confirm"]["text"] == "Delete"


@pytest.mark.parametrize(
    ("status", "title"),
    [
        ("missing_fields", "*More information needed*"),
        ("invalid_request", "*More information needed*"),
        ("company_not_found", "*Unable to complete request*"),
        ("duplicate", "*Unable to complete request*"),
        ("hubspot_not_authorized", "*Unable to complete request*"),
        ("unavailable", "*Unable to complete request*"),
    ],
)
def test_non_success_states_are_clearly_labelled(status, title):
    blocks = render_response_blocks(
        AgentResponse(status=status, text="Reason <@U123>.", request_id="r")
    )

    assert blocks[0]["text"]["text"] == title
    assert blocks[1]["text"]["text"] == "Reason &lt;@U123&gt;."


def test_summary_text_is_split_into_section_sized_chunks():
    long_text = "\n\n".join("• fact " + "x" * 1000 for _ in range(6))
    blocks = render_response_blocks(AgentResponse(status="ok", text=long_text, request_id="r"))

    assert blocks[0]["text"]["text"] == "*Here's what I found*"
    assert len(blocks) > 2
    assert all(len(block["text"]["text"]) <= 3000 for block in blocks)


@pytest.mark.parametrize(
    ("columns", "rows", "expected_values"),
    [
        (
            [
                {"key": "name", "label": "Company"},
                {"key": "industry", "label": "Industry"},
                {"key": "view_url", "label": "View in HubSpot"},
            ],
            [
                {
                    "name": "Northwind",
                    "industry": "Software",
                    "view_url": "https://app.hubspot.com/contacts/42/record/0-2/1",
                },
                {
                    "name": "Contoso",
                    "industry": "Manufacturing",
                    "view_url": "https://app.hubspot.com/contacts/42/record/0-2/2",
                },
            ],
            ("Company", "Industry", "Northwind", "Contoso"),
        ),
        (
            [
                {"key": "name", "label": "Contact"},
                {"key": "email", "label": "Email"},
                {"key": "view_url", "label": "View in HubSpot"},
            ],
            [
                {
                    "name": "Alex Smith",
                    "email": "alex@example.com",
                    "view_url": "https://app.hubspot.com/contacts/42/record/0-1/3",
                },
                {
                    "name": "Sam Jones",
                    "email": "sam@example.com",
                    "view_url": "https://app.hubspot.com/contacts/42/record/0-1/4",
                },
            ],
            ("Contact", "Email", "Alex Smith", "Sam Jones"),
        ),
        (
            [
                {"key": "name", "label": "Deal Name"},
                {"key": "company", "label": "Associated Company"},
                {"key": "owner", "label": "Deal Owner"},
                {"key": "stage", "label": "Deal Stage"},
                {"key": "amount", "label": "Amount"},
                {"key": "probability", "label": "Probability"},
                {"key": "close_date", "label": "Close Date"},
                {"key": "view_url", "label": "View in HubSpot"},
            ],
            [
                {
                    "name": "Renewal",
                    "company": "Northwind",
                    "owner": "Owner 1",
                    "stage": "Negotiation",
                    "amount": "$500",
                    "probability": "75%",
                    "close_date": "2026-12-31",
                    "view_url": "https://app.hubspot.com/contacts/42/record/0-3/5",
                },
                {
                    "name": "Expansion",
                    "company": "Contoso",
                    "owner": "Owner 2",
                    "stage": "Qualified",
                    "amount": "$900",
                    "probability": "50%",
                    "close_date": "2027-01-31",
                    "view_url": "https://app.hubspot.com/contacts/42/record/0-3/6",
                },
            ],
            (
                "Deal Name",
                "Associated Company",
                "Deal Owner",
                "Deal Stage",
                "Amount",
                "Probability",
                "Close Date",
                "Renewal",
                "Expansion",
            ),
        ),
    ],
)
def test_structured_tables_render_dynamic_columns_rows_and_hubspot_links(
    columns, rows, expected_values
):
    response = AgentResponse(
        status="ok",
        text=f"Found {len(rows)} records.",
        request_id="r",
        result={"kind": "crm_records", "table": {"columns": columns, "rows": rows}},
    )

    blocks = render_response_blocks(response)
    text = all_text(blocks)

    assert blocks[1]["text"]["text"] == f"Found {len(rows)} records."
    assert all(value in text for value in expected_values)
    assert text.count("|View in HubSpot>") == len(rows)
    record_blocks = [
        block
        for block in blocks[2:]
        if block["type"] == "section" and block.get("text", {}).get("text")
    ]
    assert all(
        block["type"] == "section"
        and "fields" not in block
        and len(block["text"]["text"]) <= 3000
        for block in blocks[2:]
        if block["type"] == "section"
    )
    assert len(record_blocks) == len(rows)
    assert sum(block["type"] == "divider" for block in blocks) == len(rows) - 1


def test_structured_table_without_view_url_renders_rows_without_links():
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text="Found 1 company.",
            request_id="r",
            result={
                "table": {
                    "columns": [{"key": "name", "label": "Company"}],
                    "rows": [{"name": "Northwind"}],
                }
            },
        )
    )

    text = all_text(blocks)
    assert "Found 1 company." in text
    assert "Northwind" in text
    assert "|View in HubSpot>" not in text


@pytest.mark.parametrize(
    ("object_type", "record_name", "record_url"),
    [
        ("contacts", "Ari Example", "https://app.hubspot.com/contacts/42/record/0-1/101"),
        ("companies", "Northwind Ltd", "https://app.hubspot.com/contacts/42/record/0-2/202"),
    ],
)
def test_batch2_app_home_displays_complete_crm_record_once_with_clickable_link(
    object_type, record_name, record_url
):
    columns = [
        {"key": "name", "label": "Name"},
        {"key": "custom_value", "label": "Custom field"},
        {"key": "missing_value", "label": "Optional field"},
        {"key": "view_url", "label": "View in HubSpot"},
    ]
    rows = [
        {
            "name": record_name,
            "custom_value": "Customer",
            "missing_value": None,
            "view_url": record_url,
        }
    ]
    response = AgentResponse(
        status="ok",
        text=(
            f"Found 1 {object_type}.\n"
            f"*Name:* {record_name} | *Custom field:* Customer | "
            "*Optional field:* Not available | "
            f"*View in HubSpot:* <{record_url}|Open in HubSpot>"
        ),
        request_id="batch2-final-response",
        result={
            "kind": "crm_intelligence",
            "table": {"columns": columns, "rows": rows},
        },
    )

    blocks = render_response_blocks(response)
    rendered = all_text(blocks)

    assert rendered.count(record_name) == 1
    assert rendered.count("Customer") == 1
    assert rendered.count("Not available") == 1
    assert rendered.count(record_url) == 1
    assert f"<{record_url}|View in HubSpot>" in rendered
    assert all(
        len(block.get("text", {}).get("text", "")) <= 3000
        for block in blocks
        if block["type"] == "section"
    )


def test_large_structured_table_stays_within_slack_home_block_limit():
    response = AgentResponse(
        status="ok",
        text="Found 100 records.",
        request_id="r",
        result={
            "table": {
                "columns": [{"key": "name", "label": "Name"}],
                "rows": [{"name": f"Record {index}"} for index in range(100)],
            }
        },
    )

    blocks = build_home_view(response=response)["blocks"]

    assert len(blocks) <= 100
    assert any(
        block.get("elements", [{}])[0].get("text", "").startswith("Showing ")
        for block in blocks
        if block["type"] == "context"
    )
    assert any(block["type"] == "input" for block in blocks)


def test_non_table_response_keeps_existing_text_fallback():
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text="Closed-Won revenue: $12,000.",
            request_id="r",
            result={"kind": "revenue_summary", "total_amount": "$12,000"},
        )
    )

    assert blocks[0]["text"]["text"] == "*Here's what I found*"
    assert blocks[1]["text"]["text"] == "Closed-Won revenue: $12,000."


def test_recent_activity_uses_compact_plain_rows_with_consistent_dates():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    recent = [
        RecentAction("create_contact", "completed", {"firstname": "Angel", "lastname": "John"}, now, now),
        RecentAction("delete_contact", "pending", {"contact_id": "123"}, now, now + timedelta(minutes=4)),
        RecentAction("update_contact", "pending", {"contact_id": "456"}, now - timedelta(hours=1), now - timedelta(minutes=55)),
    ]
    blocks = build_home_view(recent=recent, now=now)["blocks"]
    text = all_text(blocks)
    assert "Completed: Created contact Angel John" in text
    assert "Awaiting confirmation: Delete contact 123" in text
    assert "Expired: Update contact 456" in text
    assert text.count("<!date^") == 3
    assert not blocks_of_type({"blocks": blocks}, "card")
    assert not any(ord(char) in {0x2705, 0x1F6E1, 0x26A0, 0x2757} for char in text)

def test_recent_activity_empty_and_unavailable_states():
    assert "No CRM actions yet" in all_text(build_home_view(recent=[])["blocks"])
    assert "unavailable" in all_text(build_home_view(recent_unavailable=True)["blocks"])


# --- Interaction parsing ------------------------------------------------------------


def home_payload(action: dict, *, view_type: str = "home", request_text: str = "") -> dict:
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1", "team_id": "T1"},
        "view": {
            "type": view_type,
            "blocks": [{"type": "input", "block_id": "home_request:abc"}],
            "state": {
                "values": {
                    "home_request:abc": {
                        REQUEST_INPUT_ACTION: {"type": "plain_text_input", "value": request_text}
                    }
                }
            },
        },
        "actions": [{"action_ts": "1712345678.000100", **action}],
    }


def stale_state_payload(current_text: str) -> dict:
    """Shape of the real Slack payload from the failed test (13:27, 7 Oct 2026)."""

    def entry(value: str | None) -> dict:
        return {REQUEST_INPUT_ACTION: {"type": "plain_text_input", "value": value}}

    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1", "team_id": "T1"},
        "view": {
            "type": "home",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "How can I help you?"}},
                {"type": "input", "block_id": "home_request:ac9587d8b86c"},
                {"type": "actions", "block_id": "home_submit_actions"},
            ],
            "state": {
                "values": {
                    "home_request:7f3bd79e47dc": entry(None),
                    "home_request:36ac9ca5403d": entry("Tell me about the company "),
                    "home_request:9b475e12abc1": entry("Give me an account overview of "),
                    "home_request:d153fa32c192": entry("Create a company called Ai acro Company."),
                    "home_request:ac9587d8b86c": entry(current_text),
                }
            },
        },
        "actions": [{"action_id": SUBMIT_ACTION, "value": "send", "action_ts": "1791.0001"}],
    }


def test_send_reads_the_input_on_screen_not_stale_quick_action_text():
    interaction = parse_home_interaction(
        stale_state_payload("Create a company called Test AI Company.")
    )

    assert interaction is not None
    assert interaction.kind == "submit"
    assert interaction.text == "Create a company called Test AI Company."


def test_send_with_empty_on_screen_input_ignores_stale_values():
    interaction = parse_home_interaction(stale_state_payload(""))

    assert interaction is not None
    assert interaction.text == ""


def test_home_send_delivers_the_typed_request_to_the_agent():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()

    post_interaction(
        app_with(agent, client, safety),
        stale_state_payload("Create a company called Test AI Company."),
    )

    assert [request.message for request in agent.requests] == [
        "Create a company called Test AI Company."
    ]
    assert "Create a company called Test AI Company." in all_text(client.views[-1][1]["blocks"])


def test_send_button_reads_request_from_view_state():
    interaction = parse_home_interaction(
        home_payload({"action_id": SUBMIT_ACTION}, request_text="  Create Angel John  ")
    )

    assert interaction is not None
    assert interaction.kind == "submit"
    assert interaction.text == "Create Angel John"
    assert (interaction.team_id, interaction.user_id) == ("T1", "U1")
    assert interaction.action_ts == "1712345678.000100"


def test_enter_in_input_uses_the_dispatched_value():
    interaction = parse_home_interaction(
        home_payload({"action_id": REQUEST_INPUT_ACTION, "value": "Tell me about ABC"})
    )

    assert interaction is not None and interaction.text == "Tell me about ABC"


def test_quick_action_template_comes_from_server_not_payload():
    interaction = parse_home_interaction(
        home_payload({"action_id": f"{QUICK_ACTION_PREFIX}find_company", "value": "<evil>"})
    )

    assert interaction is not None
    assert interaction.kind == "quick_action"
    assert interaction.template == "Tell me about the company "


@pytest.mark.parametrize(
    "payload",
    [
        home_payload({"action_id": SUBMIT_ACTION}, view_type="modal"),
        home_payload({"action_id": "something_else"}),
        home_payload({"action_id": f"{QUICK_ACTION_PREFIX}unknown"}),
        home_payload({"action_id": CONFIRM_ACTION, "value": "not-an-action-id"}),
        {"type": "view_submission"},
        "not a dict",
    ],
)
def test_untrusted_or_foreign_interactions_are_ignored(payload):
    assert parse_home_interaction(payload) is None


def test_confirm_button_carries_validated_action_id():
    interaction = parse_home_interaction(
        home_payload({"action_id": CONFIRM_ACTION, "value": ACTION_ID})
    )

    assert interaction is not None
    assert interaction.kind == "confirm"
    assert interaction.action_id == ACTION_ID


def test_app_home_opened_is_parsed_only_for_the_home_tab():
    event = {
        "type": "event_callback",
        "team_id": "T1",
        "event": {"type": "app_home_opened", "user": "U1", "tab": "home"},
    }
    opened = parse_app_home_opened(event)
    assert opened is not None and (opened.team_id, opened.user_id) == ("T1", "U1")

    event["event"]["tab"] = "messages"
    assert parse_app_home_opened(event) is None


# --- Endpoints ----------------------------------------------------------------------


class RecordingClient:
    def __init__(self) -> None:
        self.views: list[tuple[str, dict]] = []
        self.messages: list[tuple[str, str]] = []

    async def publish_home_view(self, user_id, view):
        self.views.append((user_id, view))

    async def post_message(self, channel, text):
        self.messages.append((channel, text))


class FakeSafety:
    def __init__(self, recent: list[RecentAction] | None = None) -> None:
        self.recent = recent or []
        self.lookups: list[tuple[str, str]] = []

    async def recent_actions(self, *, tenant_id, actor_id, limit=5):
        self.lookups.append((tenant_id, actor_id))
        return self.recent


class FakeConversationHistory:
    def __init__(self):
        self.exchanges = []
        self.saved = []
        self.owned_lookups = []

    async def latest(self, *, tenant_id, team_id, user_id, limit=5):
        selected = [
            row for row in self.exchanges
            if (row.tenant_id, row.team_id, row.user_id) == (tenant_id, team_id, user_id)
        ]
        return selected[-limit:]

    async def get_owned(self, *, tenant_id, team_id, user_id, delivery_id):
        scope = (tenant_id, team_id, user_id, delivery_id)
        self.owned_lookups.append(scope)
        return next((
            row for row in self.exchanges
            if (row.tenant_id, row.team_id, row.user_id, row.delivery_id) == scope
        ), None)

    async def save_completed(
        self, *, tenant_id, team_id, user_id, delivery_id, question, response
    ):
        self.saved.append((tenant_id, team_id, user_id, delivery_id, question, response.status))
        if not delivery_id or any(row.delivery_id == delivery_id for row in self.exchanges):
            return False
        self.exchanges.append(SimpleNamespace(
            tenant_id=tenant_id, team_id=team_id, user_id=user_id, delivery_id=delivery_id,
            question=question, response_status=response.status, response_text=response.text,
            render_data={"result": response.result or {}, "cards": response.cards},
        ))
        return True


class RecordingAgent:
    def __init__(self, response: AgentResponse | None = None) -> None:
        self.requests = []
        self.response = response

    async def respond(self, request):
        self.requests.append(request)
        return self.response or AgentResponse(
            status="ok",
            text="Contact Angel John was created successfully in HubSpot.",
            request_id=request.request_id,
            result={"kind": "contact_created", "contact_id": "42", "name": "Angel John"},
        )


class AllowAuthorization:
    async def is_authorized(self, **kwargs):
        return True


def app_with(
    agent,
    client,
    safety,
    team_map='{"T1":"tenant-a"}',
    history=None,
    web_app_base_url=None,
):
    app = create_app(
        Settings(
            slack_signing_secret="signing",
            slack_team_tenant_map=team_map,
            web_app_base_url=web_app_base_url,
        )
    )
    app.dependency_overrides[get_agent] = lambda: agent
    app.dependency_overrides[get_slack_client] = lambda: client
    app.dependency_overrides[get_slack_authorization] = lambda: AllowAuthorization()
    app.dependency_overrides[get_action_safety] = lambda: safety
    app.dependency_overrides[get_conversation_history] = (
        lambda: history if history is not None else FakeConversationHistory()
    )
    return app


def signed(body: bytes, secret: str = "signing") -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256)
    return {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={signature.hexdigest()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def post_interaction(app, payload: dict, headers: dict[str, str] | None = None):
    body = urlencode({"payload": json.dumps(payload)}).encode()
    with TestClient(app) as test_client:
        return test_client.post(
            "/api/v1/slack/interactions", content=body, headers=headers or signed(body)
        )


def test_app_home_opened_publishes_home_for_mapped_workspace():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()
    history = FakeConversationHistory()
    app = app_with(
        agent,
        client,
        safety,
        history=history,
        web_app_base_url="https://assistant.example.test",
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {"type": "app_home_opened", "user": "U1", "tab": "home"},
        }
    ).encode()

    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/slack/events", content=body, headers=signed(body)
        )

    assert response.status_code == 200
    assert [user for user, _ in client.views] == ["U1"]
    assert client.views[0][1]["type"] == "home"
    assert client.views[0][1]["blocks"][0]["image_url"] == (
        "https://assistant.example.test/assistant/static/hubspot-ai-welcome.png"
    )
    assert safety.lookups == [("tenant-a", "U1")]
    assert "Your questions and answers will appear here." in all_text(client.views[0][1]["blocks"])
    assert agent.requests == []
    assert client.messages == []


def test_welcome_banner_png_is_served_from_the_existing_static_mount():
    app = create_app(Settings())

    with TestClient(app) as test_client:
        response = test_client.get("/assistant/static/hubspot-ai-welcome.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_home_request_goes_through_the_existing_agent_and_renders_result():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()
    history = FakeConversationHistory()
    app = app_with(agent, client, safety, history=history)

    response = post_interaction(
        app,
        home_payload(
            {"action_id": SUBMIT_ACTION},
            request_text="Create a contact named Angel John, email angel@example.com",
        ),
    )

    assert response.status_code == 200
    [request] = agent.requests
    assert request.tenant_id == "tenant-a"
    assert request.actor_id == "U1"
    assert request.message == "Create a contact named Angel John, email angel@example.com"
    assert request.channel_id == "apphome:U1"
    assert request.message_ts == "1712345678.000100"
    assert len(client.views) == 2
    assert "Working..." in all_text(client.views[0][1]["blocks"])
    assert "*Contact created*" in all_text(client.views[1][1]["blocks"])
    assert history.saved[0][:5] == (
        "tenant-a",
        "T1",
        "U1",
        "1712345678.000100",
        "Create a contact named Angel John, email angel@example.com",
    )
    assert client.messages == []


def test_home_submit_publishes_progress_stages_and_then_the_final_response():
    class ProgressAgent(RecordingAgent):
        async def respond(self, request, on_progress=None):
            self.requests.append(request)
            assert on_progress is not None
            await on_progress("Understanding your request")
            await on_progress("Understanding your request")
            await on_progress("Retrieving CRM data")
            return AgentResponse(
                status="ok",
                text="The requested CRM records are ready.",
                request_id=request.request_id,
            )

    client, safety, agent = RecordingClient(), FakeSafety(), ProgressAgent()
    history = FakeConversationHistory()

    response = post_interaction(
        app_with(agent, client, safety, history=history),
        home_payload(
            {"action_id": SUBMIT_ACTION},
            request_text="Show the latest contacts",
        ),
    )

    assert response.status_code == 200
    assert len(client.views) == 4  # initial, two distinct stages, final
    initial_text = all_text(client.views[0][1]["blocks"])
    understanding_text = all_text(client.views[1][1]["blocks"])
    retrieving_text = all_text(client.views[2][1]["blocks"])
    final_text = all_text(client.views[3][1]["blocks"])
    assert "Working..." in initial_text
    assert "Understanding your request..." in understanding_text
    assert "Retrieving CRM data..." in retrieving_text
    assert "The requested CRM records are ready." in final_text
    assert "Retrieving CRM data..." not in final_text
    for _, view in client.views[1:3]:
        assert blocks_of_type(view, "input")
        assert any(
            element.get("action_id") == SUBMIT_ACTION
            for block in blocks_of_type(view, "actions")
            for element in block.get("elements", [])
        )
    assert len(history.saved) == 1


def test_home_progress_publish_failure_does_not_block_final_response_or_retry_stages():
    class FailingProgressClient(RecordingClient):
        def __init__(self):
            super().__init__()
            self.publish_attempts = 0

        async def publish_home_view(self, user_id, view):
            self.publish_attempts += 1
            if self.publish_attempts == 2:
                raise IntegrationError("Slack view publish failed")
            await super().publish_home_view(user_id, view)

    class ProgressAgent(RecordingAgent):
        async def respond(self, request, on_progress=None):
            self.requests.append(request)
            assert on_progress is not None
            await on_progress("Understanding your request")
            await on_progress("Understanding your request")
            await on_progress("Retrieving CRM data")
            return AgentResponse(
                status="ok",
                text="The final answer is still available.",
                request_id=request.request_id,
            )

    client, agent = FailingProgressClient(), ProgressAgent()
    response = post_interaction(
        app_with(agent, client, FakeSafety()),
        home_payload(
            {"action_id": SUBMIT_ACTION},
            request_text="Show the latest contacts",
        ),
    )

    assert response.status_code == 200
    assert client.publish_attempts == 3  # initial, failed stage, final
    assert len(client.views) == 2
    assert "Working..." in all_text(client.views[0][1]["blocks"])
    assert "The final answer is still available." in all_text(client.views[-1][1]["blocks"])


def test_home_submit_passes_same_user_workspace_history_as_follow_up_context():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()
    history = FakeConversationHistory()
    history.exchanges.append(
        SimpleNamespace(
            tenant_id="tenant-a",
            team_id="T1",
            user_id="U1",
            delivery_id="prior-delivery",
            question="Show closed won deals",
            response_status="ok",
            response_text="There are 3 closed won deals.",
            render_data={},
        )
    )
    post_interaction(
        app_with(agent, client, safety, history=history),
        home_payload({"action_id": SUBMIT_ACTION}, request_text="what about last quarter?"),
    )

    [request] = agent.requests
    assert request.conversation_context == (
        "User: Show closed won deals\nAssistant: There are 3 closed won deals."
    )


def test_home_confirm_button_sends_confirmation_through_the_agent():
    client, safety = RecordingClient(), FakeSafety()
    agent = RecordingAgent(
        AgentResponse(status="ok", text="Contact deleted successfully in HubSpot.", request_id="r")
    )

    post_interaction(
        app_with(agent, client, safety),
        home_payload({"action_id": CONFIRM_ACTION, "value": ACTION_ID}),
    )

    assert [request.message for request in agent.requests] == [f"confirm {ACTION_ID}"]
    assert "Contact deleted successfully" in all_text(client.views[-1][1]["blocks"])


def test_quick_action_only_prefills_input_without_calling_the_agent():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()

    post_interaction(
        app_with(agent, client, safety),
        home_payload({"action_id": f"{QUICK_ACTION_PREFIX}create_contact"}),
    )

    assert agent.requests == []
    [request_input] = blocks_of_type(client.views[0][1], "input")
    assert request_input["element"]["initial_value"] == "Create a contact named "


def test_empty_home_request_asks_for_input_without_calling_the_agent():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()

    post_interaction(
        app_with(agent, client, safety), home_payload({"action_id": SUBMIT_ACTION})
    )

    assert agent.requests == []
    assert "More information needed" in all_text(client.views[0][1]["blocks"])


def test_agent_failure_is_rendered_as_an_error_state():
    class FailingAgent(RecordingAgent):
        async def respond(self, request):
            raise RuntimeError("boom")

    client = RecordingClient()
    post_interaction(
        app_with(FailingAgent(), client, FakeSafety()),
        home_payload({"action_id": SUBMIT_ACTION}, request_text="Tell me about ABC"),
    )

    final = all_text(client.views[-1][1]["blocks"])
    assert "Unable to complete request" in final
    assert "boom" not in final


def test_interactions_require_a_valid_slack_signature():
    client, agent = RecordingClient(), RecordingAgent()
    app = app_with(agent, client, FakeSafety())

    response = post_interaction(
        app,
        home_payload({"action_id": SUBMIT_ACTION}),
        headers={
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=" + "0" * 64,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    assert response.status_code == 401
    assert agent.requests == [] and client.views == []


def test_interactions_from_unmapped_workspaces_are_ignored():
    client, agent = RecordingClient(), RecordingAgent()

    response = post_interaction(
        app_with(agent, client, FakeSafety(), team_map='{"T999":"tenant-z"}'),
        home_payload({"action_id": SUBMIT_ACTION}, request_text="Create Angel John"),
    )

    assert response.status_code == 200
    assert agent.requests == [] and client.views == []


def test_channel_mention_flow_still_posts_a_message_and_not_a_home_view():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()
    body = json.dumps(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "ts": "1.0",
                "text": "<@BOT> Create a contact named Angel John",
            },
        }
    ).encode()

    with TestClient(app_with(agent, client, safety)) as test_client:
        test_client.post("/api/v1/slack/events", content=body, headers=signed(body))

    assert client.messages == [
        ("C1", "Contact Angel John was created successfully in HubSpot.")
    ]
    assert client.views == []
    assert agent.requests[0].channel_id == "C1"


# --- Slack Web API adapter ----------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_home_view_calls_views_publish():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["json"] = json.loads(request.read())
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await SlackWebApiClient(Settings(slack_bot_token="xoxb-test"), http).publish_home_view(
            "U1", {"type": "home", "blocks": []}
        )

    assert captured == {
        "url": "https://slack.com/api/views.publish",
        "authorization": "Bearer xoxb-test",
        "json": {"user_id": "U1", "view": {"type": "home", "blocks": []}},
    }


@pytest.mark.asyncio
async def test_publish_home_view_surfaces_slack_error_code():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": False, "error": "invalid_blocks"})
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(IntegrationError, match="invalid_blocks"):
            await SlackWebApiClient(
                Settings(slack_bot_token="xoxb-test"), http
            ).publish_home_view("U1", {"type": "home", "blocks": []})


# --- Companies, deals and multi-step results ------------------------------------------


def test_titled_crm_results_render_their_title():
    blocks = render_response_blocks(
        AgentResponse(
            status="ok",
            text="✅ Company created: TechNova\n✅ Contact created: John Smith",
            request_id="r",
            result={"kind": "crm_records", "title": "CRM Updated"},
        )
    )

    assert blocks[0]["text"]["text"] == "*CRM Updated*"
    assert "Company created: TechNova" in blocks[1]["text"]["text"]


def test_partial_completion_is_labelled():
    blocks = render_response_blocks(
        AgentResponse(status="partial", text="✅ Company created\n❌ Link failed", request_id="r")
    )

    assert blocks[0]["text"]["text"] == "*Partially completed*"


def test_ambiguous_records_ask_for_more_information():
    blocks = render_response_blocks(
        AgentResponse(status="company_ambiguous", text="Which one?", request_id="r")
    )

    assert blocks[0]["text"]["text"] == "*More information needed*"


def test_company_update_confirmation_names_the_company():
    blocks = render_response_blocks(
        AgentResponse(
            status="pending_confirmation",
            text="I found a request to update this HubSpot company:",
            request_id="r",
            result={
                "kind": "pending_confirmation",
                "action_id": ACTION_ID,
                "action_type": "update_company",
                "record_label": "company ABC Technologies",
            },
        )
    )

    button = next(block for block in blocks if block["type"] == "actions")["elements"][0]
    assert button["text"]["text"] == "Confirm Update"
    assert button["style"] == "primary"
    assert button["confirm"]["title"]["text"] == "Update company?"
    assert button["confirm"]["text"]["text"] == (
        "This will update HubSpot company ABC Technologies."
    )


def test_recent_activity_covers_plans_and_record_updates():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    recent = [
        RecentAction(
            "crm_plan", "completed", {"request": "Create TechNova and add John"}, now, now
        ),
        RecentAction("crm_plan", "reconciliation_required", {"request": "Big request"}, now, now),
        RecentAction(
            "update_deal", "pending", {"deal_name": "Renewal"}, now, now + timedelta(minutes=3)
        ),
        RecentAction("update_company", "completed", {"company_name": "TechNova"}, now, now),
    ]

    text = all_text(build_home_view(recent=recent, now=now)["blocks"])

    assert "Completed: Create TechNova and add John" in text
    assert "Partially completed: Big request" in text
    assert "Awaiting confirmation: Update deal Renewal" in text
    assert "Completed: Updated company TechNova" in text


def test_existing_record_outcome_is_informational_not_an_error():
    blocks = render_response_blocks(
        AgentResponse(
            status="already_exists",
            text=(
                "Test AI Company already exists in HubSpot, so I used the existing company "
                "record. No duplicate was created."
            ),
            request_id="r",
            result={"kind": "existing_record", "title": "Company already exists"},
        )
    )

    assert blocks[0]["text"]["text"] == "*Company already exists*"
    assert "No duplicate was created." in blocks[1]["text"]["text"]
    assert "Unable to complete request" not in all_text(blocks)


def test_contact_flow_duplicate_error_is_still_an_error():
    blocks = render_response_blocks(
        AgentResponse(
            status="duplicate",
            text="A contact with the email a@b.co already exists in HubSpot.",
            request_id="r",
        )
    )

    assert blocks[0]["text"]["text"] == "*Unable to complete request*"


def test_home_keeps_assistant_response_together_before_input_and_activity():
    response = AgentResponse(
        status="ok",
        text="Found one deal.",
        request_id="r",
        result={
            "kind": "deals",
            "table": {
                "columns": [{"key": "dealname", "label": "Deal name"}],
                "rows": [{"dealname": "Northwind renewal", "view_url": "https://app.hubspot.com/deals/123"}],
            },
        },
    )
    activity = RecentAction(
        "create_contact", "completed", {"firstname": "Ari"},
        datetime(2026, 10, 18, tzinfo=UTC), datetime(2026, 10, 19, tzinfo=UTC),
    )
    blocks = build_home_view(request_text="Show me deals", response=response, recent=[activity])["blocks"]
    user_index = next(
        i for i, block in enumerate(blocks)
        if block["type"] == "rich_text" and block["elements"][0]["type"] == "rich_text_quote"
    )
    assistant_index = next(
        i for i, block in enumerate(blocks)
        if block["type"] == "rich_text" and "HubSpot AI Assistant" in all_text([block])
    )
    input_index = next(i for i, block in enumerate(blocks) if block["type"] == "input")
    send_index = next(
        i for i, block in enumerate(blocks)
        if block["type"] == "actions" and any(e["action_id"] == SUBMIT_ACTION for e in block["elements"])
    )
    activity_index = next(i for i, block in enumerate(blocks) if "Recent CRM Activity" in all_text([block]))
    response_group = blocks[assistant_index:input_index]
    assert user_index < assistant_index < input_index < send_index < activity_index
    assert "Found one deal." in all_text(response_group)
    assert "Northwind renewal" in all_text(response_group)
    assert "View in HubSpot" in all_text(response_group)
    assert all("Recent CRM Activity" not in all_text([block]) for block in response_group)


def test_view_full_response_loads_only_the_authorized_saved_exchange():
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()
    history = FakeConversationHistory()
    answer = "Complete saved response " + ("details. " * 900)
    history.exchanges.append(SimpleNamespace(
        tenant_id="tenant-a", team_id="T1", user_id="U1",
        delivery_id="1712345678.0001", question="List my companies",
        response_status="ok", response_text=answer, render_data={"result": {}, "cards": []},
    ))
    app = app_with(agent, client, safety, history=history)

    response = post_interaction(
        app,
        home_payload({
            "action_id": VIEW_FULL_RESPONSE_ACTION,
            "value": "1712345678.0001:0",
        }),
    )

    assert response.status_code == 200
    assert history.owned_lookups == [("tenant-a", "T1", "U1", "1712345678.0001")]
    assert agent.requests == []
    assert len(client.views) == 1
    rendered = all_text(client.views[0][1]["blocks"])
    assert "Full response" in rendered and "Complete saved response" in rendered
    assert "Next" in rendered


@pytest.mark.parametrize(
    ("stored_tenant", "stored_user"),
    [("tenant-a", "U2"), ("tenant-b", "U1")],
)
def test_view_full_response_rejects_other_user_or_tenant(stored_tenant, stored_user):
    client, safety, agent = RecordingClient(), FakeSafety(), RecordingAgent()
    history = FakeConversationHistory()
    history.exchanges.append(SimpleNamespace(
        tenant_id=stored_tenant, team_id="T1", user_id=stored_user,
        delivery_id="1712345678.0001", question="Private question",
        response_status="ok", response_text="Private full answer", render_data={},
    ))
    app = app_with(agent, client, safety, history=history)

    response = post_interaction(
        app,
        home_payload({
            "action_id": VIEW_FULL_RESPONSE_ACTION,
            "value": "1712345678.0001:0",
        }),
    )

    assert response.status_code == 200
    assert client.views == []
    assert agent.requests == []
