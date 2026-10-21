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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.integrations.hubspot.models import HubSpotDeal, HubSpotPipeline, HubSpotPipelineStage

NumericOperator = Literal["gt", "gte", "lt", "lte", "eq", "neq", "between"]
SortDirection = Literal["asc", "desc"]
DealStatus = Literal["open", "closed", "closed_won", "closed_lost"]

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
_CANONICAL_DEAL_FILTER_FIELDS = frozenset(
    {
        "conditions",
        "stages",
        "status",
        "priority",
        "deal_type",
        "owner",
        "pipeline",
        "unassigned",
        "sort",
        "limit",
        "unsupported",
    }
)
_CHART_METADATA_KEYS = frozenset(
    {
        "output",
        "visualization",
        "chart_requested",
        "chart_type",
        "chart",
    }
)
_STATUS_ALIASES = {
    "stage_status": "status",
    "stage_state": "status",
    "stage_type": "status",
}


def _normalise_deal_filter_payload(data: object) -> object:
    """Accept only canonical CRM deal filter keys and normalize known aliases."""
    if not isinstance(data, dict):
        return data

    normalised: dict[str, object] = {}
    has_explicit_status = "status" in data
    has_explicit_sort = "sort" in data
    if "sort_by" in data or "sort_order" in data:
        if not has_explicit_sort:
            sort_by = data.get("sort_by")
            sort_order = data.get("sort_order")
            existing_sort = data.get("sort") if "sort" in data else None
            if isinstance(existing_sort, dict):
                normalised["sort"] = dict(existing_sort)
            elif isinstance(existing_sort, str):
                normalised["sort"] = existing_sort
            if sort_by is not None and isinstance(normalised.get("sort"), dict):
                normalised["sort"]["property"] = str(sort_by)
            elif sort_by is not None:
                normalised["sort"] = {"property": str(sort_by)}
            if sort_order is not None and isinstance(normalised.get("sort"), dict):
                normalised["sort"]["direction"] = str(sort_order)

    for key, value in data.items():
        if key in {"sort_by", "sort_order"} and has_explicit_sort:
            continue
        if key in _STATUS_ALIASES and has_explicit_status:
            continue
        if value is None and key not in _CANONICAL_DEAL_FILTER_FIELDS:
            continue
        if key in _CHART_METADATA_KEYS:
            continue

        canonical_key = _STATUS_ALIASES.get(key, key)
        if canonical_key not in _CANONICAL_DEAL_FILTER_FIELDS:
            raise ValueError(
                "Unsupported deal filter field "
                f"'{key}'. Only canonical CRM filter fields are allowed."
            )

        if canonical_key == "status":
            if "status" in normalised and value is not None:
                continue
            if isinstance(value, str):
                compact = re.sub(r"[^a-z]", "", value.casefold())
                if compact in {"open", "stageopen"}:
                    normalised["status"] = "open"
                    continue
                if compact in {"closed", "stageclosed"}:
                    normalised["status"] = "closed"
                    continue
                if compact in {"closedwon", "won"}:
                    normalised["status"] = "closed_won"
                    continue
                if compact in {"closedlost", "lost"}:
                    normalised["status"] = "closed_lost"
                    continue
                if compact in {"all", "any", "total"}:
                    normalised["status"] = None
                    continue
            normalised["status"] = value
            continue

        normalised[canonical_key] = value

    raw_sort = normalised.get("sort")
    if isinstance(raw_sort, str):
        match = re.fullmatch(
            r"(?P<property>amount|closedate|close_date|close-date|deal_amount|deal-amount)"
            r"(?:[-_: ])?(?P<direction>asc|desc)",
            raw_sort.strip(),
            flags=re.IGNORECASE,
        )
        if match:
            prop = match.group("property").lower().replace("-", "_").replace(" ", "_")
            normalised["sort"] = {
                "property": "amount" if prop in {"amount", "deal_amount"} else "closedate",
                "direction": match.group("direction").lower(),
            }
    elif isinstance(raw_sort, dict):
        property_name = str(raw_sort.get("property") or raw_sort.get("field") or "").strip()
        direction = str(raw_sort.get("direction") or raw_sort.get("order") or "").strip().lower()
        if property_name and direction and direction in {"asc", "desc"}:
            normalised["sort"] = {"property": property_name, "direction": direction}

    return normalised


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
    status: DealStatus | None = None
    priority: str | None = Field(default=None, max_length=20)
    deal_type: str | None = Field(default=None, max_length=50)
    owner: str | None = Field(default=None, max_length=200)
    pipeline: str | None = Field(default=None, max_length=200)
    unassigned: bool = False
    sort: DealSort | None = None
    limit: int | None = Field(default=None, ge=1, le=100)
    # Conditions the interpreter could not express; the request is refused, never trimmed.
    unsupported: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="before")
    @classmethod
    def _strip_non_crm_metadata(cls, data: object) -> object:
        """Normalize allowed CRM filter aliases and reject unsupported raw AI metadata."""
        return _normalise_deal_filter_payload(data)


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
        raise DealFilterError(
            f"I couldn't read '{raw}' as a number for the {label} filter."
        ) from exc


def _normalise_filter_value(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _normalise_priority(value: str | None) -> str:
    if value is None:
        raise DealFilterError(
            f"I couldn't match the deal priority '{value}'. "
            "Supported values: High, Medium, Low."
        )
    normalised = re.sub(r"[^a-z]", "", _normalise_filter_value(value))
    if normalised in {"high", "priorityhigh", "highpriority"}:
        return "high"
    if normalised in {"medium", "prioritymedium", "mediumpriority"}:
        return "medium"
    if normalised in {"low", "prioritylow", "lowpriority"}:
        return "low"
    raise DealFilterError(
        f"I couldn't match the deal priority '{value}'. "
        "Supported values: High, Medium, Low."
    )


def _normalise_deal_type(value: str | None) -> str:
    if value is None:
        raise DealFilterError(
            f"I couldn't match the deal type '{value}'. "
            "Supported values: New Business, Existing Business."
        )
    normalised = re.sub(r"[^a-z0-9]+", "", _normalise_filter_value(value))
    if normalised in {"newbusiness", "newbusinessdeal"}:
        return "newbusiness"
    if normalised in {"existingbusiness", "existingbusinessdeal"}:
        return "existingbusiness"
    raise DealFilterError(
        f"I couldn't match the deal type '{value}'. "
        "Supported values: New Business, Existing Business."
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
        raise DealFilterError("The deal owner filter is blank. Please provide an explicit owner name or ID.")  # noqa: E501
    if pipeline is not None and not pipeline:
        raise DealFilterError("The deal pipeline filter is blank. Please provide an explicit pipeline name.")  # noqa: E501

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
    """Map stage labels (or IDs) to configured stage IDs; unknown or ambiguous stages are rejected."""  # noqa: E501
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
                f"I found more than one deal stage matching '{name}'. Please also specify the pipeline "  # noqa: E501
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
                f"I found more than one deal pipeline matching '{name}'. Please use the exact pipeline name."  # noqa: E501
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
                f"I found more than one deal owner matching '{name}'. Please use the exact owner name."  # noqa: E501
            )
        resolved.extend(matches)
    return list(dict.fromkeys(resolved))


def to_hubspot_filter_group(
    flt: DealFilter, stage_ids: list[str], *, pipeline_ids: list[str] | None = None, owner_ids: list[str] | None = None  # noqa: E501
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
        filters.append({"propertyName": "hs_priority", "operator": "EQ", "value": flt.priority.lower()})  # noqa: E501
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
            priority_value = (deal.properties.get("hs_priority") or deal.properties.get("priority") or "")  # noqa: E501
            if _normalise_filter_value(priority_value) not in {flt.priority, f"priority:{flt.priority}"}:  # noqa: E501
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
        present.sort(key=lambda deal: key(deal), reverse=flt.sort_direction == "desc")  # type: ignore[arg-type,return-value]  # noqa: E501
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
_DEAL_WORDS = re.compile(r"\b(?:deals?|(?:opportunit|oppurtunit)(?:y|ies)|pipeline)\b", re.IGNORECASE)  # noqa: E501
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
_CHART_REQUEST = re.compile(
    r"\b(?:bar|doughnut|pie|line|funnel)\s+chart\b|"
    r"\b(?:as|in)\s+(?:a|an)?\s*(?:bar|doughnut|pie|line|funnel)\s+chart\b|"
    r"\b(?:sales\s+pipeline|pipeline)\s+(?:as|in)\s+(?:a|an)?\s*(?:bar|doughnut|pie|line|funnel|chart)\s*(?:chart)?\b|"  # noqa: E501
    r"\b(?:as|in)\s+(?:a|an)?\s*chart\b|"
    r"\b(?:sales\s+pipeline|pipeline)\s+(?:as|in)\s+(?:a|an)?\s*chart\b",
    re.IGNORECASE,
)


def _strip_visualisation_noise(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(
        r"^\s*(?:sales\s+)?pipeline\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s+(?:as|in)\s+(?:a|an)?\s*(?:bar|line|pie|doughnut|donut|funnel|chart|graph|visualization|visualisation)\s*(?:chart|graph)?\s*$",  # noqa: E501
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:sales\s+pipeline|pipeline)\s+(?:as|in)\s+(?:a|an)?\s*(?:bar|line|pie|doughnut|donut|funnel|chart|graph|visualization|visualisation)\s*(?:chart|graph)?\s*$",  # noqa: E501
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+\b(?:pipeline)\b\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:the|a|an|as|in)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:chart|graph|visualization|visualisation)\s*$", "", text, flags=re.IGNORECASE)  # noqa: E501
    return text.strip()


def _operator_name(match: re.Match[str]) -> str:
    return next(name for name in ("gte", "lte", "neq", "gt", "lt", "eq") if match.group(name))


def _plausible_amount(raw: str, has_amount_word: bool) -> bool:
    text = raw.strip().casefold()
    digits = re.sub(r"[^\d.]", "", re.sub(r"(k|m|thousand|million)$", "", text))
    if not digits:
        return False
    marked = bool(re.search(r"[$€£₹]|k$|m$|thousand|million|,", text))
    return has_amount_word or marked or Decimal(digits.split(".")[0] or "0") >= 100


def _looks_like_relative_date(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    return bool(
        re.fullmatch(r"q[1-4](?:\s+\d{4})?", text, re.IGNORECASE)
        or re.fullmatch(r"(?:this|last|previous|current)\s+(?:quarter|month|week|year)", text, re.IGNORECASE)  # noqa: E501
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", text)
        or re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", text)
    )


def detect_deal_filter(message: str) -> DealFilterSpec | None:
    """Read numeric amount conditions, top-N and value ordering from a deal question.

    Returns None when the message is not a deal read request or has no recognisable filter.
    """
    if _CHART_REQUEST.search(message):
        return None
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
            r"\b(?:in|at|on|for)\s+(?:the\s+)?(?P<name>[A-Za-z0-9][A-Za-z0-9&'./() -]{0,80}?)\s+stage\b",  # noqa: E501
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:stage|stages)\s*(?:is|=|:)?\s*(?P<name>[A-Za-z0-9][A-Za-z0-9&'./() -]{0,80}?)(?=\s*(?:$|[.?!,]|\b(?:and|with|where|that|for|or|but)\b))",  # noqa: E501
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:in|at|on)\s+(?:the\s+)?(?P<name>[A-Z][A-Za-z0-9&'./() -]{1,80}?)(?=\s*(?:$|[.?!,]|\b(?:and|with|where|that|for|or|but)\b))",  # noqa: E501
            re.IGNORECASE,
        ),
    )
    for pattern in stage_patterns:
        for match in pattern.finditer(message):
            if re.search(r"\bpipeline\b", match.group(0), flags=re.IGNORECASE):
                continue
            name = (match.group("name") or "").strip()
            if not name:
                continue
            name = re.sub(r"\s+(?:stage|stages)\s*$", "", name, flags=re.IGNORECASE)
            name = _strip_visualisation_noise(name)
            name = re.sub(r"^(?:the|a|an|as|in)\s+", "", name, flags=re.IGNORECASE)
            name = re.split(r"\s+(?:and|with|where|that|for|or|but)\b", name, maxsplit=1)[0].strip()
            if not name:
                continue
            if re.match(r"^(?:and|with|where|that|for|or|but)\b", name, re.IGNORECASE):
                continue
            if re.fullmatch(r"(?:all|every|open|closed|won|lost|deal|deals|opportunity|opportunities|pipeline)", name, re.IGNORECASE):  # noqa: E501
                continue
            if re.search(r"\bpipeline\b", name, flags=re.IGNORECASE):
                continue
            if re.search(r"\b(?:chart|graph|visualization|visualisation)\b", name, flags=re.IGNORECASE):  # noqa: E501
                continue
            if _looks_like_relative_date(name):
                continue
            if re.fullmatch(r"[a-z]", name, re.IGNORECASE):
                continue
            if name.casefold() in {"open", "closed", "won", "lost", "high", "medium", "low", "new business", "existing business"}:  # noqa: E501
                continue
            stages.append(name)
    stages = list(dict.fromkeys(stage.strip() for stage in stages if stage and stage.strip()))

    if not conditions and sort is None and limit is None:
        priority_match = re.search(
            r"\b(?:high|medium|low)\b.*\b(?:priority|priorit(?:y|ies))\b|\b(?:priority|priorit(?:y|ies))\b.*\b(?:high|medium|low)\b",  # noqa: E501
            message,
            re.IGNORECASE,
        )
        deal_type_match = re.search(
            r"\b(?:new|existing)\s*[- ]?business\b|\b(?:deal\s+type|type)\b.*?\b(?:new|existing)\s*[- ]?business\b",  # noqa: E501
            message,
            re.IGNORECASE,
        )
        pipeline_match = re.search(
            r"\b(?:in|for|at|on|with)\s+(?:the\s+)?(?P<name>[A-Za-z0-9][A-Za-z0-9&'./() -]{0,80}?)\s+pipeline\b",  # noqa: E501
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
                re.search(r"\b(?:unassigned|no owner|owner is blank|no one assigned)\b", message, re.IGNORECASE),  # noqa: E501
            )
        ):
            return None
    priority_match = re.search(
        r"\b(?:high|medium|low)\b.*\b(?:priority|priorit(?:y|ies))\b|\b(?:priority|priorit(?:y|ies))\b.*\b(?:high|medium|low)\b",  # noqa: E501
        message,
        re.IGNORECASE,
    )
    deal_type_match = re.search(
        r"\b(?:new|existing)\s*[- ]?business\b|\b(?:deal\s+type|type)\b.*?\b(?:new|existing)\s*[- ]?business\b",  # noqa: E501
        message,
        re.IGNORECASE,
    )
    pipeline_match = re.search(
        r"\b(?:in|for|at|on|with)\s+(?:the\s+)?(?P<name>[A-Za-z0-9][A-Za-z0-9&'./() -]{0,80}?)\s+pipeline\b",  # noqa: E501
        message,
        re.IGNORECASE,
    )
    pipeline_name = None
    if pipeline_match:
        pipeline_name = _strip_visualisation_noise((pipeline_match.group("name") or "").strip())
        pipeline_name = re.sub(r"\s+\b(?:sales\s+pipeline|pipeline)\b\s*$", "", pipeline_name, flags=re.IGNORECASE).strip()  # noqa: E501
    owner_match = re.search(
        r"\b(?:owner|assigned\s+to|owned\s+by)\b\s*(?:is|=|:)?\s*([A-Za-z][A-Za-z' .-]{1,80})",
        message,
        re.IGNORECASE,
    )
    unassigned = bool(re.search(r"\b(?:unassigned|no owner|owner is blank|no one assigned)\b", message, re.IGNORECASE))  # noqa: E501
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
        pipeline=(pipeline_name if pipeline_name else None),
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
