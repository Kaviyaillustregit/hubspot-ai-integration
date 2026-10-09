import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from app.agent.schemas import AgentResponse

REQUEST_INPUT_ACTION = "home_request_input"
SUBMIT_ACTION = "home_submit"
CONFIRM_ACTION = "home_confirm"
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
    recent_unavailable: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    blocks: list[Block] = [
        {"type": "header", "text": _plain("HubSpot AI Agent")},
        _context("✦  Your AI assistant for HubSpot CRM"),
        _section(
            "I can create and update contacts, companies and deals, or answer questions "
            "about your CRM. Enter a request below to get started."
        ),
        {"type": "divider"},
        _section("✦  *AI Assistant*"),
        {"type": "divider"},
    ]

    if request_text is not None:
        blocks.extend(_user_request_blocks(request_text))
    if working:
        blocks.extend([_context("✦  *AI Assistant*"), _section("⋯")])
    elif response is not None:
        blocks.extend(render_response_blocks(response))

    blocks.extend(
        [
            {
                "type": "input",
                "block_id": f"home_request:{uuid4().hex[:12]}",
                "label": _plain("CRM request"),
                "dispatch_action": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": REQUEST_INPUT_ACTION,
                    "placeholder": _plain("Ask your HubSpot AI Agent..."),
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
            {"type": "divider"},
        ],
    )

    if recent_unavailable:
        blocks.append(_context("Recent CRM activity is temporarily unavailable."))
    elif recent:
        blocks.extend(_recent_activity_blocks(recent, now or datetime.now(UTC)))
    else:
        blocks.extend([_section("*Recent activity*"), _context("No CRM actions yet")])

    for key, template in _QUICK_ACTIONS.items():
        if key == "create_contact":
            label = "Create contact"
        elif key == "find_contact":
            label = "Find contact"
        elif key == "find_company":
            label = "Find company"
        elif key == "account_overview":
            label = "Account overview"
        else:
            label = "Search CRM"
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": f"{QUICK_ACTION_PREFIX}{key}",
                        "text": _plain(label),
                        "value": template,
                    }
                ],
            }
        )

    blocks.append(_context("Powered by HubSpot AI"))
    return {"type": "home", "blocks": blocks[:100]}


def _user_request_blocks(request_text: str) -> list[Block]:
    return [
        _context("*You*"),
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


def render_response_blocks(response: AgentResponse) -> list[Block]:
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
                _section("🛡  *Confirmation needed*"),
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
        heading = "ⓘ  *More information needed*"
    elif response.status == "already_exists":
        heading = "ⓘ  *" + html.escape(str(result.get("title") or "Already exists")) + "*"
    elif response.status in {"partial", "reconciliation_required"}:
        heading = "⚠  *Partially completed*"
    elif response.status == "ok" and kind == "contact_created":
        heading = "✓  *Contact created*"
    elif response.status == "ok" and isinstance(result.get("title"), str):
        heading = "✓  *" + html.escape(str(result["title"])) + "*"
    elif response.status == "ok":
        heading = "✓  *Here's what I found*"
    else:
        heading = "⚠  *Unable to complete request*"

    table = _validated_table(result.get("table"))
    if table is not None:
        return _render_table_response(heading, response.text, table)

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
    heading: str, summary: str, table: dict[str, Any]
) -> list[Block]:
    blocks = [_section(heading), *(_section(chunk) for chunk in _split_text(summary))]
    columns = list(table["columns"])
    rows = table["rows"]
    if any("view_url" in row for row in rows) and not any(
        column["key"] == "view_url" for column in columns
    ):
        columns.append({"key": "view_url", "label": "View in HubSpot"})

    if not rows:
        blocks.append(_context("No records to display."))
        return blocks

    rendered_rows = 0
    limit_reached = False
    for row in rows:
        row_blocks = _render_table_row(columns, row)
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


def _render_table_row(columns: list[dict[str, str]], row: dict[str, Any]) -> list[Block]:
    lines: list[str] = []
    for column in columns:
        key = column["key"]
        label = html.escape(column["label"], quote=False)
        raw_value = row.get(key)
        if key == "view_url":
            value = _slack_hubspot_link(raw_value)
        else:
            value = html.escape("" if raw_value is None else str(raw_value), quote=False)
        if not value:
            continue
        lines.append(f"*{label}:* {value}")

    if not lines:
        return [_section("No record details available.")]
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
    escaped = html.escape(text, quote=False)
    if not escaped:
        return ["No response was returned."]
    chunks: list[str] = []
    while len(escaped) > 3000:
        split_at = escaped.rfind("\n", 0, 3000)
        if split_at < 1:
            split_at = 3000
        chunks.append(escaped[:split_at])
        escaped = escaped[split_at:].lstrip("\n")
    chunks.append(escaped)
    return chunks


def _recent_activity_blocks(recent: list[Any], now: datetime) -> list[Block]:
    blocks = [_section("*Recent activity*")]
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
                line = f"✅ Completed *{description}*"
            elif status == "reconciliation_required":
                line = f"❗ Partially completed *{description}*"
            else:
                line = f"⚠️ Request *{description}* was not completed"
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
                line = f"✅ {completed_verb} {entity} *{safe_name}*"
            elif status == "pending":
                name_is_identifier = "contact_id" in payload and not any(
                    key in payload
                    for key in ("firstname", "first_name", "deal_name", "company_name")
                )
                formatted_name = f"`{safe_name}`" if name_is_identifier else f"*{safe_name}*"
                line = (
                    f"🛡️ {verb} of {entity} {formatted_name} "
                    "is awaiting your confirmation"
                )
            elif status == "expired":
                line = f"⌛ {verb} of {entity} `{safe_name}` expired before confirmation"
            else:
                line = f"⚠️ {verb} of {entity} *{safe_name}* was not completed"
        created_at = action.created_at
        timestamp = int(
            (created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)).timestamp()
        )
        blocks.append(_context(f"{line}  <!date^{timestamp}^{{date_short_pretty}}|{now:%Y-%m-%d}>"))
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
    return {"type": "plain_text", "text": text, "emoji": True}


def _section(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}
