"""Reusable, validated deal filter queries (Phase 1: numeric amount, stage, sort, limit).

The LLM (or the rule-based detector below) produces an untrusted ``DealFilterSpec``.
``build_deal_filter`` validates it against an allowlist, ``to_hubspot_filter_group`` turns it
into HubSpot CRM Search filters and ``apply_deal_filter`` re-checks every deal locally, so a
condition is never silently dropped. Everything here is read-only.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.hubspot.models import HubSpotDeal, HubSpotPipeline, HubSpotPipelineStage

NumericOperator = Literal["gt", "gte", "lt", "lte", "eq", "neq", "between"]
SortDirection = Literal["asc", "desc"]

# Allowlisted deal properties. Anything else is rejected with an explanation.
NUMERIC_PROPERTIES = frozenset({"amount"})
SORT_PROPERTIES = frozenset({"amount", "closedate"})
MAX_FILTER_RESULT_PAGES = 100

_HUBSPOT_OPERATORS = {
    "gt": "GT",
    "gte": "GTE",
    "lt": "LT",
    "lte": "LTE",
    "eq": "EQ",
    "neq": "NEQ",
}
_SYMBOLS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=", "neq": "!="}


class NumericCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property: str = Field(max_length=64)
    operator: NumericOperator
    value: str = Field(max_length=50)
    upper_value: str | None = Field(default=None, max_length=50)


class DealSort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property: str = Field(max_length=64)
    direction: SortDirection = "desc"


class DealFilterSpec(BaseModel):
    """Structured, untrusted deal filters. Conditions are combined with AND."""

    model_config = ConfigDict(extra="forbid")

    conditions: list[NumericCondition] = Field(default_factory=list, max_length=6)
    stages: list[str] = Field(default_factory=list, max_length=10)
    priority: str | None = Field(default=None, max_length=20)
    deal_type: str | None = Field(default=None, max_length=50)
    owner: str | None = Field(default=None, max_length=200)
    pipeline: str | None = Field(default=None, max_length=200)
    unassigned: bool = False
    sort: DealSort | None = None
    limit: int | None = Field(default=None, ge=1, le=100)
    # Conditions the interpreter could not express; the request is refused, never trimmed.
    unsupported: list[str] = Field(default_factory=list, max_length=5)


class DealFilterError(Exception):
    """A filter that cannot be applied. The message is safe to show to the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class NumericFilter:
    property: str
    operator: NumericOperator
    value: Decimal
    upper_value: Decimal | None = None

    def matches(self, candidate: Decimal) -> bool:
        if self.operator == "between":
            assert self.upper_value is not None
            return self.value <= candidate <= self.upper_value
        return {
            "gt": candidate > self.value,
            "gte": candidate >= self.value,
            "lt": candidate < self.value,
            "lte": candidate <= self.value,
            "eq": candidate == self.value,
            "neq": candidate != self.value,
        }[self.operator]

    def describe(self) -> str:
        if self.operator == "between":
            return f"{self.property} between {_money(self.value)} and {_money(self.upper_value)}"
        return f"{self.property} {_SYMBOLS[self.operator]} {_money(self.value)}"


@dataclass(frozen=True)
class DealFilter:
    numeric: tuple[NumericFilter, ...] = ()
    stages: tuple[str, ...] = ()
    priority: str | None = None
    deal_type: str | None = None
    owner: str | None = None
    pipeline: str | None = None
    unassigned: bool = False
    sort_property: str | None = None
    sort_direction: SortDirection = "desc"
    limit: int | None = None

    def describe(self) -> str:
        parts = [item.describe() for item in self.numeric]
        if self.stages:
            parts.append("stage " + " or ".join(self.stages))
        if self.priority:
            parts.append(f"priority = {self.priority}")
        if self.deal_type:
            parts.append(f"deal type = {self.deal_type}")
        if self.owner:
            parts.append(f"owner = {self.owner}")
        if self.pipeline:
            parts.append(f"pipeline = {self.pipeline}")
        if self.unassigned:
            parts.append("owner is unassigned")
        return " and ".join(parts)


def _money(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{value:,.2f}" if value != value.to_integral_value() else f"{int(value):,}"


def _parse_number(raw: str, label: str) -> Decimal:
    # Imported lazily: extraction imports the agent schemas, which import this module.
    from app.agent.extraction import ExtractionValidationError, normalize_amount

    try:
        return Decimal(normalize_amount(raw))
    except (ExtractionValidationError, InvalidOperation) as exc:
        raise DealFilterError(f"I couldn't read '{raw}' as a number for the {label} filter.") from exc


def _normalise_filter_value(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _normalise_priority(value: str | None) -> str:
    if value is None:
        raise DealFilterError(f"I couldn't match the deal priority '{value}'. Supported values: High, Medium, Low.")
    normalised = re.sub(r"[^a-z]", "", _normalise_filter_value(value))
    if normalised in {"high", "priorityhigh", "highpriority"}:
        return "high"
    if normalised in {"medium", "prioritymedium", "mediumpriority"}:
        return "medium"
    if normalised in {"low", "prioritylow", "lowpriority"}:
        return "low"
    raise DealFilterError(f"I couldn't match the deal priority '{value}'. Supported values: High, Medium, Low.")


def _normalise_deal_type(value: str | None) -> str:
    if value is None:
        raise DealFilterError(
            f"I couldn't match the deal type '{value}'. Supported values: New Business, Existing Business."
        )
    normalised = re.sub(r"[^a-z0-9]+", "", _normalise_filter_value(value))
    if normalised in {"newbusiness", "newbusinessdeal"}:
        return "newbusiness"
    if normalised in {"existingbusiness", "existingbusinessdeal"}:
        return "existingbusiness"
    raise DealFilterError(
        f"I couldn't match the deal type '{value}'. Supported values: New Business, Existing Business."
    )


def build_deal_filter(spec: DealFilterSpec) -> DealFilter:
    """Validate an untrusted spec. Raises DealFilterError instead of ignoring anything."""
    if spec.unsupported:
        raise DealFilterError(
            "I can't apply this filter yet: "
            + "; ".join(spec.unsupported)
            + ". I didn't run the query so you don't get unfiltered results."
        )
    numeric: list[NumericFilter] = []
    for condition in spec.conditions:
        name = condition.property.strip().casefold()
        if name not in NUMERIC_PROPERTIES:
            supported = ", ".join(sorted(NUMERIC_PROPERTIES))
            raise DealFilterError(
                f"I can't filter deals by '{condition.property}' yet. "
                f"Supported numeric filters: {supported}."
            )
        value = _parse_number(condition.value, name)
        upper: Decimal | None = None
        if condition.operator == "between":
            if condition.upper_value is None:
                raise DealFilterError("A 'between' filter needs both a lower and an upper value.")
            upper = _parse_number(condition.upper_value, name)
            if upper < value:
                value, upper = upper, value
        numeric.append(NumericFilter(name, condition.operator, value, upper))

    priority_value = spec.priority.strip() if isinstance(spec.priority, str) else spec.priority
    priority = _normalise_priority(priority_value) if priority_value not in (None, "") else None
    deal_type_value = spec.deal_type.strip() if isinstance(spec.deal_type, str) else spec.deal_type
    deal_type = _normalise_deal_type(deal_type_value) if deal_type_value not in (None, "") else None
    owner = spec.owner.strip() if spec.owner is not None else None
    pipeline = spec.pipeline.strip() if spec.pipeline is not None else None
    if owner is not None and not owner:
        raise DealFilterError("The deal owner filter is blank. Please provide an explicit owner name or ID.")
    if pipeline is not None and not pipeline:
        raise DealFilterError("The deal pipeline filter is blank. Please provide an explicit pipeline name.")

    sort_property: str | None = None
    direction: SortDirection = "desc"
    if spec.sort is not None:
        sort_property = spec.sort.property.strip().casefold()
        if sort_property not in SORT_PROPERTIES:
            supported = ", ".join(sorted(SORT_PROPERTIES))
            raise DealFilterError(
                f"I can't sort deals by '{spec.sort.property}' yet. Supported: {supported}."
            )
        direction = spec.sort.direction
    stages = tuple(dict.fromkeys(stage.strip() for stage in spec.stages if stage.strip()))
    return DealFilter(
        tuple(numeric),
        stages,
        priority,
        deal_type,
        owner,
        pipeline,
        spec.unassigned,
        sort_property,
        direction,
        spec.limit,
    )


def resolve_stage_ids(
    names: tuple[str, ...], stages: Mapping[str, HubSpotPipelineStage]
) -> list[str]:
    """Map stage labels (or IDs) to configured stage IDs; unknown or ambiguous stages are rejected."""
    resolved: list[str] = []
    for name in names:
        wanted = name.casefold()
        matches = [
            stage_id
            for stage_id, stage in stages.items()
            if stage.label.casefold() == wanted or stage_id.casefold() == wanted
        ]
        if not matches:
            raise DealFilterError(f"I couldn't find a deal stage named '{name}' in HubSpot.")
        if len(matches) > 1:
            raise DealFilterError(
                f"I found more than one deal stage matching '{name}'. Please also specify the pipeline "
                "or use the exact stage name."
            )
        resolved.extend(matches)
    return list(dict.fromkeys(resolved))


def resolve_pipeline_ids(
    names: tuple[str, ...], pipelines: Mapping[str, HubSpotPipeline]
) -> list[str]:
    """Map pipeline labels or IDs to configured pipeline IDs; unknown pipelines are an error."""
    resolved: list[str] = []
    for name in names:
        wanted = name.casefold()
        matches = [
            pipeline_id
            for pipeline_id, pipeline in pipelines.items()
            if pipeline.label.casefold() == wanted or pipeline_id.casefold() == wanted
        ]
        if not matches:
            raise DealFilterError(f"I couldn't find a deal pipeline named '{name}' in HubSpot.")
        if len(matches) > 1:
            raise DealFilterError(
                f"I found more than one deal pipeline matching '{name}'. Please use the exact pipeline name."
            )
        resolved.extend(matches)
    return list(dict.fromkeys(resolved))


def resolve_owner_ids(names: tuple[str, ...], owners: Mapping[str, str]) -> list[str]:
    """Resolve an explicit owner name or ID to HubSpot owner IDs; ambiguous matches are refused."""
    resolved: list[str] = []
    for name in names:
        wanted = name.casefold()
        matches = [
            owner_id
            for owner_id, owner_name in owners.items()
            if owner_name.casefold() == wanted or owner_id.casefold() == wanted
        ]
        if not matches:
            raise DealFilterError(f"I couldn't find a deal owner named '{name}' in HubSpot.")
        if len(matches) > 1:
            raise DealFilterError(
                f"I found more than one deal owner matching '{name}'. Please use the exact owner name."
            )
        resolved.extend(matches)
    return list(dict.fromkeys(resolved))


def to_hubspot_filter_group(
    flt: DealFilter, stage_ids: list[str], *, pipeline_ids: list[str] | None = None, owner_ids: list[str] | None = None
) -> list[dict[str, object]]:
    """One AND-ed HubSpot Search filter group (HubSpot allows up to 6 filters per group)."""
    filters: list[dict[str, object]] = []
    for item in flt.numeric:
        if item.operator == "between":
            filters.append(
                {
                    "propertyName": item.property,
                    "operator": "BETWEEN",
                    "value": format(item.value, "f"),
                    "highValue": format(item.upper_value, "f"),
                }
            )
        else:
            filters.append(
                {
                    "propertyName": item.property,
                    "operator": _HUBSPOT_OPERATORS[item.operator],
                    "value": format(item.value, "f"),
                }
            )
    if stage_ids:
        filters.append({"propertyName": "dealstage", "operator": "IN", "values": stage_ids})
    if pipeline_ids:
        filters.append({"propertyName": "pipeline", "operator": "IN", "values": pipeline_ids})
    if flt.priority:
        filters.append({"propertyName": "hs_priority", "operator": "EQ", "value": flt.priority.lower()})
    if flt.deal_type:
        filters.append({"propertyName": "dealtype", "operator": "EQ", "value": flt.deal_type})
    if owner_ids:
        filters.append({"propertyName": "hubspot_owner_id", "operator": "IN", "values": owner_ids})
    if flt.unassigned:
        filters.append({"propertyName": "hubspot_owner_id", "operator": "HAS_NO_VALUE"})
    if not filters:
        raise DealFilterError("There was no filter condition to apply.")
    if len(filters) > 6:
        raise DealFilterError("That request has too many filter conditions; please simplify it.")
    return [{"filters": filters}]


def deal_amount(deal: HubSpotDeal) -> Decimal | None:
    """The deal's amount, or None when it is missing or not a finite number."""
    raw = deal.properties.get("amount")
    if raw is None or not raw.strip():
        return None
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _close_date(deal: HubSpotDeal) -> datetime | None:
    raw = deal.properties.get("closedate")
    if not raw:
        return None
    try:
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw) / 1000, UTC)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def apply_deal_filter(
    deals: list[HubSpotDeal],
    flt: DealFilter,
    stage_ids: list[str] | None = None,
    pipeline_ids: list[str] | None = None,
    owner_ids: list[str] | None = None,
) -> list[HubSpotDeal]:
    """Filter, sort and limit deals. Missing or invalid numeric values never match."""
    selected: list[HubSpotDeal] = []
    owner_lookup = {owner_id.casefold(): owner_id for owner_id in (owner_ids or [])}
    for deal in deals:
        if stage_ids and (deal.properties.get("dealstage") or "") not in stage_ids:
            continue
        if pipeline_ids and (deal.properties.get("pipeline") or "") not in pipeline_ids:
            continue
        if flt.priority:
            priority_value = (deal.properties.get("hs_priority") or deal.properties.get("priority") or "")
            if _normalise_filter_value(priority_value) not in {flt.priority, f"priority:{flt.priority}"}:
                continue
        if flt.deal_type:
            deal_type_value = deal.properties.get("dealtype") or ""
            if _normalise_deal_type(deal_type_value) != flt.deal_type:
                continue
        if flt.owner:
            owner_value = deal.properties.get("hubspot_owner_id") or ""
            if flt.owner.casefold() not in {owner_value.casefold(), *owner_lookup.keys()}:
                continue
        if flt.unassigned and (deal.properties.get("hubspot_owner_id") or "").strip():
            continue
        if owner_ids and (deal.properties.get("hubspot_owner_id") or "") not in owner_ids:
            continue
        if flt.numeric:
            amount = deal_amount(deal)
            if amount is None or not all(item.matches(amount) for item in flt.numeric):
                continue
        selected.append(deal)

    if flt.sort_property is not None:
        key: Callable[[HubSpotDeal], Decimal | datetime | None] = (
            deal_amount if flt.sort_property == "amount" else _close_date
        )
        present = [deal for deal in selected if key(deal) is not None]
        missing = [deal for deal in selected if key(deal) is None]
        present.sort(key=lambda deal: key(deal), reverse=flt.sort_direction == "desc")  # type: ignore[arg-type,return-value]
        selected = present + missing
    if flt.limit is not None:
        selected = selected[: flt.limit]
    return selected


# --- Rule-based detection -------------------------------------------------------------

_AMOUNT = r"(?:[$€£₹]\s*)?\d[\d,]*(?:\.\d+)?\s*(?:k|m|thousand|million)?\b"
_UNIT_AFTER = r"(?!\s*(?:days?|weeks?|months?|years?|%|percent|employees?|contacts?|deals?))"
_BETWEEN = re.compile(
    rf"\bbetween\s+(?P<low>{_AMOUNT})\s+(?:and|to)\s+(?P<high>{_AMOUNT})", re.IGNORECASE
)
_OPERATORS = re.compile(
    r"(?P<gte>>=|≥|greater\s+than\s+or\s+equal\s+to|at\s+least|no\s+less\s+than"
    r"|not\s+less\s+than|minimum\s+of)"
    r"|(?P<lte><=|≤|less\s+than\s+or\s+equal\s+to|at\s+most|no\s+more\s+than"
    r"|not\s+more\s+than|up\s+to|maximum\s+of)"
    r"|(?P<neq>!=|not\s+equal\s+to|other\s+than)"
    r"|(?P<gt>>|greater\s+than|more\s+than|higher\s+than|larger\s+than|bigger\s+than"
    r"|exceed(?:s|ing)?|above|over)"
    r"|(?P<lt><|less\s+than|lower\s+than|smaller\s+than|below|under)"
    r"|(?P<eq>=|equal\s+to|equals?|exactly)",
    re.IGNORECASE,
)
_OPERATOR_VALUE = re.compile(
    rf"(?:{_OPERATORS.pattern})\s*(?P<value>{_AMOUNT}){_UNIT_AFTER}", re.IGNORECASE
)
_AMOUNT_WORDS = re.compile(r"\b(?:amount|worth|value|valued|revenue|size|price)\b", re.IGNORECASE)
_DEAL_WORDS = re.compile(r"\b(?:deals?|(?:opportunit|oppurtunit)(?:y|ies)|pipeline)\b", re.IGNORECASE)
_TOP_N = re.compile(r"\b(?:top|first)\s+(\d{1,3})\b", re.IGNORECASE)
_HIGHEST = re.compile(
    r"\b(?:highest|largest|biggest|most\s+valuable|highest[- ]value|top)\b", re.IGNORECASE
)
_LOWEST = re.compile(r"\b(?:lowest|smallest|least\s+valuable|lowest[- ]value)\b", re.IGNORECASE)
_READ_VERBS = re.compile(
    r"\b(?:show|list|display|get|view|find|which|what|how\s+many|give\s+me|top)\b", re.IGNORECASE
)
_WRITE_VERBS = re.compile(
    r"\b(?:create|add|update|set|change|delete|remove|associate|link)\b", re.IGNORECASE
)
_OR_CONDITIONS = re.compile(r"\b(?:or)\b", re.IGNORECASE)


def _operator_name(match: re.Match[str]) -> str:
    return next(name for name in ("gte", "lte", "neq", "gt", "lt", "eq") if match.group(name))


def _plausible_amount(raw: str, has_amount_word: bool) -> bool:
    text = raw.strip().casefold()
    digits = re.sub(r"[^\d.]", "", re.sub(r"(k|m|thousand|million)$", "", text))
    if not digits:
        return False
    marked = bool(re.search(r"[$€£₹]|k$|m$|thousand|million|,", text))
    return has_amount_word or marked or Decimal(digits.split(".")[0] or "0") >= 100


def detect_deal_filter(message: str) -> DealFilterSpec | None:
    """Read numeric amount conditions, top-N and value ordering from a deal question.

    Returns None when the message is not a deal read request or has no recognisable filter.
    """
    if not _DEAL_WORDS.search(message) or _WRITE_VERBS.search(message):
        return None
    if not _READ_VERBS.search(message):
        return None
    has_amount_word = bool(_AMOUNT_WORDS.search(message))
    conditions: list[NumericCondition] = []
    remaining = message

    between = _BETWEEN.search(remaining)
    if between and _plausible_amount(between["low"], True):
        conditions.append(
            NumericCondition(
                property="amount",
                operator="between",
                value=between["low"].strip(),
                upper_value=between["high"].strip(),
            )
        )
        remaining = remaining[: between.start()] + " " + remaining[between.end() :]

    for match in _OPERATOR_VALUE.finditer(remaining):
        if not _plausible_amount(match["value"], has_amount_word):
            continue
        conditions.append(
            NumericCondition(
                property="amount",
                operator=_operator_name(match),  # type: ignore[arg-type]
                value=match["value"].strip(),
            )
        )

    sort: DealSort | None = None
    limit: int | None = None
    top = _TOP_N.search(message)
    if top:
        limit = max(1, min(100, int(top.group(1))))
    if _LOWEST.search(message):
        sort = DealSort(property="amount", direction="asc")
    elif _HIGHEST.search(message) and (top or has_amount_word or "value" in message.casefold()):
        sort = DealSort(property="amount", direction="desc")

    stages: list[str] = []
    stage_patterns = (
        re.compile(
            r"\b(?:in|at|on|for)\s+(?:the\s+)?(?P<name>[A-Za-z0-9][A-Za-z0-9&'./() -]{0,80}?)\s+stage\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:stage|stages)\s*(?:is|=|:)?\s*(?P<name>[A-Za-z0-9][A-Za-z0-9&'./() -]{0,80}?)(?=\s*(?:$|[.?!,]|\b(?:and|with|where|that|for|or|but)\b))",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:in|at|on)\s+(?:the\s+)?(?P<name>[A-Z][A-Za-z0-9&'./() -]{1,80}?)(?=\s*(?:$|[.?!,]|\b(?:and|with|where|that|for|or|but)\b))",
            re.IGNORECASE,
        ),
    )
    for pattern in stage_patterns:
        for match in pattern.finditer(message):
            name = (match.group("name") or "").strip()
            if not name:
                continue
            name = re.sub(r"\s+(?:stage|stages)\s*$", "", name, flags=re.IGNORECASE)
            name = re.sub(r"^(?:the|a|an)\s+", "", name, flags=re.IGNORECASE)
            name = re.split(r"\s+(?:and|with|where|that|for|or|but)\b", name, maxsplit=1)[0].strip()
            if not name:
                continue
            if re.match(r"^(?:and|with|where|that|for|or|but)\b", name, re.IGNORECASE):
                continue
            if re.fullmatch(r"(?:all|every|open|closed|won|lost|deal|deals|opportunity|opportunities|pipeline)", name, re.IGNORECASE):
                continue
            if name.casefold() in {"open", "closed", "won", "lost", "high", "medium", "low", "new business", "existing business"}:
                continue
            stages.append(name)
    stages = list(dict.fromkeys(stage.strip() for stage in stages if stage and stage.strip()))

    if not conditions and sort is None and limit is None:
        priority_match = re.search(
            r"\b(?:high|medium|low)\b.*\b(?:priority|priorit(?:y|ies))\b|\b(?:priority|priorit(?:y|ies))\b.*\b(?:high|medium|low)\b",
            message,
            re.IGNORECASE,
        )
        deal_type_match = re.search(
            r"\b(?:new|existing)\s*[- ]?business\b|\b(?:deal\s+type|type)\b.*?\b(?:new|existing)\s*[- ]?business\b",
            message,
            re.IGNORECASE,
        )
        pipeline_match = re.search(
            r"\b(?:pipeline|in\s+pipeline)\b.*?(?:named\s+)?([A-Za-z0-9][A-Za-z0-9 _-]{1,80})",
            message,
            re.IGNORECASE,
        )
        owner_match = re.search(
            r"\b(?:owner|assigned\s+to|owned\s+by)\b\s*(?:is|=|:)?\s*([A-Za-z][A-Za-z' .-]{1,80})",
            message,
            re.IGNORECASE,
        )
        if not any(
            (
                priority_match,
                deal_type_match,
                pipeline_match,
                owner_match,
                bool(stages),
                re.search(r"\b(?:unassigned|no owner|owner is blank|no one assigned)\b", message, re.IGNORECASE),
            )
        ):
            return None
    priority_match = re.search(
        r"\b(?:high|medium|low)\b.*\b(?:priority|priorit(?:y|ies))\b|\b(?:priority|priorit(?:y|ies))\b.*\b(?:high|medium|low)\b",
        message,
        re.IGNORECASE,
    )
    deal_type_match = re.search(
        r"\b(?:new|existing)\s*[- ]?business\b|\b(?:deal\s+type|type)\b.*?\b(?:new|existing)\s*[- ]?business\b",
        message,
        re.IGNORECASE,
    )
    pipeline_match = re.search(
        r"\b(?:pipeline|in\s+pipeline)\b.*?(?:named\s+)?([A-Za-z0-9][A-Za-z0-9 _-]{1,80})",
        message,
        re.IGNORECASE,
    )
    owner_match = re.search(
        r"\b(?:owner|assigned\s+to|owned\s+by)\b\s*(?:is|=|:)?\s*([A-Za-z][A-Za-z' .-]{1,80})",
        message,
        re.IGNORECASE,
    )
    unassigned = bool(re.search(r"\b(?:unassigned|no owner|owner is blank|no one assigned)\b", message, re.IGNORECASE))
    unsupported: list[str] = []
    if len(conditions) > 1 and _OR_CONDITIONS.search(message):
        unsupported.append("conditions combined with OR (only AND is supported)")
    priority_value = re.search(r"\b(high|medium|low)\b", message, re.IGNORECASE)
    deal_type_value = re.search(
        r"\b(new\s*[- ]?business|existing\s*[- ]?business)\b",
        message,
        re.IGNORECASE,
    )
    return DealFilterSpec(
        conditions=conditions[:6],
        stages=list(dict.fromkeys(stages)),
        priority=(priority_value.group(1).lower() if priority_match and priority_value else None),
        deal_type=(deal_type_value.group(0) if deal_type_match and deal_type_value else None),
        owner=(owner_match.group(1).strip() if owner_match else None),
        pipeline=(pipeline_match.group(1).strip() if pipeline_match else None),
        unassigned=unassigned,
        sort=sort,
        limit=limit,
        unsupported=unsupported,
    )


def deal_status_query(message: str) -> str:
    """The existing deal-list query name that matches the status words in a message."""
    text = message.casefold()
    if re.search(r"\bclosed[- ]won\b|\bwon\b", text):
        return "closed_won_deals"
    if re.search(r"\bclosed[- ]lost\b|\blost\b", text):
        return "closed_lost_deals"
    if re.search(r"\bclosed\b", text):
        return "closed_deals"
    if re.search(r"\bopen\b", text):
        return "open_deals"
    return "all_deals"
