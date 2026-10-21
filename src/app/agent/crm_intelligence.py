from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.integrations.hubspot.models import HubSpotDeal, HubSpotPipeline, HubSpotPipelineStage
from app.services.quickchart import build_chart_config

FieldName = str

_DEAL_FIELD_ALIASES: dict[str, str] = {
    "dealname": "dealname",
    "deal_name": "dealname",
    "name": "dealname",
    "deal": "dealname",
    "stage": "dealstage",
    "stagename": "dealstage",
    "stage_name": "dealstage",
    "dealstage": "dealstage",
    "deal_stage": "dealstage",
    "pipeline": "pipeline",
    "pipelinename": "pipeline",
    "pipeline_name": "pipeline",
    "amount": "amount",
    "deal_amount": "amount",
    "value": "amount",
    "revenue": "amount",
    "closedate": "closedate",
    "close_date": "closedate",
    "close date": "closedate",
    "createdate": "createdate",
    "create_date": "createdate",
    "creation date": "createdate",
    "created date": "createdate",
    "owner": "hubspot_owner_id",
    "owner_id": "hubspot_owner_id",
    "hubspot_owner_id": "hubspot_owner_id",
    "ownerid": "hubspot_owner_id",
    "probability": "hs_probability",
    "hs_probability": "hs_probability",
    "hs_deal_stage_probability": "hs_deal_stage_probability",
    "dealtype": "dealtype",
    "deal_type": "dealtype",
    "company": "company",
    "company_name": "company",
    "companyname": "company",
    "id": "id",
    "deal_id": "id",
    "record_id": "id",
}

_COMPANY_FIELD_ALIASES: dict[str, str] = {
    "company": "name",
    "name": "name",
    "company_name": "name",
    "owner": "hubspot_owner_id",
    "owner_id": "hubspot_owner_id",
    "hubspot_owner_id": "hubspot_owner_id",
    "phone": "phone",
    "city": "city",
    "industry": "industry",
    "employees": "numberofemployees",
    "numberofemployees": "numberofemployees",
    "lifecyclestage": "lifecyclestage",
    "lead_status": "hs_lead_status",
    "hs_lead_status": "hs_lead_status",
    "last_contacted": "notes_last_contacted",
    "notes_last_contacted": "notes_last_contacted",
}

_CONTACT_FIELD_ALIASES: dict[str, str] = {
    "contact": "firstname",
    "name": "firstname",
    "firstname": "firstname",
    "first_name": "firstname",
    "lastname": "lastname",
    "last_name": "lastname",
    "email": "email",
    "phone": "phone",
    "company": "associatedcompanyid",
    "city": "city",
    "state": "state",
    "industry": "industry",
    "owner": "hubspot_owner_id",
    "owner_id": "hubspot_owner_id",
    "hubspot_owner_id": "hubspot_owner_id",
    "jobtitle": "jobtitle",
    "job_title": "jobtitle",
    "company_name": "associatedcompanyid",
    "lead_status": "hs_lead_status",
    "hs_lead_status": "hs_lead_status",
    "lifecycle": "lifecyclestage",
    "lifecyclestage": "lifecyclestage",
}

_FIELD_ALIASES = {
    "deal": _DEAL_FIELD_ALIASES,
    "company": _COMPANY_FIELD_ALIASES,
    "contact": _CONTACT_FIELD_ALIASES,
}


class CRMIntelligenceFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(max_length=64)
    operator: Literal["eq", "neq", "gt", "gte", "lt", "lte", "in", "contains"] = "eq"
    value: str = Field(max_length=200)


class CRMIntelligenceSort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(max_length=64)
    direction: Literal["asc", "desc"] = "desc"


class CRMIntelligenceAggregation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["count", "sum", "avg", "min", "max"]
    field: str = Field(max_length=64)


class CRMIntelligenceDateRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str | None = Field(default=None, max_length=64)
    end: str | None = Field(default=None, max_length=64)
    relative: str | None = Field(default=None, max_length=64)
    field: str = Field(default="closedate", max_length=64)
    timezone: str = Field(default="UTC", max_length=64)

    @model_validator(mode="after")
    def _validate_bounds(self) -> CRMIntelligenceDateRange:
        try:
            zone = ZoneInfo(self.timezone)
            start = date.fromisoformat(self.start) if self.start else None
            end = date.fromisoformat(self.end) if self.end else None
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Date filters require valid ISO bounds and an IANA timezone") from exc
        if start and end and end <= start:
            raise ValueError("Date filter end must be after start")
        if start is None and end is None and self.relative:
            resolved_start, resolved_end = _relative_date_range(self.relative, timezone=zone.key)
            if resolved_start is None and resolved_end is None:
                raise ValueError("Unsupported relative date range")
        return self


class CRMQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object: Literal["deal", "company", "contact"]
    properties: list[str] = Field(default_factory=list, max_length=20)
    filters: list[CRMIntelligenceFilter] = Field(default_factory=list, max_length=20)
    sort: list[CRMIntelligenceSort] = Field(default_factory=list, max_length=5)
    group_by: str | None = Field(default=None, max_length=64)
    group_date_field: str = Field(default="closedate", max_length=64)
    aggregations: list[CRMIntelligenceAggregation] = Field(default_factory=list, max_length=10)
    date_range: CRMIntelligenceDateRange | None = None
    limit: int | None = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=100_000)
    chart_requested: bool = False
    chart_type: Literal["bar", "line", "pie", "doughnut", "funnel"] | None = None

    @model_validator(mode="after")
    def _validate_supported_fields(self) -> CRMQueryPlan:
        object_aliases = _FIELD_ALIASES.get(self.object, {})
        valid = set(object_aliases.values())
        for prop in self.properties:
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9 _./-]{0,63}", prop):
                raise ValueError(f"Invalid '{self.object}' property name: {prop}")
        allowed_groups = {
            "deal": {"month", "quarter", "year"},
            "company": set(),
            "contact": set(),
        }
        group = _normalise_text(self.group_by).replace("_", " ") if self.group_by else None
        if (
            group is not None
            and group not in allowed_groups.get(self.object, set())
            and (self._canonical_field(self.group_by or "", self.object) not in valid)
            and not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9 _./-]{0,63}", self.group_by or "")
        ):
            raise ValueError(f"Unsupported group_by field: {self.group_by}")
        for item in self.filters:
            if self._canonical_field(item.field, self.object) not in valid and not re.fullmatch(
                r"[a-zA-Z][a-zA-Z0-9 _./-]{0,63}", item.field
            ):
                raise ValueError(f"Unsupported filter field: {item.field}")
        for item in self.aggregations:
            if self._canonical_field(item.field, self.object) not in valid and not re.fullmatch(
                r"[a-zA-Z][a-zA-Z0-9 _./-]{0,63}", item.field
            ):
                raise ValueError(f"Unsupported aggregation field: {item.field}")
        return self

    @staticmethod
    def _canonical_field(field: str, object_name: str) -> str:
        return _normalise_field_name(field, object_name)


def _normalise_text(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def _normalise_field_name(field: str, object_name: str) -> str:
    raw = _normalise_text(field)
    if not raw:
        return ""
    raw = raw.replace("-", " ").replace("_", " ")
    raw = raw.replace("/", " ")
    aliases = _FIELD_ALIASES.get(object_name, {})
    if raw in aliases:
        return aliases[raw]
    compact = re.sub(r"[^a-z0-9]+", "", raw)
    for key, canonical in aliases.items():
        if re.sub(r"[^a-z0-9]+", "", key) == compact:
            return canonical
    if raw in {"deal stage", "stage name", "stagename"}:
        return "dealstage"
    return raw


def _round_currency(value: Decimal) -> str:
    if value == value.to_integral_value():
        return format(value.quantize(Decimal("1")), ",")
    return format(value.quantize(Decimal("0.01")), ",")


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"", "n/a", "null", "none", "unknown"}:
        return None
    cleaned = text.replace("$", "").replace(",", "").replace("%", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    """Half-open [start, end) bounds of a calendar quarter; quarter may overflow/underflow."""
    index = year * 4 + (quarter - 1)
    year, q = divmod(index, 4)
    start = date(year, q * 3 + 1, 1)
    end_year, end_q = divmod(index + 1, 4)
    return start, date(end_year, end_q * 3 + 1, 1)


def _month_start(year: int, month: int) -> date:
    index = year * 12 + (month - 1)
    year, month0 = divmod(index, 12)
    return date(year, month0 + 1, 1)


def _relative_date_range(
    relative: str, today: date | None = None, timezone: str = "UTC"
) -> tuple[str | None, str | None]:
    """Resolve a relative phrase to ISO [start, end) dates, or (None, None) if unknown."""
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        zone = UTC
    today = today or datetime.now(zone).date()
    text = _normalise_text(relative)
    quarter_match = re.fullmatch(r"q([1-4])(?: (\d{4}))?", text)
    if quarter_match:
        year = int(quarter_match.group(2) or today.year)
        start, end = _quarter_bounds(year, int(quarter_match.group(1)))
    elif "quarter" in text or "qtr" in text or "quatar" in text:
        offset = (
            -1
            if any(word in text for word in ("last", "previous", "past"))
            else (1 if "next" in text else 0)
        )
        start, end = _quarter_bounds(today.year, (today.month - 1) // 3 + 1 + offset)
    elif "year" in text:
        offset = (
            -1
            if any(word in text for word in ("last", "previous", "past"))
            else (1 if "next" in text else 0)
        )
        start, end = date(today.year + offset, 1, 1), date(today.year + offset + 1, 1, 1)
    elif "month" in text:
        offset = (
            -1
            if any(word in text for word in ("last", "previous", "past"))
            else (1 if "next" in text else 0)
        )
        start = _month_start(today.year, today.month + offset)
        end = _month_start(start.year, start.month + 1)
    elif "week" in text:
        monday = today - timedelta(days=today.weekday())
        if any(word in text for word in ("last", "previous", "past")):
            monday -= timedelta(days=7)
        elif "next" in text:
            monday += timedelta(days=7)
        start, end = monday, monday + timedelta(days=7)
    elif text in {"today", "yesterday"}:
        start = today if text == "today" else today - timedelta(days=1)
        end = start + timedelta(days=1)
    else:
        return None, None
    return start.isoformat(), end.isoformat()


def _extract_group_field(message: str, object_name: str) -> str | None:
    text = _normalise_text(message)
    if object_name == "deal" and re.search(
        r"\b(?:compare|comparison|versus|vs)\b", text
    ) and all(re.search(rf"\b{status}\b", text) for status in ("open", "won", "lost")):
        return "dealstatus"
    if re.search(r"\bmonthly\b", text):
        return "month"
    label_breakdown = re.search(
        r"\b(stage|pipeline|owner|month|quarter|year)\s+(?:breakdown|distribution)\b",
        text,
    )
    if label_breakdown:
        return _normalise_field_name(label_breakdown.group(1), object_name)
    known_group = re.search(
        r"\b(?:group(?:ed)?\s+by|breakdown\s+by|by|each|every|per)\s+"
        r"(?:deal\s+)?(stage|pipeline|owner|month|quarter|year)\b",
        text,
    )
    if known_group:
        candidate = known_group.group(1)
        return _normalise_field_name(candidate, object_name)
    for pattern in (
        r"\b(?:by|per|each|every)\s+(month|quarter|year)\b",
        r"groupby\s+([a-z0-9 _-]+)",
        r"group by\s+([a-z0-9 _-]+)",
        r"grouped by\s+([a-z0-9 _-]+)",
        r"breakdown\s+by\s+([a-z0-9 _-]+)",
        r"\b(?:each|every|per)\s+(?:deal\s+)?(stage|pipeline|owner|company|type)\b",
    ):
        match = re.search(pattern, text.rstrip("?.! "))
        if match:
            candidate = match.group(1).strip()
            if candidate:
                aliases = _FIELD_ALIASES.get(object_name, {}).values()
                words = candidate.split()
                for size in range(len(words), 0, -1):
                    field = _normalise_field_name(" ".join(words[:size]), object_name)
                    if field in aliases:
                        return field
                return _normalise_field_name(candidate, object_name)
    if object_name in {"company", "contact"} and re.search(
        r"\b(?:count|counts|number\s+of|group(?:ed)?|breakdown|chart|graph|visuali[sz])\b",
        text,
    ):
        grouped_by = re.search(
            r"\bby\s+(?:the\s+)?([a-z0-9 _-]+?)"
            r"(?=\s+(?:in|as|with|for|over|under|including|chart|graph|visuali[sz])\b|[,;?.!]|$)",
            text,
        )
        if grouped_by:
            candidate = grouped_by.group(1).strip()
            field = _normalise_field_name(candidate, object_name)
            if field in _FIELD_ALIASES.get(object_name, {}).values():
                return field
    return None


def _extract_sort_fields(message: str, object_name: str) -> list[CRMIntelligenceSort]:
    text = _normalise_text(message)
    sorts: list[CRMIntelligenceSort] = []
    date_field = (
        "createdate" if re.search(r"\b(?:creat(?:ed|ion)|createdate)\b", text) else "closedate"
    )
    if re.search(r"\b(?:latest|newest|most recent|recent)\b", text):
        sorts.append(CRMIntelligenceSort(field=date_field, direction="desc"))
    elif re.search(r"\b(?:oldest|earliest)\b", text):
        sorts.append(CRMIntelligenceSort(field=date_field, direction="asc"))
    amount_sort = re.search(r"\b(highest|largest|biggest|lowest|smallest|top)\b", text)
    if amount_sort:
        sorts.append(
            CRMIntelligenceSort(
                field="amount",
                direction="asc" if amount_sort.group(1) in {"lowest", "smallest"} else "desc",
            )
        )
    sort_match = re.search(r"\bsort\s+by\s+([a-z0-9 _-]+?)(?:\s+(asc|desc))?(?:[,;?.!]|$)", text)
    if sort_match:
        field = _normalise_field_name(sort_match.group(1).strip(), object_name)
        direction = sort_match.group(2) or "desc"
        sorts = [CRMIntelligenceSort(field=field, direction=direction)]
    return sorts


def _extract_limit(message: str) -> int | None:
    match = re.search(
        r"(?:top|latest|newest|oldest|earliest|first|bottom|highest|lowest|largest|smallest)"
        r"\s+(\d+)",
        _normalise_text(message),
    )
    if match:
        return int(match.group(1))
    word_limit = re.search(
        r"\b(?:top|first|bottom|highest|lowest|largest|smallest)\s+"
        r"(one|two|three|four|five|six|seven|eight|nine|ten)\b",
        _normalise_text(message),
    )
    if word_limit:
        return {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }[word_limit.group(1)]
    match = re.search(
        r"\b(\d+)\s+(?:most\s+recent|latest|newest|oldest|earliest)\b",
        _normalise_text(message),
    )
    if match:
        return int(match.group(1))
    match = re.search(r"limit\s+(\d+)", _normalise_text(message))
    if match:
        return int(match.group(1))
    return None


def _extract_filters(message: str, object_name: str) -> list[CRMIntelligenceFilter]:
    text = _normalise_text(message)
    filters: list[CRMIntelligenceFilter] = []
    if object_name != "deal":
        return filters
    # Date ranges are carried by CRMQueryPlan.date_range, not by filters.
    if re.search(r"\bopen\b", text):
        filters.append(CRMIntelligenceFilter(field="dealstage", operator="in", value="open"))
    if re.search(r"\bclosed\b", text):
        filters.append(CRMIntelligenceFilter(field="dealstage", operator="in", value="closed"))
    if re.search(r"\bwon\b", text):
        filters.append(CRMIntelligenceFilter(field="dealstage", operator="eq", value="won"))
    if re.search(r"\blost\b", text):
        filters.append(CRMIntelligenceFilter(field="dealstage", operator="eq", value="lost"))

    def capture(pattern: str) -> str | None:
        match = re.search(pattern, text)
        if not match:
            return None
        return match.group(1).strip(" `\"'.,?!")

    stage = capture(
        r"\b(?:deal\s+)?stage\s+(?:is|equals?|=|:)\s+([a-z0-9 _-]+?)"
        r"(?=\s+(?:and|with|for|in|over|under|above|below|pipeline|owner|created|amount)\b|[,;?.!]|$)"
    ) or capture(r"\bin\s+(?!(?:each|every)\b)(?:the\s+)?([a-z0-9 _-]+?)\s+stage\b")
    if stage and stage not in {"open", "closed", "won", "lost"}:
        filters.append(CRMIntelligenceFilter(field="dealstage", value=stage))
    pipeline = capture(
        r"\bpipeline\s+(?:is|equals?|=|:)\s+([a-z0-9 _-]+?)"
        r"(?=\s+(?:and|with|for|over|under|owner|created|amount)\b|[,;?.!]|$)"
    ) or capture(r"\b(?:in|from)\s+(?!(?:each|every)\b)(?:the\s+)?([a-z0-9 _-]+?)\s+pipeline\b")
    if pipeline:
        filters.append(CRMIntelligenceFilter(field="pipeline", value=pipeline))
    deal_name = capture(
        r"\b(?:deal\s+)?name\s+(?:is|equals?|=|contains?)\s+(.+?)"
        r"(?=\s+(?:and|with|in|stage|pipeline)\b|[,;?!]|$)"
    ) or capture(
        r"\b(?:deals?|opportunities)\s+(?:named|called)\s+(.+?)"
        r"(?=\s+(?:and|with|in|stage|pipeline)\b|[,;?!]|$)"
    )
    if deal_name:
        operator = "eq" if re.search(r"\b(?:is|equals?)\b", text) else "contains"
        filters.append(CRMIntelligenceFilter(field="dealname", operator=operator, value=deal_name))
    deal_id = capture(r"\b(?:deal|record)\s+id\s+(?:is|equals?|=|:)?\s*([a-z0-9-]+)\b")
    if deal_id:
        filters.append(CRMIntelligenceFilter(field="id", value=deal_id))
    amount = re.search(
        r"\b(?:amount|deal\s+value|value)\s*(>=|<=|>|<|equals?|is|over|above|greater than|"
        r"at least|under|below|less than|at most)\s*\$?([\d,]+(?:\.\d+)?)\b"
        r"|\b(?:deals?\s+)?(over|above|greater than|at least|under|below|less than|at most)"
        r"\s*\$?([\d,]+(?:\.\d+)?)\b",
        text,
    )
    if amount:
        raw_operator = amount.group(1) or amount.group(3)
        raw_value = amount.group(2) or amount.group(4)
        operator = {
            "=": "eq",
            "is": "eq",
            "equal": "eq",
            "equals": "eq",
            ">": "gt",
            "over": "gt",
            "above": "gt",
            "greater than": "gt",
            ">=": "gte",
            "at least": "gte",
            "<": "lt",
            "under": "lt",
            "below": "lt",
            "less than": "lt",
            "<=": "lte",
            "at most": "lte",
        }[raw_operator]
        filters.append(
            CRMIntelligenceFilter(
                field="amount", operator=operator, value=raw_value.replace(",", "")
            )
        )
    owner_id = capture(
        r"\bowner(?:\s+id)?\s*(?:(?:is|equals?|=|:)\s*)?(\d+)\b"
    ) or capture(r"\bassigned\s+to\s+(?:owner\s+)?(\d+)\b")
    if owner_id:
        filters.append(CRMIntelligenceFilter(field="hubspot_owner_id", value=owner_id))
    return filters


def _extract_aggregations(message: str, object_name: str) -> list[CRMIntelligenceAggregation]:
    text = _normalise_text(message)
    listing_request = bool(
        (
            re.search(
                r"\b(?:show|list|display|find|get|retrieve)\b.*\b(?:deals?|opportunities)\b", text
            )
            and not re.search(
                r"\b(?:count|how many|summary|average|minimum|maximum|highest|lowest|"
                r"sum|total deals?|total amount|deal amount|amount by)\b",
                text,
            )
        )
        or re.search(
            r"\b(?:latest|newest|recent|top|first|highest|lowest|largest|smallest)\b"
            r".*\b(?:deals?|records?|opportunities?)\b"
            r".*\b(?:with|including|showing)\b",
            text,
        )
        or re.search(
            r"\b(?:show|list|give|get|find|retrieve)\b.*"
            r"\b(?:top|latest|newest|oldest|earliest|highest|lowest|largest|smallest)\s+\d+\s+"
            r"(?:hubspot\s+)?(?:deals?|records?|opportunities?)\b",
            text,
        )
    )
    aggs: list[CRMIntelligenceAggregation] = []
    count_requested = bool(
        re.search(
            r"\b(?:count|counts|how many|number of|total(?: number of)? deals?)\b",
            text,
        )
    )
    extreme_requested = bool(
        re.search(
            r"\b(?:average|avg|mean|minimum|min|lowest|smallest|maximum|max|highest|largest|biggest)\b",  # noqa: E501
            text,
        )
    )
    if listing_request and not re.search(
        r"\b(?:total|summary|count|how many|average|min|max)\b",
        text,
    ):
        return aggs
    explicit_amount = re.search(
        r"\b(?:sum|totals?)\b(?!\s+(?:number|count|deals?\b))"
        if extreme_requested
        else r"\b(?:sum|amounts?|revenue|worth|value)\b"
        r"|\btotals?\b(?!\s+(?:number|count|deals?\b))",
        text,
    )
    pipeline_summary = bool(re.search(r"\b(?:summary|pipeline)\b", text))
    if explicit_amount or pipeline_summary or (
        not count_requested and not extreme_requested and "summary" in text
    ):
        aggs.append(CRMIntelligenceAggregation(operation="sum", field="amount"))
    if count_requested:
        aggs.append(
            CRMIntelligenceAggregation(
                operation="count", field="dealname" if object_name == "deal" else "id"
            )
        )
    if re.search(r"\b(?:average|avg|mean)\b", text):
        aggs.append(CRMIntelligenceAggregation(operation="avg", field="amount"))
    if re.search(r"\b(?:minimum|min|lowest|smallest)\b", text):
        aggs.append(CRMIntelligenceAggregation(operation="min", field="amount"))
    if re.search(r"\b(?:maximum|max|highest|largest|biggest)\b", text):
        aggs.append(CRMIntelligenceAggregation(operation="max", field="amount"))
    return aggs


def _extract_date_range(message: str) -> CRMIntelligenceDateRange | None:
    text = _normalise_text(message)
    field = "createdate" if re.search(r"\b(?:creat(?:ed|ion)|createdate)\b", text) else "closedate"
    timezone = "UTC"
    zone_match = re.search(r"\b(?:in|timezone)\s+([a-z]+/[a-z_]+)\b", text)
    if zone_match:
        try:
            ZoneInfo(zone_match.group(1))
            timezone = zone_match.group(1)
        except ZoneInfoNotFoundError:
            pass

    def result(start: date | None, end: date | None, label: str | None = None):
        return CRMIntelligenceDateRange(
            start=start.isoformat() if start else None,
            end=end.isoformat() if end else None,
            relative=label,
            field=field,
            timezone=timezone,
        )

    def parse_explicit(raw: str) -> date | None:
        formats = (
            ("%Y-%m-%d",)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw)
            else ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y")
        )
        for date_format in formats:
            try:
                return datetime.strptime(raw, date_format).date()
            except ValueError:
                continue
        return None

    # Explicit ranges use inclusive natural-language end dates. The analyzer applies
    # half-open bounds, so advance the end by one day here.
    custom = re.search(
        r"\b(?:from|between)\s+([a-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})"
        r"\s+(?:to|and)\s+([a-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})\b",
        text,
    )
    if custom:
        start, inclusive_end = parse_explicit(custom.group(1)), parse_explicit(custom.group(2))
        if start is None or inclusive_end is None:
            return None
        end = inclusive_end + timedelta(days=1)
        if end <= start:
            return None
        return result(start, end)

    explicit = re.search(r"\b(\d{4}-\d{2}-\d{2}|[a-z]{3,9}\s+\d{1,2},?\s+\d{4})\b", text)
    if explicit:
        parsed = parse_explicit(explicit.group(1))
        if parsed is None:
            return None
        if re.search(r"\b(?:before|earlier than)\b", text):
            return result(None, parsed, f"before {parsed.isoformat()}")
        if re.search(r"\b(?:after|later than)\b", text):
            return result(parsed + timedelta(days=1), None, f"after {parsed.isoformat()}")
        if re.search(r"\b(?:since|from)\b", text):
            return result(parsed, None, f"since {parsed.isoformat()}")
        return result(parsed, parsed + timedelta(days=1), parsed.isoformat())

    year_match = re.search(r"\b(?:in|during|for)\s+(20\d{2})\b", text)
    if year_match:
        year = int(year_match.group(1))
        return result(date(year, 1, 1), date(year + 1, 1, 1), str(year))

    # Exact day and named calendar month requests are common CRM questions. Keep
    # parsing deterministic and store both as half-open ISO date bounds.
    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    named_date_match = re.search(r"\b([a-z]{3,9}\s+\d{1,2},?\s+\d{4})\b", text)
    raw_date = (
        iso_match.group(1)
        if iso_match
        else (named_date_match.group(1) if named_date_match else None)
    )
    if raw_date:
        parsed = None
        formats = ["%Y-%m-%d"] if iso_match else ["%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"]
        for date_format in formats:
            try:
                parsed = datetime.strptime(raw_date, date_format).date()
                break
            except ValueError:
                continue
        if parsed:
            return CRMIntelligenceDateRange(
                start=parsed.isoformat(),
                end=(parsed + timedelta(days=1)).isoformat(),
                relative=raw_date,
            )

    month_match = re.search(r"\b([a-z]+)\s+(\d{4})\b", text)
    if month_match:
        try:
            try:
                month_start = (
                    datetime.strptime(f"{month_match.group(1)} {month_match.group(2)}", "%B %Y")
                    .date()
                    .replace(day=1)
                )
            except ValueError:
                month_start = (
                    datetime.strptime(f"{month_match.group(1)} {month_match.group(2)}", "%b %Y")
                    .date()
                    .replace(day=1)
                )
        except ValueError:
            month_start = None
        if month_start:
            month_end = _month_start(month_start.year, month_start.month + 1)
            return result(
                month_start, month_end, f"{month_start.strftime('%B')} {month_start.year}"
            )

    relative: str | None = None
    quarter = re.search(r"\bq([1-4])(?:\s+(\d{4}))?\b", text)
    if quarter:
        relative = f"q{quarter.group(1)}" + (f" {quarter.group(2)}" if quarter.group(2) else "")
    elif "this quarter" in text or "this quatar" in text or "current quarter" in text:
        relative = "this quarter"
    elif "last quarter" in text or "previous quarter" in text or "past quarter" in text:
        relative = "last quarter"
    elif "next quarter" in text:
        relative = "next quarter"
    elif "this month" in text:
        relative = "this month"
    elif "last month" in text:
        relative = "last month"
    elif "past month" in text:
        relative = "last month"
    elif "next month" in text:
        relative = "next month"
    elif "this week" in text:
        relative = "this week"
    elif "last week" in text:
        relative = "last week"
    elif "past week" in text:
        relative = "last week"
    elif "next week" in text:
        relative = "next week"
    elif "this year" in text:
        relative = "this year"
    elif "last year" in text:
        relative = "last year"
    elif "past year" in text:
        relative = "last year"
    elif "next year" in text:
        relative = "next year"
    elif "yesterday" in text:
        relative = "yesterday"
    elif "today" in text:
        relative = "today"
    else:
        days = re.search(r"\b(?:last|past|previous)\s+(\d{1,3})\s+days?\b", text)
        if days:
            relative = f"last {days.group(1)} days"
        else:
            days = re.search(r"\bnext\s+(\d{1,3})\s+days?\b", text)
            if days:
                relative = f"next {days.group(1)} days"
    if relative is None:
        return None
    if relative in {"today", "yesterday"}:
        now = datetime.now(ZoneInfo(timezone)).date()
        start = now if relative == "today" else now - timedelta(days=1)
        return result(start, start + timedelta(days=1), relative)
    days_match = re.fullmatch(r"last (\d+) days", relative)
    if days_match:
        end = datetime.now(ZoneInfo(timezone)).date() + timedelta(days=1)
        return result(end - timedelta(days=int(days_match.group(1))), end, relative)
    days_match = re.fullmatch(r"next (\d+) days", relative)
    if days_match:
        start = datetime.now(ZoneInfo(timezone)).date()
        return result(start, start + timedelta(days=int(days_match.group(1))), relative)
    start_text, end_text = _relative_date_range(relative, timezone=timezone)
    return CRMIntelligenceDateRange(
        start=start_text, end=end_text, relative=relative, field=field, timezone=timezone
    )


def _resolve_object_name(message: str) -> str:
    text = _normalise_text(message)
    if re.search(
        r"\b(?:contact|contacts|person|people|lead|leads|prospect|prospects)\b"
        r"|\bpotential\s+customers?\b",
        text,
    ):
        return "contact"
    if re.search(r"\b(?:company|companies|account|accounts)\b", text):
        return "company"
    if any(word in text for word in ("deal", "opportunity", "pipeline")):
        return "deal"
    return "deal"


def plan_crm_query(message: str) -> CRMQueryPlan:
    object_name = _resolve_object_name(message)
    normalized_message = _normalise_text(message)
    status_comparison = bool(
        object_name == "deal"
        and re.search(r"\b(?:compare|comparison|versus|vs)\b", normalized_message)
        and all(
            re.search(rf"\b{status}\b", normalized_message)
            for status in ("open", "won", "lost")
        )
    )
    group_field = ("dealstatus" if status_comparison else None) or _extract_group_field(
        message, object_name
    ) or (
        "dealstage"
        if object_name == "deal"
        and (
            (
                "pipeline" in _normalise_text(message)
                and any(word in _normalise_text(message) for word in ("summary", "breakdown"))
            )
            or "group" in _normalise_text(message)
            or re.search(r"\b(?:compare|comparison|versus|vs)\b", _normalise_text(message))
            and "stage" in _normalise_text(message)
        )
        else None
    )
    default_sort = []
    if object_name == "deal":
        default_sort = (
            [CRMIntelligenceSort(field="closedate", direction="desc")]
            if "latest" in _normalise_text(message) or "newest" in _normalise_text(message)
            else []
        )
    filters = _extract_filters(message, object_name)
    pipeline_status_summary = bool(
        object_name == "deal"
        and "pipeline" in normalized_message
        and "summary" in normalized_message
        and all(
            re.search(rf"\b{status}\b", normalized_message)
            for status in ("open", "won", "lost")
        )
    )
    if status_comparison or pipeline_status_summary:
        filters = [
            item
            for item in filters
            if not (
                _normalise_field_name(item.field, "deal") == "dealstage"
                and item.value.casefold() in {"open", "won", "lost", "closed"}
            )
        ]
    if object_name == "contact" and re.search(
        r"\b(?:leads?|potential\s+customers?|prospects?)\b", _normalise_text(message)
    ):
        filters = [
            *filters,
            CRMIntelligenceFilter(
                field="lifecyclestage",
                operator="in",
                value="lead,marketingqualifiedlead,salesqualifiedlead",
            ),
        ]
    overdue = object_name == "deal" and re.search(r"\boverdue\b", normalized_message)
    active_pipeline = object_name == "deal" and re.search(
        r"\bactive\s+pipeline\b", normalized_message
    )
    if overdue or active_pipeline:
        today = datetime.now(UTC).date()
        filters = [
            item
            for item in filters
            if not (
                _normalise_field_name(item.field, "deal") == "dealstage"
                and item.value.casefold() in {"won", "lost", "closed"}
            )
        ]
        filters.append(CRMIntelligenceFilter(field="dealstage", operator="eq", value="open"))
        overdue_range = (
            CRMIntelligenceDateRange(
                end=today.isoformat(), relative="before today", field="closedate", timezone="UTC"
            )
            if overdue
            else None
        )
    else:
        overdue_range = None
    plan = CRMQueryPlan(
        object=object_name,
        properties=[
            _normalise_field_name(field, object_name)
            for field in _resolve_property_candidates(message, object_name)
        ],
        filters=filters,
        sort=(
            [CRMIntelligenceSort(field="closedate", direction="asc")]
            if overdue
            else default_sort or _extract_sort_fields(message, object_name)
        ),
        group_by=group_field,
        group_date_field=(
            "createdate"
            if re.search(r"\b(?:creat(?:ed|ion)|createdate)\b", _normalise_text(message))
            else "closedate"
        ),
        aggregations=_extract_aggregations(message, object_name),
        date_range=overdue_range or _extract_date_range(message),
        limit=_extract_limit(message) or 20,
        offset=(
            max(0, (int(match.group(1)) - 1) * (_extract_limit(message) or 20))
            if (match := re.search(r"\bpage\s+(\d+)\b", _normalise_text(message)))
            else 0
        ),
        chart_requested=bool(
            re.search(r"\b(?:chart|graph|visuali[sz]e?)\b", _normalise_text(message))
            or ("pipeline" in _normalise_text(message) and "summary" in _normalise_text(message))
            or (object_name in {"contact", "company"} and group_field is not None)
        ),
        chart_type=_default_chart_type(message, group_field),
    )
    if object_name == "deal" and plan.chart_requested and plan.chart_type is None:
        plan.chart_type = "funnel"
    return plan


def _resolve_property_candidates(message: str, object_name: str) -> list[str]:
    text = _normalise_text(message)
    candidates: list[str] = []
    if object_name == "deal":
        if "amount" in text or "revenue" in text or "total" in text:
            candidates.append("amount")
        if "stage" in text or "pipeline" in text or "status" in text:
            candidates.append("dealstage")
        if "owner" in text:
            candidates.append("hubspot_owner_id")
        if "create date" in text or "creation date" in text or "createdate" in text:
            candidates.append("createdate")
        if any(term in text for term in ("close date", "closedate", "close", "closing")):
            candidates.append("closedate")
        if "pipeline" in text:
            candidates.append("pipeline")
        if "owner" in text:
            candidates.append("hubspot_owner_id")
        if "deal name" in text or "name" in text:
            candidates.append("dealname")
        if not candidates:
            candidates.extend(["dealname", "amount", "dealstage", "closedate"])
    elif object_name == "company":
        candidates.extend(["name", "hubspot_owner_id", "industry", "city", "numberofemployees"])
    else:
        candidates.extend(["firstname", "lastname", "email", "jobtitle", "city"])
    return candidates


def _default_chart_type(message: str, group_field: str | None) -> str | None:
    text = _normalise_text(message)
    explicit = re.search(r"\b(bar|line|pie|doughnut|donut|funnel)\s+chart\b", text)
    if explicit:
        return "doughnut" if explicit.group(1) == "donut" else explicit.group(1)
    if "pipeline" in text or "funnel" in text:
        return "funnel"
    if "trend" in text or "month" in text or "quarter" in text:
        return "line"
    if "distribution" in text or "breakdown" in text or group_field is not None:
        return "bar"
    return None


class CRMIntelligencePlanner:
    def __init__(self, ai_service: Any | None = None) -> None:
        self._ai_service = ai_service

    async def plan(
        self,
        message: str,
        conversation_context: str | None = None,
        property_catalog: list[dict[str, str]] | None = None,
    ) -> CRMQueryPlan:
        follow_up = bool(
            conversation_context
            and re.match(
                r"\s*(?:and\s+)?(?:what about|how about|those|them|that|same|instead|also|"
                r"what if|now|next)\b",
                message,
                re.IGNORECASE,
            )
        )
        deterministic_message = f"{conversation_context}\n{message}" if follow_up else message
        if self._ai_service is not None:
            try:
                result = await self._ai_service.generate(
                    prompt_name="crm-intelligence/v1",
                    variables={
                        "query": message,
                        "conversation_context": conversation_context or "",
                        "property_catalog": property_catalog or [],
                    },
                    output_schema=CRMQueryPlan,
                )
                if isinstance(result, CRMQueryPlan):
                    return result
            except Exception:
                pass
        return plan_crm_query(deterministic_message)


def _normalise_record_value(raw: Any) -> str:
    if raw is None:
        return ""
    return str(raw).strip()


def _get_record_field(record: Any, field_name: str) -> str:
    properties = getattr(record, "properties", {}) or {}
    alias_map = {key.lower(): key for key in properties.keys()}
    candidates = [field_name, field_name.lower(), field_name.replace("_", " ")]
    if field_name in {"dealstage", "deal_stage"}:
        candidates.extend(["dealstage", "stage", "deal_stage", "stagename"])
    if field_name == "hubspot_owner_id":
        candidates.extend(["hubspot_owner_id", "hubspot_owner_id", "owner", "ownerid"])
    for candidate in candidates:
        match = alias_map.get(candidate.lower())
        if match is not None:
            return _normalise_record_value(properties.get(match))
    if field_name == "dealname":
        return _normalise_record_value(properties.get("dealname") or properties.get("name"))
    if field_name == "amount":
        amount_text = properties.get("amount")
        if amount_text is not None:
            return _normalise_record_value(amount_text)
    return ""


class _StageResolver:
    """Maps HubSpot stage ids to labels across every pipeline and classifies open/won/lost."""

    def __init__(self, pipelines: list[HubSpotPipeline] | None) -> None:
        self._pairs: dict[tuple[str, str], HubSpotPipelineStage] = {}
        self._by_id: dict[str, HubSpotPipelineStage] = {}
        self._order: dict[str, int] = {}
        self._pipeline_list = pipelines or []
        self.has_pipelines = bool(pipelines)
        for pipeline in pipelines or []:
            for stage in sorted(pipeline.stages, key=lambda item: item.display_order):
                self._pairs[(pipeline.id, stage.id)] = stage
                self._by_id.setdefault(stage.id, stage)
                self._order.setdefault(stage.label, len(self._order))

    def _stage(self, record: Any) -> HubSpotPipelineStage | None:
        raw = _get_record_field(record, "dealstage")
        if not raw:
            return None
        pipeline_id = _get_record_field(record, "pipeline")
        return self._pairs.get((pipeline_id, raw)) or self._by_id.get(raw)

    def label(self, record: Any) -> str:
        raw = _get_record_field(record, "dealstage")
        if not raw:
            return "Unknown stage"
        stage = self._stage(record)
        if stage is not None:
            return stage.label
        if not self.has_pipelines:
            return raw
        return f"Unknown stage ({raw})"

    def sort_key(self, label: str) -> tuple[int, int, str]:
        if label in self._order:
            return (0, self._order[label], label)
        return (1, 0, label.casefold())

    def stage_matches(self, record: Any, value: str) -> bool:
        actual_id = _get_record_field(record, "dealstage").casefold()
        actual_label = self.label(record).casefold()
        requested = value.strip().casefold()
        return actual_id == requested or actual_label == requested

    def state(self, record: Any) -> str | None:
        stage = self._stage(record)
        label = (
            stage.label if stage is not None else _get_record_field(record, "dealstage")
        ).casefold()
        if stage is not None and str(stage.metadata.get("isClosed", "")).lower() in {
            "true",
            "false",
        }:
            if stage.metadata["isClosed"].lower() == "false":
                return "open"
            probability = _parse_decimal(stage.metadata.get("probability"))
            if probability is not None:
                return "won" if probability >= 1 else "lost"
            return "closed"
        if "won" in label:
            return "won"
        if "lost" in label:
            return "lost"
        return "open" if stage is not None else None


def _coerce_label(value: str | None, *, pipeline: HubSpotPipeline | None = None) -> str:
    if value is None:
        return "Unknown stage"
    clean = value.strip()
    if not clean:
        return "Unknown stage"
    if pipeline is not None:
        for stage in pipeline.stages:
            if stage.id == clean or stage.label.lower() == clean.lower():
                return stage.label
    return clean


def _metric_values(
    records: list[Any], aggregation: CRMIntelligenceAggregation
) -> Decimal | int | None:
    if aggregation.operation == "count":
        return len(records)
    values = [
        value
        for value in (
            _parse_decimal(_get_record_field(record, aggregation.field)) for record in records
        )
        if value is not None
    ]
    if aggregation.operation == "sum":
        return sum(values, Decimal("0"))
    if not values:
        return None
    if aggregation.operation == "avg":
        return sum(values, Decimal("0")) / Decimal(len(values))
    return min(values) if aggregation.operation == "min" else max(values)


def _build_row(
    label: str,
    records: list[Any],
    aggregations: list[CRMIntelligenceAggregation],
) -> dict[str, Any]:
    row: dict[str, Any] = {"label": label, "stage": label, "count": len(records)}
    for aggregation in aggregations:
        key = f"{aggregation.operation}_{aggregation.field}"
        value = _metric_values(records, aggregation)
        if aggregation.operation == "count":
            row[key] = value
            continue
        row[key] = None if value is None else _round_currency(value)
        row[f"{key}_value"] = None if value is None else float(value)
        if key == "sum_amount":
            row["total_amount"] = row[key]
    return row


def _group_records(
    records: list[Any],
    plan: CRMQueryPlan,
    resolver: _StageResolver,
) -> list[tuple[str, list[Any]]]:
    groups: dict[str, list[Any]] = {}
    group_by = _normalise_field_name(plan.group_by or "", "deal")
    pipeline_labels = {pipeline.id: pipeline.label for pipeline in resolver._pipeline_list}
    for record in records:
        if group_by == "dealstage":
            label = resolver.label(record)
        elif group_by == "dealstatus":
            label = (resolver.state(record) or "unknown").title()
        elif group_by == "pipeline":
            pipeline_id = _get_record_field(record, "pipeline")
            label = pipeline_labels.get(pipeline_id, pipeline_id or "Unknown pipeline")
        elif group_by == "hubspot_owner_id":
            label = _get_record_field(record, "hubspot_owner_id") or "Unassigned owner"
        elif group_by in {"month", "quarter", "year"}:
            raw = _get_record_field(record, plan.group_date_field)
            day = _parse_hubspot_date(raw, plan.date_range.timezone if plan.date_range else "UTC")
            if day is None:
                label = "Unknown date"
            elif group_by == "month":
                label = day.strftime("%Y-%m")
            elif group_by == "quarter":
                label = f"{day.year} Q{(day.month - 1) // 3 + 1}"
            else:
                label = str(day.year)
        else:
            label = _get_record_field(record, group_by) or "Unknown"
        groups.setdefault(label, []).append(record)
    if group_by == "dealstage":
        return sorted(groups.items(), key=lambda item: resolver.sort_key(item[0]))
    return sorted(groups.items(), key=lambda item: (item[0] == "Unknown date", item[0].casefold()))


def _aggregate_records(
    records: list[Any],
    plan: CRMQueryPlan,
    *,
    pipelines: list[HubSpotPipeline] | None = None,
) -> list[dict[str, Any]]:
    resolver = _StageResolver(pipelines)
    return [
        _build_row(label, group, plan.aggregations)
        for label, group in _group_records(records, plan, resolver)
    ]


def execute_plan(
    plan: CRMQueryPlan, records: list[Any], *, pipelines: list[HubSpotPipeline] | None = None
) -> dict[str, Any]:
    if plan.group_by is None:
        simplified = []
        for record in records[: plan.limit or len(records)]:
            simplified.append(
                {"id": getattr(record, "id", None), "properties": getattr(record, "properties", {})}
            )
        return {"plan": plan.model_dump(), "rows": simplified}
    rows = _aggregate_records(records, plan, pipelines=pipelines)
    return {
        "plan": plan.model_dump(),
        "rows": rows,
        "summary": {
            "total_rows": len(rows),
            "chart_type": plan.chart_type,
        },
    }


def summarize_pipeline(
    records: list[HubSpotDeal], *, pipelines: list[HubSpotPipeline] | None = None
) -> list[dict[str, Any]]:
    plan = CRMQueryPlan(
        object="deal",
        group_by="dealstage",
        aggregations=[
            CRMIntelligenceAggregation(operation="count", field="dealname"),
            CRMIntelligenceAggregation(operation="sum", field="amount"),
        ],
    )
    return _aggregate_records(records, plan, pipelines=pipelines)


def build_chart_payload(plan: CRMQueryPlan, rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [row.get("label") or row.get("stage") or "Unknown" for row in rows]
    metric_key = "count"
    if plan.aggregations:
        for item in plan.aggregations:
            if item.operation == "count":
                metric_key = "count"
                break
            if item.operation in {"sum", "avg", "min", "max"} and item.field == "amount":
                metric_key = f"{item.operation}_amount"
                break
    if metric_key == "count" and not any(row.get("count") is not None for row in rows):
        if any(row.get("sum_amount") is not None for row in rows):
            metric_key = "sum_amount"
        elif any(row.get("avg_amount") is not None for row in rows):
            metric_key = "avg_amount"
        elif any(row.get("min_amount") is not None for row in rows):
            metric_key = "min_amount"
        elif any(row.get("max_amount") is not None for row in rows):
            metric_key = "max_amount"
    values: list[float] = []
    for row in rows:
        total = row.get(metric_key)
        if total is None and metric_key != "count":
            total = row.get("count", 0)
        if isinstance(total, str):
            total = total.replace(",", "")
        try:
            values.append(float(total))
        except (TypeError, ValueError):
            values.append(0.0)
    config = build_chart_config(
        plan.chart_type or "bar",
        f"CRM {plan.object.title()} summary",
        labels,
        values,
        # "Deal value" would make QuickChart prefix ticks with "$"; avoid assuming a currency.
        value_label="Deal amount" if metric_key != "count" else "Deal count",
    )
    if config.get("type") == "bar" and config.get("options", {}).get("indexAxis") == "y":
        options = config["options"]
        options["plugins"]["legend"] = {"display": False}
        options["scales"]["y"]["title"]["text"] = (
            "Pipeline"
            if _normalise_field_name(plan.group_by or "", plan.object) == "pipeline"
            else "Pipeline stage"
        )
    return {"chart": config, "chart_type": plan.chart_type or "bar"}


_BULLET = "\u2022"
_SEP = " \u00b7 "
_METRIC_LABELS = {"sum": "Total", "avg": "Average", "min": "Minimum", "max": "Maximum"}
_OBJECT_LABELS = {"company": "companies", "contact": "contacts"}


def unsupported_intelligence_message(plan: CRMQueryPlan) -> str | None:
    """Explain why a plan is outside Stage 1 scope, or None when it is supported."""
    if plan.object != "deal":
        noun = _OBJECT_LABELS.get(plan.object, plan.object)
        return (
            f"I can't run analytics on {noun} yet. CRM analytics currently cover deals only: "
            "counts and amounts by stage, pipeline summaries, averages, minimums, maximums, "
            "date ranges, and latest deals."
        )
    supported_groups = {
        "dealstage", "dealstatus", "pipeline", "hubspot_owner_id", "month", "quarter", "year"
    }
    if (
        plan.group_by is not None
        and _normalise_field_name(plan.group_by, "deal") not in supported_groups
    ):
        return (
            f"I can't group deals by {plan.group_by}. "
            "Try stage, pipeline, owner, month, quarter, or year."
        )
    return None


def _parse_hubspot_date(value: str | None, timezone: str = "UTC") -> date | None:
    if not value:
        return None
    try:
        zone = ZoneInfo(timezone)
        if value.isdigit():
            return datetime.fromtimestamp(int(value) / 1000, UTC).astimezone(zone).date()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value and " " not in value:
            return parsed.date()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(zone).date()
    except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
        return None


def _resolved_range(plan: CRMQueryPlan) -> tuple[date | None, date | None, str | None]:
    date_range = plan.date_range
    if date_range is None:
        return None, None, None
    start_text, end_text = date_range.start, date_range.end
    if start_text is None and end_text is None and date_range.relative:
        start_text, end_text = _relative_date_range(
            date_range.relative, timezone=date_range.timezone
        )
    start = _parse_hubspot_date(start_text, date_range.timezone)
    end = _parse_hubspot_date(end_text, date_range.timezone)
    label = date_range.relative
    if label and re.fullmatch(r"q[1-4](?: \d{4})?", label):
        label = label.upper()
    if not label and (start or end):
        label = f"{start or 'start'} to {end - timedelta(days=1) if end else 'now'}"
    return start, end, label


def _currency_note(deals: list[HubSpotDeal]) -> tuple[str | None, str | None]:
    """Return (code, note); a code is only used when every priced deal shares it."""
    codes: set[str] = set()
    missing = False
    for deal in deals:
        if _parse_decimal(_get_record_field(deal, "amount")) is None:
            continue
        code = _get_record_field(deal, "deal_currency_code").upper()
        if code:
            codes.add(code)
        else:
            missing = True
    if len(codes) == 1 and not missing:
        return next(iter(codes)), None
    if len(codes) > 1:
        return None, "Amounts span multiple currencies and are not converted."
    if len(codes) == 1 and missing:
        return None, "Some deal amounts have no currency code; combined amounts are omitted."
    return None, None


def _money(value: Any, currency: str | None) -> str:
    if value is None:
        return "N/A"
    text = value if isinstance(value, str) else _round_currency(Decimal(str(value)))
    return f"{currency} {text}" if currency else text


def _plural(count: int, noun: str = "deal") -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _wanted_metrics(
    plan: CRMQueryPlan, message: str, *, grouped: bool
) -> list[CRMIntelligenceAggregation]:
    aggregations = [item for item in plan.aggregations if item.operation != "count"]
    text = _normalise_text(message)
    pipeline_summary = grouped and (
        "pipeline" in text or "summary" in text or plan.chart_type == "funnel"
    )
    if pipeline_summary and not any(
        item.operation == "sum" and item.field == "amount" for item in aggregations
    ):
        aggregations.insert(0, CRMIntelligenceAggregation(operation="sum", field="amount"))
    return aggregations


def _is_comparison(message: str) -> bool:
    return bool(re.search(r"\b(?:compare|comparison|versus|vs)\b", _normalise_text(message)))


def _comparison_labels(message: str, labels: list[str]) -> list[str]:
    if not _is_comparison(message):
        return []
    text = _normalise_text(message)
    return [label for label in labels if label.casefold() in text]


def _insight(rows: list[dict[str, Any]], currency: str | None, *, comparing: bool) -> str | None:
    if not rows:
        return None
    total_count = sum(row["count"] for row in rows)
    has_amount = any(row.get("sum_amount_value") is not None for row in rows)
    if comparing and len(rows) >= 2:
        first, second = rows[0], rows[1]
        parts = [
            f"{first['label']} has {abs(first['count'] - second['count'])} "
            f"{'more' if first['count'] >= second['count'] else 'fewer'} "
            f"deals than {second['label']}"
        ]
        if has_amount:
            diff = Decimal(str(first.get("sum_amount_value") or 0)) - Decimal(
                str(second.get("sum_amount_value") or 0)
            )
            parts.append(
                f"and a total amount {'higher' if diff >= 0 else 'lower'} by "
                f"{_money(abs(diff), currency)}"
            )
        return " ".join(parts) + "."
    if len(rows) < 2:
        return None
    highest_count = max(row["count"] for row in rows)
    leaders = [row["label"] for row in rows if row["count"] == highest_count]
    if len(leaders) > 1:
        text = (
            f"{' and '.join(leaders)} are tied for the most deals "
            f"({highest_count} of {total_count} each)."
        )
    else:
        text = f"{leaders[0]} has the most deals ({highest_count} of {total_count})."
    if has_amount:
        total_value = sum(Decimal(str(row.get("sum_amount_value") or 0)) for row in rows)
        top = max(rows, key=lambda row: row.get("sum_amount_value") or 0)
        if total_value > 0 and (top.get("sum_amount_value") or 0) > 0:
            share = Decimal(str(top["sum_amount_value"])) / total_value * 100
            text += (
                f" {top['label']} holds the most value "
                f"({_money(top['sum_amount'], currency)}, {share:.1f}% of the total)."
            )
    return text


def _stage_state_matches(resolver: _StageResolver, deal: HubSpotDeal, plan: CRMQueryPlan) -> bool:
    for item in plan.filters:
        values = [value.strip().casefold() for value in item.value.split(",")]
        if (
            _normalise_field_name(item.field, "deal") != "dealstage"
            or item.operator not in {"eq", "in"}
            or not any(value in {"open", "closed", "won", "lost"} for value in values)
        ):
            continue
        state = resolver.state(deal)
        ok = any(
            state in {"won", "lost", "closed"} if requested == "closed" else state == requested
            for requested in values
        )
        if not ok:
            return False
    return True


def _filter_matches(
    deal: HubSpotDeal,
    item: CRMIntelligenceFilter,
    resolver: _StageResolver,
    pipelines: list[HubSpotPipeline] | None,
    timezone: str,
) -> bool:
    field = _normalise_field_name(item.field, "deal")
    requested_values = [part.strip() for part in re.split(r"[,|]", item.value) if part.strip()]
    if not requested_values:
        return False
    actual = _get_record_field(deal, field)
    operator = item.operator
    if field == "dealstage":
        states = {"open", "closed", "won", "lost"}
        requested_states = [
            value.casefold() for value in requested_values if value.casefold() in states
        ]
        if requested_states:
            actual_state = resolver.state(deal)
            matched = any(
                actual_state in {"won", "lost", "closed"}
                if state == "closed"
                else actual_state == state
                for state in requested_states
            )
            return not matched if operator == "neq" else matched
        match = resolver.stage_matches(deal, item.value)
        return not match if operator == "neq" else match
    if field == "id":
        matched = str(deal.id).casefold() in {value.casefold() for value in requested_values}
        return not matched if operator == "neq" else matched
    if field == "pipeline":
        labels = {pipeline.id.casefold(): pipeline.label.casefold() for pipeline in pipelines or []}
        label = labels.get(actual.casefold(), actual).casefold()
        matches = [value.casefold() in {actual.casefold(), label} for value in requested_values]
        matched = any(matches) if operator == "in" else matches[0]
        return not matched if operator == "neq" else matched
    if field in {"amount", "hs_probability", "hs_deal_stage_probability"}:
        value = _parse_decimal(actual)
        target = _parse_decimal(item.value)
        if value is None or target is None:
            return False
        return {
            "eq": value == target,
            "neq": value != target,
            "gt": value > target,
            "gte": value >= target,
            "lt": value < target,
            "lte": value <= target,
            "in": value in [_parse_decimal(candidate) for candidate in requested_values],
            "contains": False,
        }[operator]
    if field in {"closedate", "createdate"}:
        value = _parse_hubspot_date(actual, timezone)
        target = _parse_hubspot_date(item.value, timezone)
        if value is None or target is None:
            return False
        return {
            "eq": value == target,
            "neq": value != target,
            "gt": value > target,
            "gte": value >= target,
            "lt": value < target,
            "lte": value <= target,
            "in": value
            in [_parse_hubspot_date(candidate, timezone) for candidate in requested_values],
            "contains": False,
        }[operator]
    normalized_actual = actual.casefold()
    normalized_requested = [value.casefold() for value in requested_values]
    if operator == "contains":
        return any(value in normalized_actual for value in normalized_requested)
    if operator == "in":
        return normalized_actual in normalized_requested
    if operator in {"eq", "neq"}:
        matched = normalized_actual == normalized_requested[0]
        return not matched if operator == "neq" else matched
    if operator in {"gt", "gte", "lt", "lte"}:
        numeric_actual = _parse_decimal(actual)
        numeric_requested = _parse_decimal(item.value)
        if numeric_actual is not None and numeric_requested is not None:
            return {
                "gt": numeric_actual > numeric_requested,
                "gte": numeric_actual >= numeric_requested,
                "lt": numeric_actual < numeric_requested,
                "lte": numeric_actual <= numeric_requested,
            }[operator]
        actual_date = _parse_hubspot_date(actual, timezone)
        requested_date = _parse_hubspot_date(item.value, timezone)
        if actual_date is not None and requested_date is not None:
            return {
                "gt": actual_date > requested_date,
                "gte": actual_date >= requested_date,
                "lt": actual_date < requested_date,
                "lte": actual_date <= requested_date,
            }[operator]
    return False


def _unsupported_filters(plan: CRMQueryPlan) -> list[str]:
    # Runtime callers validate every field against HubSpot's tenant property catalog.
    return []


def analyze_deals(
    plan: CRMQueryPlan,
    all_deals: list[HubSpotDeal],
    pipelines: list[HubSpotPipeline] | None,
    message: str = "",
) -> tuple[dict[str, Any], str]:
    """Aggregate every matching deal and render the answer; limits never precede aggregation."""
    resolver = _StageResolver(pipelines)
    start, end, range_label = _resolved_range(plan)
    notes: list[str] = []

    deals = list(all_deals)
    undated = 0
    if start is not None or end is not None:
        dated: list[HubSpotDeal] = []
        for deal in deals:
            selected_date_field = plan.date_range.field if plan.date_range else "closedate"
            record_date = _parse_hubspot_date(
                _get_record_field(deal, selected_date_field),
                plan.date_range.timezone if plan.date_range else "UTC",
            )
            if record_date is None:
                undated += 1
            elif (start is None or record_date >= start) and (end is None or record_date < end):
                dated.append(deal)
        deals = dated
        if undated:
            missing_date_label = (
                "creation date"
                if plan.date_range and plan.date_range.field == "createdate"
                else "close date"
            )
            notes.append(
                f"{_plural(undated)} without a {missing_date_label} "
                f"{'was' if undated == 1 else 'were'} excluded from the date filter."
            )
    timezone = plan.date_range.timezone if plan.date_range else "UTC"
    state_filters = [
        item
        for item in plan.filters
        if _normalise_field_name(item.field, "deal") == "dealstage"
        and item.value.casefold() in {"open", "closed", "won", "lost"}
    ]
    regular_filters = [item for item in plan.filters if item not in state_filters]
    deals = [
        deal
        for deal in deals
        if (
            any(
                _filter_matches(deal, item, resolver, pipelines, timezone) for item in state_filters
            )
            if _is_comparison(message) and state_filters
            else _stage_state_matches(resolver, deal, plan)
        )
        and all(
            _filter_matches(deal, item, resolver, pipelines, timezone) for item in regular_filters
        )
    ]
    skipped_filters = _unsupported_filters(plan)
    if skipped_filters:
        notes.append(
            f"These filters are not supported and were not applied: {', '.join(skipped_filters)}."
        )

    scope = f" ({range_label})" if range_label else ""
    grouped = plan.group_by is not None
    metrics = _wanted_metrics(plan, message, grouped=grouped)
    if not grouped and not plan.aggregations:
        mode = "list"
    else:
        mode = "grouped" if grouped else "overall"
    currency, currency_note = _currency_note(deals)
    aggregations = list(plan.aggregations)
    amount_metrics = _wanted_metrics(plan, message, grouped=grouped)
    if currency_note and any(
        item.field == "amount" and item.operation != "count"
        for item in aggregations + amount_metrics
    ):
        notes.append(
            "Amount totals and averages were omitted because currency values aren't comparable."
        )
        aggregations = [
            item for item in aggregations
            if item.field != "amount" or item.operation == "count"
        ]
        amount_metrics = [
            item for item in amount_metrics
            if item.field != "amount" or item.operation == "count"
        ]
    metrics = amount_metrics
    summary: dict[str, Any] = {
        "total_records": len(all_deals),
        "matched_records": len(deals),
        "analyzed_records": len(deals),
        "complete": True,
        "has_more": False,
        "currency": currency,
    }
    missing_amount = sum(
        1 for deal in deals if _parse_decimal(_get_record_field(deal, "amount")) is None
    )
    summary["missing_amount_records"] = missing_amount
    quarter_status_summary = bool(
        grouped
        and "pipeline" in _normalise_text(message)
        and "summary" in _normalise_text(message)
        and all(
            re.search(rf"\b{status}\b", _normalise_text(message))
            for status in ("open", "won", "lost")
        )
    )

    if not all_deals:
        summary["returned_records"] = 0
        return {"rows": [], "summary": summary}, "There are no deals in HubSpot yet."
    if not deals:
        summary["returned_records"] = 0
        return {"rows": [], "summary": summary}, (
            f"No deals matched your request{scope}. HubSpot has {_plural(len(all_deals))} in total."
            + ("".join(f"\n_{note}_" for note in notes))
        )

    lines: list[str] = []
    rows: list[dict[str, Any]]
    if mode == "list":
        sorts = plan.sort or [CRMIntelligenceSort(field="closedate", direction="desc")]

        def sort_value(deal: HubSpotDeal, field: str) -> Any:
            if field == "dealstage":
                return resolver.label(deal).casefold()
            if field == "pipeline":
                pipeline_id = _get_record_field(deal, "pipeline")
                return next(
                    (
                        pipeline.label.casefold()
                        for pipeline in pipelines or []
                        if pipeline.id == pipeline_id
                    ),
                    pipeline_id.casefold(),
                )
            raw = _get_record_field(deal, field)
            if field in {"amount", "hs_probability", "hs_deal_stage_probability"}:
                return _parse_decimal(raw)
            if field in {"closedate", "createdate"}:
                return _parse_hubspot_date(raw, timezone)
            return raw.casefold() if raw else None

        ordered = list(deals)
        for sort in reversed(sorts):
            field = _normalise_field_name(sort.field, "deal")
            present = [deal for deal in ordered if sort_value(deal, field) is not None]
            absent = [deal for deal in ordered if sort_value(deal, field) is None]
            ordered = (
                sorted(
                    present,
                    key=lambda deal: sort_value(deal, field),
                    reverse=sort.direction == "desc",
                )
                + absent
            )
        limit = plan.limit or len(ordered)
        shown = ordered[plan.offset : plan.offset + limit]
        summary["has_more"] = plan.offset + len(shown) < len(ordered)
        summary["offset"] = plan.offset
        summary["page"] = plan.offset // limit + 1
        rows = []
        for deal in shown:
            row = {
                "id": deal.id,
                "dealname": _get_record_field(deal, "dealname") or "Untitled deal",
                "stage": resolver.label(deal),
                "amount": _get_record_field(deal, "amount") or None,
                "deal_currency_code": _get_record_field(deal, "deal_currency_code") or None,
                "closedate": _get_record_field(deal, "closedate") or None,
                "createdate": _get_record_field(deal, "createdate") or None,
                "pipeline": sort_value(deal, "pipeline") or "Unknown pipeline",
                "hubspot_owner_id": _get_record_field(deal, "hubspot_owner_id") or None,
            }
            for prop in plan.properties:
                field = _normalise_field_name(prop, "deal")
                if field == "dealstage":
                    row[field] = resolver.label(deal)
                elif field == "pipeline":
                    row[field] = sort_value(deal, field)
                else:
                    row[field] = _get_record_field(deal, field) or None
            rows.append(row)
        key = _normalise_field_name(sorts[0].field, "deal")
        label = (
            "Latest deals"
            if key in {"closedate", "createdate"} and sorts[0].direction == "desc"
            else (
                "Oldest deals"
                if key in {"closedate", "createdate"}
                else "Top deals by amount"
                if key == "amount" and sorts[0].direction == "desc"
                else "Deals"
            )
        )
        lines.append(f"*{label}*{scope}: showing {len(shown)} of {_plural(len(deals))}")
        for row in rows:
            close_field = plan.date_range.field if plan.date_range else "closedate"
            close = _parse_hubspot_date(row.get(close_field), timezone)
            parts = [row["stage"]]
            if row["amount"] is not None and _parse_decimal(row["amount"]) is not None:
                parts.append(_money(_parse_decimal(row["amount"]), currency))
            if close_field == "createdate":
                readable_date = (
                    f"{close.strftime('%b')} {close.day}, {close.year}" if close else None
                )
                parts.append(f"created {readable_date}" if close else "no creation date")
            else:
                readable_date = (
                    f"{close.strftime('%b')} {close.day}, {close.year}" if close else None
                )
                parts.append(f"closes {readable_date}" if close else "no close date")
            lines.append(f"{_BULLET} *{row['dealname']}*: {_SEP.join(parts)}")
    elif mode == "overall":
        row = _build_row(
            "All deals",
            deals,
            [CRMIntelligenceAggregation(operation="count", field="dealname"), *aggregations],
        )
        rows = [row]
        lines.append(f"*Deal summary*{scope}: {_plural(len(deals))}")
        for aggregation in aggregations:
            if aggregation.operation == "count":
                continue
            value = row.get(f"{aggregation.operation}_{aggregation.field}")
            lines.append(
                f"{_BULLET} {_METRIC_LABELS[aggregation.operation]} amount: "
                f"{_money(value, currency)}"
            )
    else:
        count_only = not metrics
        rows = _aggregate_records(
            deals, plan.model_copy(update={"aggregations": amount_metrics}), pipelines=pipelines
        )
        comparing: list[str] = _comparison_labels(message, [row["label"] for row in rows])
        if len(comparing) >= 2:
            rows = [row for row in rows if row["label"] in comparing]
        group_name = {
            "dealstage": "stage",
            "dealstatus": "status",
            "pipeline": "pipeline",
            "hubspot_owner_id": "owner",
            "month": "month",
            "quarter": "quarter",
            "year": "year",
        }.get(_normalise_field_name(plan.group_by or "dealstage", "deal"), "stage")
        title = "Stage comparison" if len(comparing) >= 2 else f"Deals by {group_name}"
        lines.append(f"*{title}*{scope}: {_plural(sum(row['count'] for row in rows))}")
        for row in rows:
            parts = [_plural(row["count"])]
            for aggregation in metrics:
                value = row.get(f"{aggregation.operation}_{aggregation.field}")
                parts.append(f"{_METRIC_LABELS[aggregation.operation]} {_money(value, currency)}")
            lines.append(f"{_BULLET} *{row['label']}*: {_SEP.join(parts)}")
        if len(rows) > 1 or not count_only:
            selected_labels = {row["label"] for row in rows}
            selected_deals = [
                deal
                for label, group in _group_records(deals, plan, resolver)
                if label in selected_labels
                for deal in group
            ]
            total_row = _build_row(
                "Total",
                selected_deals,
                metrics,
            )
            parts = [_plural(total_row["count"])]
            for aggregation in metrics:
                if aggregation.operation in {"sum"}:
                    total_amount = _money(
                        total_row.get(f"{aggregation.operation}_{aggregation.field}"),
                        currency,
                    )
                    parts.append(f"Total {total_amount}")
            if len(rows) > 1:
                lines.append(f"*Overall*: {_SEP.join(parts)}")
        insight = _insight(rows, currency, comparing=len(comparing) >= 2)
        if insight:
            lines.append(f"_Insight: {insight}_")

    if quarter_status_summary:
        state_groups = {state: [] for state in ("open", "won", "lost")}
        for deal in deals:
            state = resolver.state(deal)
            if state in state_groups:
                state_groups[state].append(deal)
        lines.append("*Pipeline status summary:*")
        total_value = Decimal("0")
        for state, state_deals in state_groups.items():
            value = (
                None
                if currency_note
                else _metric_values(
                    state_deals, CRMIntelligenceAggregation(operation="sum", field="amount")
                )
            )
            if value is not None:
                total_value += value
            lines.append(
                f"{_BULLET} {state.title()}: {_plural(len(state_deals))}; "
                f"value {_money(value, currency)}"
            )
        lines.append(
            f"{_BULLET} Total: {_plural(len(deals))}; "
            f"value {_money(None if currency_note else total_value, currency)}"
        )
        lines.append(
            "Open value is active pipeline; won and lost values are closed historical deals."
        )

    grouped_details: list[dict[str, Any]] = []
    detail_intent = bool(re.search(
        r"\b(?:latest|newest|all)\b|\b(?:including|include|with|details?|records?)\b|"
        r"\bdeal\s+name\b|\bclosing\s+date\b",
        _normalise_text(message),
    ))
    if grouped and detail_intent:
        detail_sort = plan.sort or [CRMIntelligenceSort(field="closedate", direction="desc")]
        detail_deals = list(deals)
        for sort in reversed(detail_sort):
            field = _normalise_field_name(sort.field, "deal")
            def detail_sort_value(deal: HubSpotDeal, sort_field: str = field) -> Any:
                raw = _get_record_field(deal, sort_field)
                if sort_field in {"closedate", "createdate"}:
                    return _parse_hubspot_date(raw, timezone)
                if sort_field == "amount":
                    return _parse_decimal(raw)
                return raw.casefold() if raw else None

            present = [deal for deal in detail_deals if detail_sort_value(deal) is not None]
            absent = [deal for deal in detail_deals if detail_sort_value(deal) is None]
            detail_deals = sorted(
                present, key=detail_sort_value, reverse=sort.direction == "desc"
            ) + absent
        detail_limit = plan.limit or 20
        page_details = detail_deals[plan.offset : plan.offset + detail_limit]
        for deal in page_details:
            grouped_details.append({
                "id": deal.id,
                "dealname": _get_record_field(deal, "dealname") or "Untitled deal",
                "stage": resolver.label(deal),
                "amount": _get_record_field(deal, "amount") or None,
                "deal_currency_code": _get_record_field(deal, "deal_currency_code") or None,
                "closedate": _get_record_field(deal, "closedate") or None,
                "hubspot_owner_id": _get_record_field(deal, "hubspot_owner_id") or None,
            })
        summary["details_shown"] = len(grouped_details)
        summary["details_limit"] = detail_limit
        summary["details_offset"] = plan.offset
        summary["details_total"] = len(detail_deals)
        summary["details_has_more"] = plan.offset + len(grouped_details) < len(detail_deals)

    summary["returned_records"] = len(rows)
    if missing_amount and mode != "list":
        ops = {item.operation for item in plan.aggregations + metrics}
        if "sum" in ops:
            notes.append(
                f"{_plural(missing_amount)} {'has' if missing_amount == 1 else 'have'} no amount "
                f"and {'counts' if missing_amount == 1 else 'count'} as 0 in totals."
            )
        elif ops & {"avg", "min", "max"}:
            notes.append(
                f"{_plural(missing_amount)} without an amount "
                f"{'was' if missing_amount == 1 else 'were'} left out of the amount calculations."
            )
    if currency_note:
        notes.append(currency_note)
    lines.extend(f"_{note}_" for note in notes)
    summary["notes"] = notes
    cleaned = "\n".join(lines).strip().rstrip("\\")
    result = {"rows": rows, "summary": summary}
    if grouped_details:
        result["details"] = grouped_details
        if summary["details_has_more"]:
            next_page = plan.offset // (plan.limit or 20) + 2
            cleaned += (
                f"\nShowing {len(grouped_details)} of {len(deals)} deal records. "
                f"Ask for page {next_page} to view the next records."
            )
    return result, cleaned
