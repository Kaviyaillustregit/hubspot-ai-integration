"""Backend formatting and validation for Slack agent responses.

The response renderer owns presentation choices; this panel validates the
resulting Block Kit, protects HubSpot links, and pages channel messages without
dropping response content. ``response.text`` remains the canonical fallback.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlsplit

from app.agent.schemas import AgentResponse

Block = dict[str, object]
Surface = Literal["channel", "home"]
ResponseRenderer = Callable[[AgentResponse], list[Block]]

_MAX_MESSAGE_BLOCKS = 50
_MAX_SECTION_TEXT = 3000
_MAX_SECTION_FIELDS = 10
_MAX_FIELD_TEXT = 2000
_SLACK_LINK = re.compile(r"<([^<>|]+)\|([^<>]*)>")
_ESCAPED_SLACK_LINK = re.compile(r"&lt;(https?[^<>|]+?)\|([^<>]*?)&gt;", re.IGNORECASE)
_NESTED_MARKDOWN_LINK = re.compile(
    r"(?:&lt;|<)?\[(?P<label>[^\]]+)\]\((?P<url>https?[^)\s]+)\)"
    r"(?P<tail>\s+in\s+HubSpot)?(?:&gt;|>)?",
    re.IGNORECASE,
)
_RAW_SLACK_LINK = re.compile(r"<(https?[^<>|]*)\|([^<>]*)>", re.IGNORECASE)
_CODE_SPAN = re.compile(r"(```[\s\S]*?```|`[^`\n]*`)")
_SAFE_ENTITY = re.compile(
    r"&(?:[a-z][a-z\d]+|#\d{1,7}|#x[\da-f]{1,6});", re.IGNORECASE
)
_ESCAPED_EMPHASIS = re.compile(r"\\([*_~])([^\n]+?)\\\1")


@dataclass(frozen=True)
class FormattedSlackResponse:
    """Validated Slack output, with the original response text as fallback."""

    fallback_text: str
    messages: list[list[Block]]
    response_type: str


def format_slack_response(
    response: AgentResponse,
    *,
    surface: Surface = "channel",
    renderer: ResponseRenderer | None = None,
) -> FormattedSlackResponse:
    """Apply the backend panel before a response is sent to a Slack surface."""
    if renderer is None:
        # Import at call time so App Home can use this module to wrap its existing
        # renderer without creating an import cycle or duplicating its layout.
        from app.integrations.slack.home import _build_response_blocks

        renderer = _build_response_blocks

    response_type = _response_type(response)
    raw_blocks = renderer(response)
    blocks = _sanitize_and_validate_blocks(raw_blocks)

    if surface == "home":
        # App Home has its own 100-block composition limit and existing layout
        # handling. Leave its response block sequence intact for the caller.
        messages = [blocks]
    else:
        messages = [
            blocks[index : index + _MAX_MESSAGE_BLOCKS]
            for index in range(0, len(blocks), _MAX_MESSAGE_BLOCKS)
        ] or [[]]

    # Check the final Python data structure before handing it to Slack's API.
    json.dumps(messages, ensure_ascii=False)
    clean_fallback = re.sub(
        r"(?im)^\s*•\s*(?:HubSpot|Contact|Company|Deal)\s*ID:\s*`?[0-9a-zA-Z_-]+`?\s*\n?",
        "",
        response.text,
    )
    clean_fallback = re.sub(r"\s*\(contact ID `?[0-9a-zA-Z_-]+`?\)", "", clean_fallback)
    return FormattedSlackResponse(
        fallback_text=clean_fallback,
        messages=messages,
        response_type=response_type,
    )


def _response_type(response: AgentResponse) -> str:
    result = response.result or {}
    kind = result.get("kind")
    plan = result.get("plan")
    query = result.get("query")
    if not isinstance(query, str) and isinstance(plan, dict):
        query = plan.get("query")
    if response.status == "pending_confirmation" or kind == "pending_confirmation":
        return "confirmation"
    if response.status not in {"ok", "partial"}:
        return "error_or_clarification"
    if result.get("chart_url"):
        return "chart"
    title = result.get("title")
    normalized_title = " ".join(title.casefold().split()) if isinstance(title, str) else ""
    if query in {"contact_details", "company_details", "deal_details"} or normalized_title in {
        "contact details",
        "company details",
        "deal details",
    }:
        return "record_details"
    if query in {"deal_analysis", "deal_follow_up", "closed_won_revenue"}:
        return "analytical_response"
    if isinstance(plan, dict) and (plan.get("group_by") or plan.get("aggregations")):
        return "grouped_summary"
    table = result.get("table")
    if isinstance(table, dict):
        return (
            "grouped_summary"
            if table.get("groups")
            or (isinstance(plan, dict) and (plan.get("group_by") or plan.get("aggregations")))
            else "crm_listing"
        )
    if kind in {"contact_created", "company_created", "deal_created"}:
        return "crud_confirmation"
    if response.cards or result.get("hubspot_url"):
        return "record_details"
    return "conversation"


def format_property_value(key: str, value: object) -> str:
    """Format recognized CRM amounts and ISO dates for display only."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, datetime) and _is_date_property(key):
        return _pretty_datetime(value)
    if isinstance(value, date) and _is_date_property(key):
        return _pretty_date(value)

    if _is_amount_property(key):
        if isinstance(value, (int, float, Decimal)):
            raw = str(value)
        elif isinstance(value, str):
            raw = value.strip()
        else:
            raw = ""
        if raw:
            try:
                amount = Decimal(raw)
            except InvalidOperation:
                pass
            else:
                if amount.is_finite():
                    return format(amount.normalize(), ",f")

    if isinstance(value, str) and _is_date_property(key):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                return _pretty_date(date.fromisoformat(value))
            except ValueError:
                return value
        try:
            parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                return _pretty_date(date.fromisoformat(value))
            except ValueError:
                return value
        return _pretty_datetime(parsed_datetime)

    return "" if value is None else str(value)


def _pretty_date(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def _pretty_datetime(value: datetime) -> str:
    rendered = _pretty_date(value.date())
    if value.timetz().replace(tzinfo=None) == datetime.min.time():
        return rendered
    rendered += f" at {value.strftime('%H:%M:%S')}"
    if value.tzinfo is not None:
        rendered += f" {value.strftime('%z')}"
    return rendered


def _is_amount_property(key: str) -> bool:
    normalized = key.casefold()
    return "amount" in normalized or "revenue" in normalized


def _is_date_property(key: str) -> bool:
    normalized = key.casefold()
    return "date" in normalized or normalized.endswith(("_at", "_time"))


def _sanitize_and_validate_blocks(blocks: list[Block]) -> list[Block]:
    seen_hubspot_urls: set[str] = set()
    output: list[Block] = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ValueError("Slack formatter returned an invalid Block Kit block")
        cleaned = _clean_block(block, seen_hubspot_urls)
        cleaned_text = cleaned.get("text")
        if (
            cleaned.get("type") == "section"
            and isinstance(cleaned_text, dict)
            and cleaned_text.get("type") == "mrkdwn"
            and not str(cleaned_text.get("text", "")).strip()
        ):
            continue
        output.extend(_split_oversized_section(cleaned))
    json.dumps(output, ensure_ascii=False)
    return output


def _clean_block(block: Block, seen_hubspot_urls: set[str]) -> Block:
    cleaned: Block = {}
    for key, value in block.items():
        if key == "text" and isinstance(value, dict):
            text_value = dict(value)
            text = text_value.get("text")
            if text_value.get("type") == "mrkdwn" and isinstance(text, str):
                text_value["text"] = _clean_mrkdwn(text, seen_hubspot_urls)
            cleaned[key] = text_value
        elif isinstance(value, dict):
            cleaned[key] = _clean_block(value, seen_hubspot_urls)
        elif isinstance(value, list):
            cleaned[key] = [
                _clean_block(item, seen_hubspot_urls)
                if isinstance(item, dict)
                else item
                for item in value
            ]
        else:
            cleaned[key] = value
    return cleaned



_SLACK_EMOJI_NAME_MAP: dict[str, str] = {
    ":large_yellow_circle:": "\U0001F7E1",
    ":yellow_circle:": "\U0001F7E1",
    ":large_green_circle:": "\U0001F7E2",
    ":green_circle:": "\U0001F7E2",
    ":large_red_circle:": "\U0001F534",
    ":red_circle:": "\U0001F534",
    ":large_blue_circle:": "\U0001F535",
    ":blue_circle:": "\U0001F535",
    ":large_blue_diamond:": "\U0001F7E6",
    ":blue_diamond:": "\U0001F7E6",
    ":white_circle:": "\u26AA",
    ":purple_circle:": "\U0001F7E3",
    ":brown_circle:": "\U0001F7E4",
    ":large_orange_circle:": "\U0001F7E0",
    ":orange_circle:": "\U0001F7E0",
}
_SLACK_EMOJI_IMAGE_LINK = re.compile(
    r"\[(?P<label>[^\]]*)\]\(https?://[^\s)]*slack-edge\.com[^\s)]*\)\s*",
    re.IGNORECASE,
)


def _clean_slack_emoji_links(text: str) -> str:
    def replace_emoji(match: re.Match[str]) -> str:
        label = match.group("label").strip()
        emoji = _SLACK_EMOJI_NAME_MAP.get(label.casefold())
        if emoji:
            return f"{emoji} "
        if label.startswith(":") and label.endswith(":"):
            return f"{label} "
        return ""

    text = _SLACK_EMOJI_IMAGE_LINK.sub(replace_emoji, text)
    text = re.sub(r"https?://[^\s<>|)]*slack-edge\.com[^\s<>|)]*", "", text)
    return text


def _clean_mrkdwn(text: str, seen_hubspot_urls: set[str]) -> str:
    text = _clean_slack_emoji_links(text)
    text = _normalize_escaped_slack_links(text)

    def replace_link(match: re.Match[str]) -> str:
        raw_url, label = match.groups()
        url, is_hubspot_link, valid = _hubspot_link_status(raw_url, label)

        if not is_hubspot_link:
            return match.group(0)
        if not valid:
            return label
        dedupe_key = _hubspot_link_key(url)
        if dedupe_key in seen_hubspot_urls:
            return ""
        seen_hubspot_urls.add(dedupe_key)
        return f"<{html.escape(url, quote=False)}|{label}>"

    return _SLACK_LINK.sub(replace_link, text)


def _normalize_escaped_slack_links(text: str) -> str:
    """Restore HTML-escaped Slack link syntax before the text is escaped again."""
    for _ in range(2):
        normalized = _ESCAPED_SLACK_LINK.sub(_restore_escaped_link, text)
        if normalized == text:
            break
        text = normalized
    return text


def _restore_escaped_link(match: re.Match[str]) -> str:
    url = _normalize_url_backslashes(html.unescape(match.group(1)))
    label = html.unescape(match.group(2))
    normalized, is_hubspot_link, valid = _hubspot_link_status(url, label)
    if valid and is_hubspot_link:
        return f"<{normalized}|{label}>"
    return label if is_hubspot_link else match.group(0)


def _hubspot_link_key(url: str) -> str:
    """Deduplicate links to the same HubSpot record despite query/fragment noise."""
    parsed = urlsplit(url)
    return f"{(parsed.hostname or '').casefold()}{parsed.path.rstrip('/')}"


def escape_slack_text(text: str) -> str:
    """Normalize and escape narrative text, retaining only validated HubSpot links."""
    return normalize_slack_display_text(text)


def normalize_slack_display_text(text: str) -> str:
    """Normalize current or stored AI prose for safe Slack mrkdwn display."""
    text = re.sub(
        r"(?im)^\s*•\s*(?:HubSpot|Contact|Company|Deal)\s*ID:\s*`?[0-9a-zA-Z_-]+`?\s*\n?",
        "",
        text,
    )
    text = re.sub(r"\s*\(contact ID `?[0-9a-zA-Z_-]+`?\)", "", text)
    links: list[tuple[str, str]] = []
    seen_hubspot_urls: set[str] = set()
    output: list[str] = []

    for part in _CODE_SPAN.split(text):
        if not part:
            continue
        if _CODE_SPAN.fullmatch(part):
            output.append(html.escape(part, quote=False))
            continue

        normalized = _normalize_display_prose(part)

        def preserve_or_remove_link(match: re.Match[str]) -> str:
            raw_url, label = match.groups()
            url = _normalize_url_backslashes(raw_url)
            url, is_hubspot_link, valid = _hubspot_link_status(url, label)
            if not is_hubspot_link:
                return match.group(0)
            if not valid:
                return label
            dedupe_key = _hubspot_link_key(url)
            if dedupe_key in seen_hubspot_urls:
                return ""
            seen_hubspot_urls.add(dedupe_key)
            marker = f"\uE000SLACKLINK{len(links)}\uE001"
            safe_url = html.escape(url, quote=False)
            safe_label = html.escape(label, quote=False)
            links.append((marker, f"<{safe_url}|{safe_label}>"))
            return marker

        escaped = html.escape(_RAW_SLACK_LINK.sub(preserve_or_remove_link, normalized), quote=False)
        for marker, link in links:
            escaped = escaped.replace(marker, link)
        links.clear()
        output.append(escaped)
    return "".join(output)


def _normalize_display_prose(text: str) -> str:
    text = _clean_slack_emoji_links(text)
    text = _decode_safe_entities(text)
    text = _NESTED_MARKDOWN_LINK.sub(_restore_nested_markdown_link, text)
    text = _normalize_escaped_slack_links(text)

    def normalize_raw_link(match: re.Match[str]) -> str:
        raw_url, label = match.groups()
        url = _normalize_url_backslashes(raw_url)
        normalized_url, is_hubspot_link, valid = _hubspot_link_status(url, label)
        if not is_hubspot_link:
            return match.group(0)
        if not valid:
            return "HubSpot link unavailable"
        return f"<{normalized_url}|{label}>"

    text = _RAW_SLACK_LINK.sub(normalize_raw_link, text)
    text = _ESCAPED_EMPHASIS.sub(r"\1\2\1", text)
    return re.sub(r"(?m)^([ \t]*)\\(-)(?=\s)", r"\1\2", text)


def _restore_nested_markdown_link(match: re.Match[str]) -> str:
    label = _decode_safe_entities(match.group("label"))
    target = _normalize_url_backslashes(_decode_safe_entities(match.group("url")))
    label_url, separator, label_text = label.partition("|")
    candidates = [target]
    if separator and label_url.strip().lower().startswith("https"):
        candidates.append(_normalize_url_backslashes(label_url.strip()))

    for candidate in candidates:
        normalized, is_hubspot_link, valid = _hubspot_link_status(candidate, label)
        if is_hubspot_link and valid:
            display_label = label_text.strip() if separator else label.strip()
            if match.group("tail") and "hubspot" not in display_label.casefold():
                display_label = f"{display_label} in HubSpot"
            if not display_label or display_label.lower().startswith("https"):
                display_label = "View in HubSpot"
            return f"<{normalized}|{display_label}>"

    if "hubspot" in (label + target).casefold():
        display_label = label_text.strip() if separator else label.strip()
        if match.group("tail") and "hubspot" not in display_label.casefold():
            display_label = f"{display_label} in HubSpot"
        return display_label or "HubSpot link unavailable"
    return match.group(0)


def _decode_safe_entities(text: str) -> str:
    def decode(match: re.Match[str]) -> str:
        value = html.unescape(match.group(0))
        if all(character.isprintable() or character in "\t\n\r" for character in value):
            return value
        return match.group(0)

    return _SAFE_ENTITY.sub(decode, text)


def _normalize_url_backslashes(url: str) -> str:
    return re.sub(r"\\(?=[:/])", "", url)


def _hubspot_link_status(raw_url: str, label: str) -> tuple[str, bool, bool]:
    url = html.unescape(raw_url)
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        is_hubspot_link = "hubspot" in hostname or "hubspot" in label.lower()
        valid = (
            parsed.scheme == "https"
            and hostname == "app.hubspot.com"
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443}
            and bool(parsed.path)
            and not any(char in url for char in "<>|")
        )
    except ValueError:
        is_hubspot_link = "hubspot" in label.lower()
        valid = False
    return url, is_hubspot_link, valid


def _split_oversized_section(block: Block) -> list[Block]:
    if block.get("type") != "section":
        return [block]

    fields = block.get("fields")
    if isinstance(fields, list):
        output: list[Block] = []
        field_group: list[object] = []

        def flush_fields() -> None:
            if field_group:
                output.append({**block, "fields": list(field_group)})
                field_group.clear()

        for field in fields:
            field_text = field.get("text") if isinstance(field, dict) else None
            if isinstance(field_text, str) and len(field_text) > _MAX_FIELD_TEXT:
                flush_fields()
                output.extend(
                    {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
                    for chunk in _split_text_at_limit(field_text, _MAX_SECTION_TEXT)
                )
                continue
            field_group.append(field)
            if len(field_group) == _MAX_SECTION_FIELDS:
                flush_fields()
        flush_fields()
        return output

    text = block.get("text")
    if not isinstance(text, dict) or text.get("type") != "mrkdwn":
        return [block]
    value = text.get("text")
    if not isinstance(value, str) or len(value) <= _MAX_SECTION_TEXT:
        return [block]

    chunks: list[str] = []
    remaining = value
    while len(remaining) > _MAX_SECTION_TEXT:
        split_at = remaining.rfind("\n", 0, _MAX_SECTION_TEXT + 1)
        if split_at < 1:
            split_at = _MAX_SECTION_TEXT
        else:
            split_at += 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    chunks.append(remaining)
    return [
        {**block, "text": {**text, "text": chunk}}
        for chunk in chunks
    ]


def _split_text_at_limit(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < 1:
            split_at = limit
        else:
            split_at += 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return chunks


# -------------------------------------------------------------------------
# Shared CRUD response card formatting
# -------------------------------------------------------------------------

_MOJIBAKE_EM_DASH = "—".encode("utf-8").decode("latin-1")


def _format_crud_date(val: object) -> str:
    if val is None or val == "" or val == "—" or val == _MOJIBAKE_EM_DASH:
        return "Not available"
    if isinstance(val, (int, float)):
        try:
            ts = float(val)
            if ts > 1e11:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return str(val)
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, date):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    if not s or s.casefold() in {"not available", "none", "null", "—"}:
        return "Not available"
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if iso_match:
        return iso_match.group(1)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%b %d, %Y at %H:%M:%S %z",
        "%b %d, %Y",
        "%B %d, %Y",
        "%m/%d/%Y",
        "%d/%m/%Y",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def _format_crud_amount(val: object) -> str:
    if val is None or val == "" or val == "—" or val == _MOJIBAKE_EM_DASH:
        return "Not available"
    if isinstance(val, (int, float, Decimal)):
        try:
            d = Decimal(str(val))
            if d == d.to_integral():
                return f"${int(d):,}"
            return f"${d:,.2f}"
        except Exception:
            return f"${val}"
    s = str(val).strip()
    if not s or s.casefold() in {"not available", "none", "null", "—"}:
        return "Not available"
    if s.startswith("$"):
        return s
    cleaned = re.sub(r"^[A-Z]{3}\s*", "", s, flags=re.IGNORECASE).replace(",", "").strip()
    try:
        d = Decimal(cleaned)
        if d == d.to_integral():
            return f"${int(d):,}"
        return f"${d:,.2f}"
    except (InvalidOperation, ValueError):
        return f"${s}"


def _resolve_crud_owner(val: object, row: dict[str, Any]) -> str:
    for key in ("owner_name", "hubspot_owner_name"):
        cand = row.get(key)
        if isinstance(cand, str) and cand.strip() and not cand.strip().isdigit():
            return cand.strip()
    if val is None or val == "" or val == "—" or val == _MOJIBAKE_EM_DASH:
        return "Unassigned"
    s = str(val).strip()
    if not s or s.casefold() in {"unassigned", "none", "null", "—"}:
        return "Unassigned"
    if s.isdigit():
        return "Owner not available"
    if s.casefold() in {"owner not available", "not available"}:
        return "Owner not available"
    return s


def _resolve_crud_company(val: object, row: dict[str, Any]) -> str:
    for key in ("company_name", "companyname", "associated_company_name", "account_name"):
        cand = row.get(key)
        if isinstance(cand, str) and cand.strip() and not cand.strip().isdigit():
            return cand.strip()
    if val is None or val == "" or val == "—" or val == _MOJIBAKE_EM_DASH:
        return "Not available"
    s = str(val).strip()
    if not s or s.casefold() in {"not available", "none", "null", "—"}:
        return "Not available"
    if s.isdigit():
        return "Not available"
    return s


def render_crud_card(
    record_type: str,
    operation: str,
    row: dict[str, Any] | None = None,
    *,
    record_name: str | None = None,
    record_url: str | None = None,
    changes: dict[str, Any] | None = None,
    today_date: str | None = None,
) -> list[Block]:
    """Render a consistent Block Kit response card for CREATE, UPDATE, and DELETE operations.

    1. Divider: {"type": "divider"}
    2. Header: {emoji} *<{url}|{name}>* ({badge}) + View in HubSpot ↗ accessory button.
       (Accessory button omitted on DELETE).
    3. Fields: 2 or 4 balanced fields in a 2-column grid.
       - Owner / company IDs resolved to display names.
       - Monetary amounts formatted with currency ($).
       - Dates formatted as YYYY-MM-DD.
       - Missing fields handled gracefully without None or raw internal IDs.
    """
    if today_date is None:
        today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    data = dict(row or {})
    op = (operation or "create").casefold()
    is_delete = op in {"delete", "archive"}
    is_update = op in {"update", "edit"}

    rt = (record_type or "record").casefold()
    for cand in ("contact", "company", "deal"):
        if cand in rt:
            rt = cand
            break

    emoji_map = {"contact": "👤", "company": "🏢", "deal": "💼"}
    badge_map = {"contact": "Contact", "company": "Company", "deal": "Deal"}
    emoji = emoji_map.get(rt, "📄")
    badge = badge_map.get(rt, "Record")

    name = record_name
    if not name:
        if rt == "contact":
            first = data.get("firstname") or data.get("first_name")
            last = data.get("lastname") or data.get("last_name")
            f_clean = str(first).strip() if first and str(first).strip() != "None" else ""
            l_clean = str(last).strip() if last and str(last).strip() != "None" else ""
            if f_clean and l_clean:
                name = f"{f_clean} {l_clean}"
            elif f_clean or l_clean:
                name = f_clean or l_clean
        elif rt == "deal":
            for k in ("dealname", "deal_name", "title"):
                if data.get(k):
                    name = str(data[k]).strip()
                    break
        elif rt == "company":
            for k in ("company_name", "companyname"):
                if data.get(k):
                    name = str(data[k]).strip()
                    break
    if not name:
        for k in ("name", "title", "label"):
            if data.get(k) and not str(data[k]).strip().casefold().startswith("http"):
                name = str(data[k]).strip()
                break
    if not name:
        name = badge
    escaped_name = html.escape(str(name), quote=False)

    url_candidate = record_url or data.get("hubspot_url") or data.get("view_url") or data.get("url")
    valid_url = None
    if not is_delete and isinstance(url_candidate, str) and url_candidate.strip():
        u = url_candidate.strip()
        parsed = urlsplit(u)
        if parsed.scheme == "https" and not any(c in u for c in '<>|" '):
            valid_url = u

    # 1. Divider
    card_blocks: list[Block] = [{"type": "divider"}]

    # 2. Header
    header_text = (
        f"{emoji} *<{valid_url}|{escaped_name}>* ({badge})"
        if valid_url
        else f"{emoji} *{escaped_name}* ({badge})"
    )
    header_block: Block = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": header_text},
    }
    card_blocks.append(header_block)

    def _fmt_crud_val(k: str, v: object) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        if not s or s.casefold() in {"none", "null", "undefined", "—", _MOJIBAKE_EM_DASH}:
            return ""
        k_lower = k.casefold()
        if k_lower in {"owner", "hubspot_owner_id", "owner_id"}:
            res = _resolve_crud_owner(s, data)
            return "" if res in {"Owner not available", "Unassigned"} and not s else res
        if k_lower in {"company", "company_id", "associatedcompanyid", "associated_company"}:
            res = _resolve_crud_company(s, data)
            return "" if res == "Not available" and not s else res
        if any(x in k_lower for x in ("amount", "revenue", "price", "total")):
            return _format_crud_amount(v)
        if any(x in k_lower for x in ("date", "closedate", "createdate")):
            return _format_crud_date(v)
        if k_lower.endswith("_id") or k_lower in {"id", "hs_object_id"}:
            return ""
        return html.escape(s, quote=False)

    def _crud_field_mrkdwn(key: str, label: str, val: str) -> str:
        k_l = key.casefold()
        l_l = label.casefold()
        if "email" in k_l or "email" in l_l:
            e, lbl = "📧", "Email"
        elif "phone" in k_l or "phone" in l_l:
            e, lbl = "📞", "Phone"
        elif "domain" in k_l or "website" in k_l:
            e, lbl = "🌐", "Domain"
        elif "industry" in k_l:
            e, lbl = "🏭", "Industry"
        elif "city" in k_l or "location" in k_l:
            e, lbl = "📍", "City"
        elif "amount" in k_l or "revenue" in k_l:
            e, lbl = "💰", "Amount"
        elif "stage" in k_l:
            e, lbl = "📊", "Stage"
        elif "date" in k_l or "close" in k_l:
            e, lbl = "📅", (label if label else "Close Date")
        elif "owner" in k_l:
            e, lbl = "👤", "Owner"
        elif "company" in k_l or "account" in k_l:
            e, lbl = "🏢", "Company"
        elif "title" in k_l or "job" in k_l:
            e, lbl = "💼", "Job Title"
        elif "status" in k_l or "status" in l_l:
            e, lbl = "🔄", label if label else "Status"
        elif "deleted" in l_l or "archived" in l_l:
            e, lbl = "🗑️", label
        elif "updated" in l_l:
            e, lbl = "🔄", "Updated"
        else:
            e = emoji_map.get(rt, "📌")
            lbl = label.replace("_", " ").title()
        return f"*{e} {lbl}:* {val}"

    fields: list[str] = []

    if is_delete:
        fields.append("*🗑️ Status:* Archived")
        if rt == "contact":
            ident_val = _fmt_crud_val("email", data.get("email"))
            if ident_val:
                fields.append(_crud_field_mrkdwn("email", "Email", ident_val))
        elif rt == "company":
            ident_val = _fmt_crud_val("domain", data.get("domain") or data.get("website"))
            if ident_val:
                fields.append(_crud_field_mrkdwn("domain", "Domain", ident_val))
        elif rt == "deal":
            ident_val = _fmt_crud_val("amount", data.get("amount"))
            if ident_val:
                fields.append(_crud_field_mrkdwn("amount", "Amount", ident_val))

        if len(fields) == 1:
            fields.append(f"*📅 Archived Date:* {today_date}")

    elif is_update:
        ch = changes or data.get("changes") or data.get("properties") or {}
        for k, v in ch.items():
            f_val = _fmt_crud_val(k, v)
            if f_val:
                fields.append(_crud_field_mrkdwn(k, k.replace("_", " ").title(), f_val))

        if len(fields) >= 4:
            fields = fields[:4]
        elif len(fields) == 3:
            fields.append(f"*📅 Updated:* {today_date}")
        elif len(fields) == 2:
            pass
        elif len(fields) == 1:
            fields.append(f"*📅 Updated:* {today_date}")
        else:
            fields = ["*🔄 Status:* Updated", f"*📅 Updated:* {today_date}"]

    else:  # CREATE
        candidates: list[tuple[str, str, str]] = []
        if rt == "contact":
            for k in ("email", "phone", "company", "jobtitle", "owner", "createdate"):
                if k == "owner":
                    v = data.get("owner") or data.get("owner_name") or data.get("owner_id") or data.get("hubspot_owner_id")
                else:
                    v = data.get(k)
                fv = _fmt_crud_val(k, v)
                if fv:
                    candidates.append((k, k.replace("_", " ").title(), fv))
        elif rt == "company":
            for k in ("domain", "industry", "city", "phone", "hubspot_owner_id", "createdate"):
                v = data.get(k)
                fv = _fmt_crud_val(k, v)
                if fv:
                    candidates.append((k, k.replace("_", " ").title(), fv))
        elif rt == "deal":
            for k in ("amount", "dealstage", "closedate", "company", "hubspot_owner_id"):
                v = data.get(k)
                fv = _fmt_crud_val(k, v)
                if fv:
                    candidates.append((k, k.replace("_", " ").title(), fv))

        for k, lbl, val in candidates:
            fields.append(_crud_field_mrkdwn(k, lbl, val))

        if len(fields) >= 4:
            fields = fields[:4]
        elif len(fields) == 3:
            fields.append(f"*📅 Created:* {today_date}")
        elif len(fields) == 2:
            pass
        elif len(fields) == 1:
            fields.append(f"*📅 Created:* {today_date}")
        else:
            fields = ["*🔄 Status:* Active", f"*📅 Created:* {today_date}"]

    fields_block: Block = {
        "type": "section",
        "fields": [{"type": "mrkdwn", "text": f} for f in fields],
    }
    if valid_url and not is_delete:
        fields_block["accessory"] = {
            "type": "button",
            "text": {"type": "plain_text", "text": "View in HubSpot ↗", "emoji": True},
            "url": valid_url,
            "action_id": "view_in_hubspot",
        }
    card_blocks.append(fields_block)
    return card_blocks


def render_crud_response_card(response: AgentResponse) -> list[Block] | None:
    """Inspect an AgentResponse and render consistent CRUD response card blocks if applicable."""
    result = response.result or {}
    title = str(result.get("title") or "")
    tools_used = response.tools_used or []
    cards = response.cards or []
    text = response.text or ""

    kind = result.get("kind")
    is_mutation = bool(
        result.get("card") is True
        or result.get("crud_card") is True
        or result.get("operation") in {"create", "update", "delete", "archive"}
        or kind in {"company_created", "deal_created"}
        or (kind == "contact_created" and any("create_contact" in t for t in tools_used))
        or any(
            t in title
            for t in (
                "Company Updated", "Deal Updated", "Contact Updated",
                "Company Archived", "Deal Archived", "Contact Archived",
                "Company Deleted", "Deal Deleted", "Contact Deleted",
                "Company created", "Deal created", "Contact created",
            )
        )
        or any(
            tool in {
                "create_company", "create_deal",
                "update_contact", "update_company", "update_deal",
                "delete_contact", "delete_company", "delete_deal",
            }
            for tool in tools_used
        )
    )

    if not is_mutation:
        return None

    obj_type = result.get("object_type") or result.get("record_type")
    if not obj_type:
        for cand in ("contact", "company", "deal"):
            if cand in title.casefold():
                obj_type = cand
                break
        if not obj_type and cards:
            obj_type = cards[0].get("kind")
        if not obj_type:
            for tool in tools_used:
                for cand in ("contact", "company", "deal"):
                    if cand in tool:
                        obj_type = cand
                        break
        if not obj_type:
            for cand in ("contact", "company", "deal"):
                if cand in text.casefold():
                    obj_type = cand
                    break

    if not obj_type:
        return None

    operation = result.get("operation")
    if not operation:
        if any(w in title.casefold() for w in ("archive", "delete")) or any("delete_" in t for t in tools_used) or "archived in hubspot" in text.casefold() or "deleted successfully" in text.casefold():
            operation = "delete"
        elif "update" in title.casefold() or any("update_" in t for t in tools_used) or "updated successfully" in text.casefold():
            operation = "update"
        else:
            operation = "create"

    record_name = result.get("record_name") or result.get("name")
    if not record_name and cards:
        record_name = cards[0].get("name")
    if not record_name:
        name_match = re.search(
            r"(?:Company|Deal|Contact)\s+([A-Za-z0-9._\s-]+?)\s+(?:updated|archived|deleted|created|was created)",
            text,
            re.IGNORECASE,
        )
        if name_match:
            record_name = name_match.group(1).strip()

    record_url = result.get("hubspot_url")
    if not record_url and cards:
        record_url = cards[0].get("hubspot_url")

    changes = result.get("changes") or result.get("properties")
    if not changes and operation == "update":
        changes_match = re.search(r"• Changes:\s*(.*)", text)
        if changes_match:
            raw_changes = changes_match.group(1).strip()
            changes = {}
            for part in raw_changes.split(","):
                if ":" in part:
                    k, _, v = part.partition(":")
                    changes[k.strip()] = v.strip()
                elif part.strip():
                    changes[part.strip()] = "Updated"

    data = dict(result)
    if cards:
        data.update(cards[0])
    if changes and isinstance(changes, dict):
        data.setdefault("changes", changes)

    card_blocks = render_crud_card(
        obj_type,
        operation,
        data,
        record_name=record_name,
        record_url=record_url,
        changes=changes,
    )

    clean_summary = re.sub(
        r"(?im)^\s*•\s*(?:HubSpot|Contact|Company|Deal)\s*ID:\s*`?[0-9a-zA-Z_-]+`?\s*\n?",
        "",
        text,
    ).strip()


    blocks: list[Block] = []
    if clean_summary:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": clean_summary}})
    blocks.extend(card_blocks)

    owner_name = result.get("owner_name")
    if isinstance(owner_name, str) and owner_name and owner_name != "Unassigned":
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "👤 Owner: " + html.escape(owner_name, quote=False)}],
        })

    if operation != "delete" and record_url:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{record_url}|View in HubSpot>"},
        })

    return blocks
