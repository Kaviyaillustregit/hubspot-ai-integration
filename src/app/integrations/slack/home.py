import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from app.agent.schemas import AgentResponse
from app.integrations.slack.response_formatting import (
    escape_slack_text,
    format_property_value,
    format_slack_response,
    normalize_slack_display_text,
)
from app.integrations.slack.welcome_banner import build_welcome_banner_block

REQUEST_INPUT_ACTION = "home_request_input"
SUBMIT_ACTION = "home_submit"
CONFIRM_ACTION = "home_confirm"
VIEW_FULL_RESPONSE_ACTION = "home_view_full_response"
QUICK_ACTION_PREFIX = "home_quick:"

_QUICK_ACTIONS = {
    "create_contact": "Create a contact named ",
    "find_contact": "Find a contact named ",
    "find_company": "Tell me about the company ",
    "account_overview": "Give me an account overview of ",
    "search_crm": "Search HubSpot for ",
}
_ACTION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_MAX_HOME_TABLE_BLOCKS = 70
_MAX_SECTION_TEXT = 3000
_SLACK_LINK_TEXT = re.compile(r"<([^<>|]+)\|([^<>]*)>")
_HISTORY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HISTORY_TOKEN_PATTERN = re.compile(r"<[^<>\n]*>|https?://[^\s<>]+|\s+|[^\s]+", re.IGNORECASE)
_HISTORY_PAGE_BLOCK_CHARS = 3000

Block = dict[str, Any]


@dataclass(frozen=True)
class HomeInteraction:
    team_id: str
    user_id: str
    kind: str
    text: str = ""
    template: str = ""
    action_id: str = ""
    action_ts: str = ""
    history_delivery_id: str = ""
    history_page: int = 0


def parse_home_interaction(payload: object) -> HomeInteraction | None:
    if not isinstance(payload, dict) or payload.get("type") != "block_actions":
        return None
    view = payload.get("view")
    user = payload.get("user")
    team = payload.get("team")
    if not isinstance(view, dict) or view.get("type") != "home" or not isinstance(user, dict):
        return None

    user_id = user.get("id")
    team_id = team.get("id") if isinstance(team, dict) else None
    team_id = team_id or user.get("team_id")
    actions = payload.get("actions")
    if (
        not isinstance(user_id, str)
        or not user_id
        or not isinstance(team_id, str)
        or not team_id
        or not isinstance(actions, list)
        or not actions
        or not isinstance(actions[0], dict)
    ):
        return None

    action = actions[0]
    action_id = action.get("action_id")
    if not isinstance(action_id, str):
        return None
    action_ts = action.get("action_ts")
    action_ts = action_ts if isinstance(action_ts, str) else ""

    if action_id == REQUEST_INPUT_ACTION:
        value = action.get("value")
        return HomeInteraction(
            team_id,
            user_id,
            "input",
            value.strip() if isinstance(value, str) else "",
            action_ts=action_ts,
        )
    if action_id == SUBMIT_ACTION:
        return HomeInteraction(
            team_id,
            user_id,
            "submit",
            _submitted_text(view),
            action_ts=action_ts,
        )
    if action_id.startswith(QUICK_ACTION_PREFIX):
        template = _QUICK_ACTIONS.get(action_id.removeprefix(QUICK_ACTION_PREFIX))
        if template is None:
            return None
        return HomeInteraction(
            team_id, user_id, "quick_action", template=template, action_ts=action_ts
        )
    if action_id == CONFIRM_ACTION:
        confirmation_id = action.get("value")
        if not isinstance(confirmation_id, str) or not _ACTION_ID_PATTERN.fullmatch(
            confirmation_id
        ):
            return None
        return HomeInteraction(
            team_id,
            user_id,
            "confirm",
            action_id=confirmation_id,
            action_ts=action_ts,
        )
    if action_id == VIEW_FULL_RESPONSE_ACTION:
        value = action.get("value")
        if not isinstance(value, str) or ":" not in value:
            return None
        delivery_id, page_text = value.rsplit(":", 1)
        if not _HISTORY_ID_PATTERN.fullmatch(delivery_id) or not page_text.isdigit():
            return None
        page = int(page_text)
        if page > 10_000:
            return None
        return HomeInteraction(
            team_id,
            user_id,
            "view_full_response",
            action_ts=action_ts,
            history_delivery_id=delivery_id,
            history_page=page,
        )
    return None


def _submitted_text(view: dict[str, Any]) -> str:
    blocks = view.get("blocks")
    state = view.get("state")
    values = state.get("values") if isinstance(state, dict) else None
    if not isinstance(blocks, list) or not isinstance(values, dict):
        return ""

    # Home state can contain stale values from earlier quick-action inputs. Only read
    # the input block currently present in the view that generated this interaction.
    current_blocks = {
        block.get("block_id")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "input"
    }
    for block_id in current_blocks:
        block_values = values.get(block_id)
        entry = block_values.get(REQUEST_INPUT_ACTION) if isinstance(block_values, dict) else None
        value = entry.get("value") if isinstance(entry, dict) else None
        if isinstance(value, str):
            return value.strip()
    return ""


def build_home_view(
    *,
    recent: list[Any] | None = None,
    draft: str | None = None,
    request_text: str | None = None,
    response: AgentResponse | None = None,
    working: bool = False,
    progress_status: str | None = None,
    recent_unavailable: bool = False,
    history: list[Any] | None = None,
    history_detail: Any | None = None,
    history_page: int = 0,
    active_delivery_id: str | None = None,
    banner_image_url: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    blocks: list[Block] = []
    working_status = (
        f":hourglass_flowing_sand: *{escape_slack_text(progress_status)}...*"
        if progress_status
        else "Working..."
    )
    banner = build_welcome_banner_block(banner_image_url)
    if banner is not None:
        blocks.append(banner)
    blocks.extend(
        [
            {"type": "header", "text": _plain("HubSpot AI Assistant")},
            _section(
                "Ask about deals and pipeline, or manage contacts, companies, and deals."
            ),
            {
                "type": "actions",
                "block_id": "home_quick_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": f"{QUICK_ACTION_PREFIX}{key}",
                        "text": _plain(_quick_action_label(key)),
                        "value": template,
                    }
                    for key, template in _QUICK_ACTIONS.items()
                ],
            },
        ]
    )

    if history is not None:
        blocks.append(_section("*Conversation History*"))
        stored_history = list(history[-5:])
        live_in_history = bool(
            active_delivery_id
            and stored_history
            and stored_history[-1].delivery_id == active_delivery_id
        )
        displayed_history = stored_history[:-1] if live_in_history else stored_history
        if response is None and stored_history and not live_in_history:
            # The newest stored response is rendered below with its saved CRM
            # data, so keep it out of the compact transcript to avoid a duplicate.
            displayed_history = displayed_history[:-1]
        if response is not None and not live_in_history:
            displayed_history = displayed_history[-4:]
        if not stored_history and response is None and not working:
            blocks.append(_context("Your questions and answers will appear here."))
        for exchange in displayed_history:
            blocks.extend(_user_request_blocks(str(exchange.question)))
            blocks.extend(_history_preview_blocks(exchange))
        if history_detail is not None:
            blocks.extend(_full_history_response_blocks(history_detail, history_page))
        elif request_text is not None and (working or response is not None):
            blocks.extend(_user_request_blocks(request_text))
            if working:
                blocks.extend(
                    [_rich_text_heading("HubSpot AI Assistant"), _section(working_status)]
                )
            elif response is not None:
                blocks.extend(
                    [_rich_text_heading("HubSpot AI Assistant"), *render_response_blocks(response)]
                )
        elif response is not None:
            blocks.extend(
                [_rich_text_heading("HubSpot AI Assistant"), *render_response_blocks(response)]
            )
        elif stored_history and not live_in_history:
            exchange = stored_history[-1]
            blocks.extend(_user_request_blocks(str(exchange.question)))
            blocks.extend(_history_preview_blocks(exchange))
    else:
        if request_text is not None:
            blocks.extend(_user_request_blocks(request_text))
        if working:
            blocks.extend(
                [
                    _rich_text_heading("HubSpot AI Assistant"),
                    _section(working_status),
                ]
            )
        elif response is not None:
            blocks.extend(
                [
                    _rich_text_heading("HubSpot AI Assistant"),
                    *render_response_blocks(response),
                ]
            )

    # Reserve room for input, Send, and CRM activity before adding conversation.
    activity = _activity_blocks(recent, recent_unavailable, now or datetime.now(UTC))
    fixed = 2 + len(activity)
    if len(blocks) + fixed > 100:
        blocks = blocks[: 100 - fixed]

    blocks.extend(
        [
            {
                "type": "input",
                "block_id": f"home_request:{uuid4().hex[:12]}",
                "label": _plain("Message HubSpot AI"),
                "dispatch_action": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": REQUEST_INPUT_ACTION,
                    "placeholder": _plain(
                        "Ask about deals, contacts, companies, or your pipeline..."
                    ),
                    **({"initial_value": draft} if draft is not None else {}),
                },
            },
            {
                "type": "actions",
                "block_id": "home_submit_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": SUBMIT_ACTION,
                        "text": _plain("Send"),
                        "style": "primary",
                        "value": "send",
                    }
                ],
            },
        ]
    )

    blocks.extend(activity)
    return {"type": "home", "blocks": blocks[:100]}


def _compact_assistant_response(text: str) -> list[Block]:
    response = AgentResponse(
        status="ok",
        text=text,
        request_id="conversation-history",
    )
    formatted = format_slack_response(
        response,
        surface="home",
        renderer=lambda _: [_section(chunk) for chunk in _split_text(text)],
    )
    return [
        _rich_text_heading("HubSpot AI Assistant"),
        *formatted.messages[0],
    ]


def _history_preview_blocks(exchange: Any) -> list[Block]:
    """Render a short preview and an opaque, user-scoped history reference."""
    original = str(exchange.response_text)
    normalized = normalize_slack_display_text(original)
    result = (
        exchange.render_data.get("result", {})
        if isinstance(exchange.render_data, dict)
        else {}
    )
    table = result.get("table") if isinstance(result, dict) else None
    lines: list[str]
    shortened = False
    rows = table.get("rows") if isinstance(table, dict) else None
    columns = table.get("columns") if isinstance(table, dict) else None
    plan = result.get("plan") if isinstance(result, dict) else None
    grouped = bool(
        isinstance(table, dict) and table.get("groups")
        or isinstance(plan, dict) and (plan.get("group_by") or plan.get("aggregations"))
    )
    if isinstance(rows, list) and rows and not grouped:
        labels = {
            column.get("key"): column.get("label", column.get("key"))
            for column in columns or []
            if isinstance(column, dict) and isinstance(column.get("key"), str)
        }
        count_label = "record" if len(rows) == 1 else "records"
        lines = [f"{len(rows)} {count_label} in the saved result."]
        for row in rows[:2]:
            if not isinstance(row, dict):
                continue
            all_values = [
                f"{html.escape(str(labels.get(key, key)), quote=False)}: "
                f"{html.escape(format_property_value(key, value), quote=False)}"
                for key, value in row.items()
                if key != "view_url" and value not in (None, "")
            ]
            values = all_values[:2]
            link = _slack_hubspot_link(row.get("view_url"))
            record = " · ".join(values) or "Record"
            if link.startswith("<https://"):
                record += f" · {link}"
            lines.append(record)
            shortened = shortened or len(all_values) > 2
        shortened = shortened or len(rows) > 2 or len(normalized.splitlines()) > 4
    elif isinstance(rows, list) and rows and grouped:
        labels = {
            column.get("key"): column.get("label", column.get("key"))
            for column in columns or []
            if isinstance(column, dict) and isinstance(column.get("key"), str)
        }
        lines = []
        for row in rows[:3]:
            if not isinstance(row, dict):
                continue
            metrics = [
                f"{html.escape(str(labels.get(key, key)), quote=False)}: "
                f"{html.escape(format_property_value(key, value), quote=False)}"
                for key, value in row.items()
                if value not in (None, "")
            ][:3]
            if metrics:
                lines.append(" · ".join(metrics))
        if not lines:
            lines = [_preview_line(line, 220) for line in normalized.splitlines()[:4]]
        shortened = len(rows) > 3 or len(normalized.splitlines()) > len(lines)
    else:
        source_lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        if not source_lines:
            source_lines = ["No response was returned."]
        lines = [_preview_line(line, 220) for line in source_lines[:4]]
        shortened = len(source_lines) > 4 or any(
            _preview_line(line, 220) != line for line in source_lines[:4]
        )
    lines = lines[:4]
    blocks = [_rich_text_heading("HubSpot AI Assistant"), _section("\n".join(lines))]
    delivery_id = getattr(exchange, "delivery_id", "")
    if shortened and isinstance(delivery_id, str) and _HISTORY_ID_PATTERN.fullmatch(delivery_id):
        blocks.append(_context("Preview shortened."))
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "action_id": VIEW_FULL_RESPONSE_ACTION,
                "text": _plain("View full response"),
                "value": f"{delivery_id}:0",
            }],
        })
    if getattr(exchange, "response_status", "") == "pending_confirmation":
        blocks.append(_context("This confirmation is no longer active."))
    return _format_history_blocks(original, blocks)


def _preview_line(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    kept: list[str] = []
    size = 0
    for token in _HISTORY_TOKEN_PATTERN.findall(text):
        if size + len(token) > limit - 1:
            break
        kept.append(token)
        size += len(token)
    return "".join(kept).rstrip() + "…"


def _full_history_response_blocks(exchange: Any, page: int) -> list[Block]:
    text = normalize_slack_display_text(str(exchange.response_text))
    chunks = _split_history_response(text)
    page_count = max(1, (len(chunks) + 1) // 2)
    selected_page = min(max(page, 0), page_count - 1)
    page_chunks = chunks[selected_page * 2 : selected_page * 2 + 2]
    blocks = [
        _header("Full response"),
        _section(f"*{escape_slack_text(str(exchange.question))}*"),
        *(_section(chunk) for chunk in page_chunks),
        _context(f"Page {selected_page + 1} of {page_count}"),
    ]
    elements = []
    delivery_id = str(exchange.delivery_id)
    if selected_page > 0:
        elements.append({
            "type": "button", "action_id": VIEW_FULL_RESPONSE_ACTION,
            "text": _plain("Previous"), "value": f"{delivery_id}:{selected_page - 1}",
        })
    if selected_page + 1 < page_count:
        elements.append({
            "type": "button", "action_id": VIEW_FULL_RESPONSE_ACTION,
            "text": _plain("Next"), "value": f"{delivery_id}:{selected_page + 1}",
        })
    if elements:
        blocks.append({"type": "actions", "elements": elements})
    return _format_history_blocks(text, blocks)


def _format_history_blocks(text: str, blocks: list[Block]) -> list[Block]:
    response = AgentResponse(
        status="ok",
        text=text,
        request_id="conversation-history",
    )
    formatted = format_slack_response(
        response,
        surface="home",
        renderer=lambda _: blocks,
    )
    return formatted.messages[0]


def _split_history_response(text: str) -> list[str]:
    """Split at whitespace boundaries, keeping Slack links and URLs intact."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for token in _HISTORY_TOKEN_PATTERN.findall(text):
        if length and length + len(token) > _HISTORY_PAGE_BLOCK_CHARS:
            chunks.append("".join(current))
            current, length = [], 0
        if len(token) > _HISTORY_PAGE_BLOCK_CHARS:
            # Slack cannot render an atomic token over the section limit. Keep the
            # failure visible and preserve all surrounding response text/pages.
            chunks.append("[A single link or text token exceeds Slack's display limit.]")
            continue
        current.append(token)
        length += len(token)
    if current or not chunks:
        chunks.append("".join(current))
    return chunks


def _activity_blocks(
    recent: list[Any] | None, recent_unavailable: bool, now: datetime
) -> list[Block]:
    if recent_unavailable:
        return [
            _section("*Recent CRM Activity*"),
            _context("Recent CRM activity is temporarily unavailable."),
        ]
    if recent:
        return _recent_activity_blocks(recent, now)
    return [_section("*Recent CRM Activity*"), _context("No CRM actions yet")]

def _user_request_blocks(request_text: str) -> list[Block]:
    return [
        _rich_text_heading("You"),
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_quote",
                    "elements": [{"type": "text", "text": request_text}],
                }
            ],
        },
    ]


def _rich_text_heading(text: str) -> Block:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [{"type": "text", "text": text, "style": {"bold": True}}],
            }
        ],
    }


def _quick_action_label(key: str) -> str:
    return {
        "create_contact": "Create contact",
        "find_contact": "Find contact",
        "find_company": "Find company",
        "account_overview": "Account overview",
        "search_crm": "Search CRM",
    }[key]


def render_response_blocks(response: AgentResponse) -> list[Block]:
    """Format App Home responses through the shared backend formatting panel."""
    formatted = format_slack_response(
        response,
        surface="home",
        renderer=_build_response_blocks,
    )
    return formatted.messages[0]


def _build_response_blocks(response: AgentResponse) -> list[Block]:
    result = response.result or {}
    kind = result.get("kind")

    if response.status == "pending_confirmation" or kind == "pending_confirmation":
        action_id = result.get("action_id")
        if isinstance(action_id, str) and _ACTION_ID_PATTERN.fullmatch(action_id):
            action_type = str(result.get("action_type", ""))
            record_label = result.get("record_label")
            label = str(record_label) if record_label else _confirmation_label(action_type)
            title = _confirmation_title(action_type)
            return [
                _section("*Confirmation needed*"),
                _section("Please confirm: " + html.escape(label, quote=False) + "."),
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": CONFIRM_ACTION,
                            "value": action_id,
                            "text": _plain(title),
                            "style": "danger" if action_type.startswith("delete_") else "primary",
                            "confirm": {
                                "title": _plain(_confirmation_question(action_type)),
                                "text": _plain(
                                    "This will "
                                    + _confirmation_description(action_type, label)
                                    + "."
                                ),
                                "confirm": _plain(action_type.partition("_")[0].capitalize()),
                                "deny": _plain("Cancel"),
                                "style": "danger"
                                if action_type.startswith("delete_")
                                else "primary",
                            },
                        }
                    ],
                },
                *_record_link_blocks(response),
            ]

    if response.status in {"missing_fields", "invalid_request", "company_ambiguous"}:
        heading = "*More information needed*"
    elif response.status == "already_exists":
        heading = "*" + html.escape(str(result.get("title") or "Already exists")) + "*"
    elif response.status in {"partial", "reconciliation_required"}:
        heading = "*Partially completed*"
    elif response.status == "ok" and kind == "contact_created":
        heading = "*Contact created*"
    elif response.status == "ok" and isinstance(result.get("title"), str):
        heading = "*" + html.escape(str(result["title"])) + "*"
    elif response.status == "ok":
        heading = "*Here's what I found*"
    else:
        heading = "*Unable to complete request*"

    detail_type = _record_detail_type(result)
    if detail_type:
        heading = {
            "deal": "💼 Deal details",
            "contact": "👤 Contact details",
            "company": "🏢 Company details",
        }[detail_type]

    chart_url = result.get("chart_url")
    table = _validated_table(result.get("table")) or _grouped_result_table(result)
    grouped = _is_grouped_result(result)
    if isinstance(chart_url, str) and chart_url:
        chart_title = result.get("chart_title")
        title = str(chart_title or heading)
        blocks = [_section(heading)]
        if kind == "crm_intelligence" and table is not None:
            summary = response.text.splitlines()[0] if response.text else ""
            if summary:
                blocks.append(_section(_escape_mrkdwn(summary)))
        else:
            blocks.extend(_section(chunk) for chunk in _split_text(response.text))
        blocks.append({
            "type": "image",
            "image_url": chart_url,
            "alt_text": title,
        })
        chart_summary = result.get("chart_summary")
        if isinstance(chart_summary, str) and chart_summary.strip():
            blocks.extend(_section(chunk) for chunk in _split_text(chart_summary))
        if table is not None:
            blocks.extend(
                _render_table_response(
                    "",
                    "",
                    table,
                    show_missing=True,
                    include_heading=False,
                    include_summary=False,
                    structured_rows=grouped,
                )
            )
        details_table = _validated_table(result.get("details_table"))
        if details_table is not None:
            blocks.append(_section("*Deal records*"))
            blocks.extend(_render_table_response(
                "", "", details_table, show_missing=True, include_heading=False,
                include_summary=False,
            ))
        notice = _details_pagination_notice(result)
        if notice:
            blocks.append(_context(notice))
        blocks.extend(_record_link_blocks(response))
        return blocks

    if table is not None:
        if detail_type and len(table["rows"]) == 1:
            return _render_record_card(
                detail_type,
                table["columns"],
                table["rows"][0],
                record_url=result.get("hubspot_url"),
            )
        if kind == "crm_intelligence":
            # Batch 2 also puts its complete table in response.text for channel
            # messages. App Home renders the structured table below, so keep
            # only the summary here to avoid showing every row twice.
            summary = response.text.splitlines()[0] if response.text else ""
            blocks = _render_table_response(
                heading, summary, table, show_missing=True, structured_rows=grouped
            )
            details_table = _validated_table(result.get("details_table"))
            if details_table is not None:
                blocks.append(_section("*Deal records*"))
                blocks.extend(_render_table_response(
                    "", "", details_table, show_missing=True, include_heading=False,
                    include_summary=False,
                ))
            notice = _details_pagination_notice(result)
            if notice:
                blocks.append(_context(notice))
            return blocks
        return _render_table_response(
            heading, response.text, table, structured_rows=grouped
        )

    text = response.text
    if kind == "contact_created":
        name = result.get("name")
        company = result.get("company_name")
        email = result.get("email")
        contact_id = result.get("contact_id")
        details = []
        if isinstance(name, str) and name:
            details.append(html.escape(name, quote=False))
        if isinstance(company, str) and company:
            details.append("Associated with " + html.escape(company, quote=False))
        summary = " was successfully added to HubSpot"
        if len(details) > 1:
            summary += ". " + details[1]
        if details:
            summary = details[0] + summary
        summary += "."
        rendered = [_section(heading), _section(summary)]
        metadata = []
        if isinstance(email, str) and email:
            metadata.append(html.escape(email, quote=False))
        if isinstance(contact_id, str) and contact_id:
            metadata.append("HubSpot ID `" + html.escape(contact_id, quote=False) + "`")
        if metadata:
            rendered.append(_context("  ·  ".join(metadata)))
        rendered.extend(_record_link_blocks(response))
        return rendered

    chunks = _split_text(text)
    return [
        _section(heading),
        *(_section(chunk) for chunk in chunks),
        *_record_link_blocks(response),
    ]


def _record_link_blocks(response: AgentResponse) -> list[Block]:
    result_url = (response.result or {}).get("hubspot_url")
    urls = [result_url] if isinstance(result_url, str) else []
    urls.extend(
        card["hubspot_url"]
        for card in response.cards
        if isinstance(card.get("hubspot_url"), str)
    )
    rendered: list[Block] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        link = _slack_hubspot_link(url)
        if link != "—":
            rendered.append(_section(link))
    return rendered


def _record_detail_type(result: dict[str, Any]) -> str | None:
    plan = result.get("plan")
    query = result.get("query")
    if not isinstance(query, str) and isinstance(plan, dict):
        query = plan.get("query")
    by_query = {
        "deal_details": "deal",
        "contact_details": "contact",
        "company_details": "company",
    }
    if query in by_query:
        return by_query[query]

    title = result.get("title")
    normalized_title = " ".join(title.casefold().split()) if isinstance(title, str) else ""
    by_title = {
        "deal details": "deal",
        "contact details": "contact",
        "company details": "company",
    }
    return by_title.get(normalized_title)


def _is_grouped_result(result: dict[str, Any]) -> bool:
    plan = result.get("plan")
    table = result.get("table")
    return bool(
        isinstance(plan, dict) and (plan.get("group_by") or plan.get("aggregations"))
    ) or (isinstance(table, dict) and bool(table.get("groups")))


def _details_pagination_notice(result: dict[str, Any]) -> str | None:
    summary = result.get("summary")
    if not isinstance(summary, dict) or not summary.get("details_has_more"):
        return None
    total = summary.get("details_total", "more")
    shown = summary.get("details_shown", 0)
    offset = summary.get("details_offset", 0)
    limit = max(1, int(summary.get("details_limit", 20) or 20))
    next_page = int(offset) // limit + 2
    return (
        f"Showing {shown} of {total} deal records. Ask for page {next_page} "
        "to view the next records."
    )


def _grouped_result_table(result: dict[str, Any]) -> dict[str, Any] | None:
    if not _is_grouped_result(result):
        return None
    rows = result.get("rows")
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        return None
    keys = list(dict.fromkeys(key for row in rows for key in row if isinstance(key, str)))
    labels = {
        "group": "Group",
        "count": "Count",
        "sum_amount": "Total amount",
        "total_amount": "Total amount",
        "avg_amount": "Average amount",
        "min_amount": "Minimum amount",
        "max_amount": "Maximum amount",
        "hubspot_owner_id": "Owner",
        "dealstage": "Deal stage",
        "pipeline": "Pipeline",
    }
    return {
        "columns": [
            {"key": key, "label": labels.get(key, key.replace("_", " ").title())}
            for key in keys
        ],
        "rows": rows,
    }


def _validated_table(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    columns = value.get("columns")
    rows = value.get("rows")
    if (
        not isinstance(columns, list)
        or not isinstance(rows, list)
        or not all(
            isinstance(column, dict)
            and isinstance(column.get("key"), str)
            and isinstance(column.get("label"), str)
            for column in columns
        )
        or not all(isinstance(row, dict) for row in rows)
    ):
        return None
    return {"columns": columns, "rows": rows}


def _render_table_response(
    heading: str,
    summary: str,
    table: dict[str, Any],
    *,
    show_missing: bool = False,
    include_heading: bool = True,
    include_summary: bool = True,
    structured_rows: bool = False,
) -> list[Block]:
    names = {
        str(row.get("dealname"))
        for row in table["rows"]
        if isinstance(row.get("dealname"), str) and row["dealname"]
    }
    if names:
        summary = "\n".join(
            line
            for line in summary.splitlines()
            if not any(name in line for name in names)
        )
    columns = list(table["columns"])
    rows = table["rows"]
    if any("view_url" in row for row in rows) and not any(
        column["key"] == "view_url" for column in columns
    ):
        columns.append({"key": "view_url", "label": "View in HubSpot"})

    row_urls = {
        row.get("view_url") for row in rows
        if isinstance(row.get("view_url"), str) and row.get("view_url")
    }
    summary = _remove_duplicate_hubspot_links(summary, row_urls)
    blocks: list[Block] = []
    if include_heading and heading:
        blocks.append(_section(heading))
    if include_summary and summary.strip():
        blocks.extend(_section(chunk) for chunk in _split_text(summary))

    if not rows:
        blocks.append(_context("No records to display."))
        return blocks

    rendered_rows = 0
    limit_reached = False
    for row in rows:
        row_blocks = _render_table_row(
            columns, row, show_missing=show_missing, structured=structured_rows
        )
        remaining_rows = len(rows) - rendered_rows - 1
        row_separator = int(remaining_rows > 0)
        reserve_notice = int(remaining_rows > 0)
        if (
            len(blocks) + len(row_blocks) + row_separator + reserve_notice
            > _MAX_HOME_TABLE_BLOCKS
        ):
            limit_reached = True
            break
        blocks.extend(row_blocks)
        rendered_rows += 1
        if remaining_rows:
            blocks.append({"type": "divider"})

    if limit_reached:
        blocks.append(
            _context(
                f"Showing {rendered_rows} of {len(rows)} records; "
                "the Slack Home display limit was reached."
            )
        )
    return blocks


def _render_table_row(
    columns: list[dict[str, str]],
    row: dict[str, Any],
    *,
    show_missing: bool = False,
    structured: bool = False,
) -> list[Block]:
    lines: list[str] = []
    grouped_fields: list[tuple[str, str, str]] = []
    for column in columns:
        key = column["key"]
        label = html.escape(column["label"], quote=False)
        raw_value = row.get(key)
        if key == "view_url":
            value = _slack_hubspot_link(raw_value)
            if value == "—":
                if show_missing:
                    lines.append("*HubSpot record:* Not available")
            else:
                lines.append(value)
                grouped_fields.append((key, "HubSpot record", value))
            continue
        if key.endswith("_links") and isinstance(raw_value, list):
            links = []
            for association in raw_value:
                if not isinstance(association, dict):
                    continue
                link = _slack_hubspot_link(association.get("url"))
                if link == "—":
                    continue
                name = html.escape(str(association.get("name") or "Record"), quote=False)
                links.append(link.replace("View in HubSpot", name))
            if links:
                value = ", ".join(links)
                lines.append(f"*{label}:* {value}")
                grouped_fields.append((key, html.unescape(label), value))
            continue
        else:
            if (
                key in {"owner", "hubspot_owner_id"}
                and isinstance(raw_value, str)
                and raw_value.isdigit()
            ):
                label = "Owner ID"
            value = html.escape(
                "Unassigned"
                if key in {"owner", "hubspot_owner_id"} and raw_value in (None, "")
                else "Not available"
                if show_missing and (
                    raw_value is None or raw_value == "" or raw_value == "—" or raw_value == "â€”"
                )
                else format_property_value(key, raw_value),
                quote=False,
            )
        if not value:
            continue
        lines.append(f"*{label}:* {value}")
        grouped_fields.append((key, html.unescape(label), value))

    if not lines:
        return [_section("No record details available.")]
    if structured:
        return _render_grouped_row(grouped_fields)
    content: list[str] = []
    current = ""
    for line in lines:
        for part in _split_escaped_text(line, _MAX_SECTION_TEXT):
            if current and len(current) + 1 + len(part) > _MAX_SECTION_TEXT:
                content.append(current)
                current = ""
            current = f"{current}\n{part}" if current else part
    if current:
        content.append(current)
    return [_section(chunk) for chunk in content]


def _render_record_card(
    record_type: str,
    columns: list[dict[str, str]],
    row: dict[str, Any],
    *,
    record_url: object = None,
) -> list[Block]:
    headings = {
        "deal": "\U0001F4BC Deal details",
        "contact": "\U0001F464 Contact details",
        "company": "\U0001F3E2 Company details",
    }
    blocks: list[Block] = [_header(headings[record_type]), {"type": "divider"}]
    rendered_row = dict(row)
    if not rendered_row.get("view_url") and isinstance(record_url, str):
        rendered_row["view_url"] = record_url
    row_columns = list(columns)
    if not any(column.get("key") == "view_url" for column in row_columns):
        url = rendered_row.get("view_url")
        if isinstance(url, str) and url:
            row_columns.append({"key": "view_url", "label": "HubSpot record"})
    lines: list[str] = []
    for column in row_columns:
        key = column.get("key", "")
        raw_value = rendered_row.get(key)
        if key == "view_url":
            link = _slack_hubspot_link(raw_value)
            if link == "—":
                continue
            lines.append(link)
            continue
        else:
            label = column.get("label", key.replace("_", " ").title())
            if (
                key in {"owner", "hubspot_owner_id"}
                and isinstance(raw_value, str)
                and raw_value.isdigit()
            ):
                label = "Owner ID"
            if key in {"owner", "hubspot_owner_id"} and raw_value in (None, ""):
                displayed = "Unassigned"
            elif raw_value is None or raw_value == "" or raw_value == "—" or raw_value == "â€”":
                displayed = "Not available"
            else:
                displayed = format_property_value(key, raw_value)
            value = html.escape(displayed, quote=False)

        lines.append(f"*{html.escape(label, quote=False)}:* {value}")
    if lines:
        blocks.extend(
            _section(chunk)
            for chunk in _split_escaped_text("\n".join(lines), _MAX_SECTION_TEXT)
        )
    if not lines:
        blocks.append(_context("No record details are available."))
    return blocks


def _render_grouped_row(fields: list[tuple[str, str, str]]) -> list[Block]:
    """Render group names and metrics as a compact single-column summary."""
    group_keys = {"group", "label", "dealstage", "pipeline", "stage"}
    metric_labels = {
        "sum_amount": "Total amount",
        "sum_amount_value": "Total amount",
        "total_amount": "Total amount",
        "total_amount_value": "Total amount",
        "avg_amount": "Average amount",
        "avg_amount_value": "Average amount",
        "min_amount": "Minimum amount",
        "min_amount_value": "Minimum amount",
        "max_amount": "Maximum amount",
        "max_amount_value": "Maximum amount",
    }
    group_items = [item for item in fields if item[0].casefold() in group_keys]
    group_lines: list[str] = []
    group_values: set[str] = set()
    for _key, label, value in group_items:
        normalized = value.strip().casefold()
        if normalized in group_values:
            continue
        group_values.add(normalized)
        if not group_lines:
            group_lines.append(f"*{value}*")
        else:
            group_lines.append(f"*{html.escape(label, quote=False)}:* {value}")

    metrics: list[str] = []
    seen_metrics: set[tuple[str, str]] = set()
    for key, label, value in fields:
        normalized_key = key.casefold()
        if normalized_key in group_keys:
            continue
        if normalized_key == "count":
            count = int(value) if value.isdigit() else None
            metrics.append(f"{value} {'deal' if count == 1 else 'deals'}")
            continue
        display_label = metric_labels.get(normalized_key, label)
        if normalized_key in metric_labels:
            dedupe_key = (display_label.casefold(), value.strip().casefold())
            if dedupe_key in seen_metrics:
                continue
            seen_metrics.add(dedupe_key)
        metrics.append(f"*{html.escape(display_label, quote=False)}:* {value}")

    if group_lines and metrics:
        group_lines[0] += ": " + " | ".join(metrics)
        lines = group_lines
    else:
        lines = group_lines + ([" | ".join(metrics)] if metrics else [])
    if not lines:
        return [_section("No record details available.")]
    return [
        _section(chunk)
        for chunk in _split_escaped_text("\n".join(lines), _MAX_SECTION_TEXT)
    ]


def _remove_duplicate_hubspot_links(summary: str, row_urls: set[object]) -> str:
    if not summary:
        return summary
    safe_urls = {url for url in row_urls if isinstance(url, str)}

    def replace(match: re.Match[str]) -> str:
        raw_url, label = match.groups()
        url = html.unescape(raw_url)
        try:
            hostname = (urlsplit(url).hostname or "").casefold()
        except ValueError:
            hostname = ""
        if url in safe_urls:
            return ""
        if "hubspot" in hostname or "hubspot" in label.casefold():
            return "HubSpot link unavailable"
        return match.group(0)

    cleaned = _SLACK_LINK_TEXT.sub(replace, summary)
    return "\n".join(line for line in cleaned.splitlines() if line.strip())


def _split_escaped_text(value: str, limit: int) -> list[str]:
    if not value:
        return [""]
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    tokens = re.findall(r"&(?:amp|lt|gt);|[\s\S]", value)
    for token in tokens:
        if current and current_length + len(token) > limit:
            chunks.append("".join(current))
            current = []
            current_length = 0
        current.append(token)
        current_length += len(token)
    chunks.append("".join(current))
    return chunks


def _slack_hubspot_link(value: object) -> str:
    if not isinstance(value, str):
        return "—"
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "app.hubspot.com"
        or parsed.username
        or parsed.password
        or len(value) + len("|View in HubSpot") + 3 > _MAX_SECTION_TEXT - 32
        or any(character in value for character in "<>|")
    ):
        return "—"
    return f"<{value}|View in HubSpot>"


def _split_text(text: str) -> list[str]:
    escaped = escape_slack_text(text)
    if not escaped:
        return ["No response was returned."]
    chunks: list[str] = []
    while len(escaped) > 3000:
        split_at = escaped.rfind("\n", 0, 3001)
        if split_at < 1:
            split_at = 3000
        else:
            split_at += 1
        chunks.append(escaped[:split_at])
        escaped = escaped[split_at:]
    chunks.append(escaped)
    return chunks


def _recent_activity_blocks(recent: list[Any], now: datetime) -> list[Block]:
    blocks = [_section("*Recent CRM Activity*")]
    for action in recent[:5]:
        payload = action.payload
        action_type = action.action_type
        status = action.status
        expires_at = action.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if status == "pending" and expires_at <= now:
            status = "expired"

        if action_type == "crm_plan":
            description = html.escape(str(payload.get("request") or "CRM request"), quote=False)
            if status == "completed":
                state = "Completed"
            elif status == "reconciliation_required":
                state = "Partially completed"
            elif status == "pending":
                state = "Awaiting confirmation"
            elif status == "expired":
                state = "Expired"
            else:
                state = "Not completed"
            description = f"{state}: {description}"
        else:
            entity, verb = _action_entity_verb(action_type)
            name = (
                payload.get("firstname") or payload.get("first_name") or payload.get("deal_name")
                or payload.get("company_name") or payload.get("contact_id") or "record"
            )
            if action_type == "create_contact":
                name = " ".join(
                    part
                    for part in (payload.get("firstname"), payload.get("lastname"))
                    if part
                ) or name
            safe_name = html.escape(str(name), quote=False)
            if status == "completed":
                completed_verb = {
                    "Create": "Created",
                    "Update": "Updated",
                    "Delete": "Deleted",
                }.get(verb, verb)
                description = f"Completed: {completed_verb} {entity} {safe_name}"
            elif status == "pending":
                description = f"Awaiting confirmation: {verb} {entity} {safe_name}"
            elif status == "expired":
                description = f"Expired: {verb} {entity} {safe_name}"
            elif status == "reconciliation_required":
                description = f"Partially completed: {verb} {entity} {safe_name}"
            else:
                description = f"Not completed: {verb} {entity} {safe_name}"

        created_at = action.created_at
        timestamp = int(
            (created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)).timestamp()
        )
        date = f"<!date^{timestamp}^{{date_short_pretty}}|{now:%Y-%m-%d}>"
        blocks.append(_section(f"{description[:2700]}  {date}"))
    return blocks

def _action_entity_verb(action_type: str) -> tuple[str, str]:
    parts = action_type.split("_", 1)
    verb = parts[0].capitalize() if parts else "Completed"
    entity = parts[1].replace("_", " ") if len(parts) > 1 else "record"
    return entity, verb


def _confirmation_label(action_type: str) -> str:
    action, _, entity = action_type.partition("_")
    return f"{entity.replace('_', ' ')} {action}" if entity else action_type.replace("_", " ")


def _confirmation_title(action_type: str) -> str:
    action, _, entity = action_type.partition("_")
    return f"Confirm {action.capitalize()}" if entity else "Confirm"


def _confirmation_question(action_type: str) -> str:
    action, _, entity = action_type.partition("_")
    return f"{action.capitalize()} {entity.replace('_', ' ')}?"


def _confirmation_description(action_type: str, label: str) -> str:
    action = action_type.partition("_")[0]
    return f"{action} HubSpot {label}"


def _plain(text: str) -> Block:
    return {"type": "plain_text", "text": text, "emoji": False}


def _header(text: str) -> Block:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def _section(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _escape_mrkdwn(text: str) -> str:
    return html.escape(text, quote=False)
