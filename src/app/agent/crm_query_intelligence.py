"""Validated, read-only query helpers shared by CRM object listings."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import get_close_matches
from typing import Any
from zoneinfo import ZoneInfo

from app.agent.crm_intelligence import (
    CRMIntelligenceDateRange,
    CRMQueryPlan,
    _relative_date_range,
)
from app.integrations.hubspot.properties import HubSpotPropertyDefinition


class UnsupportedCRMField(ValueError):
    """Raised when a requested field is not present in HubSpot's property catalog."""


def resolve_date_bounds(
    date_range: CRMIntelligenceDateRange, *, today: date | None = None
) -> tuple[date | None, date | None]:
    """Resolve explicit or relative bounds as a half-open date range."""
    if date_range.relative and not date_range.start and not date_range.end:
        start, end = _relative_date_range(
            date_range.relative, today=today, timezone=date_range.timezone
        )
    else:
        start, end = date_range.start, date_range.end
    return (
        date.fromisoformat(start) if start else None,
        date.fromisoformat(end) if end else None,
    )


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def resolve_catalog_fields(
    plan: CRMQueryPlan, catalog: list[HubSpotPropertyDefinition]
) -> CRMQueryPlan:
    """Resolve internal names, labels, and common aliases against the live catalog."""
    names = {item.name: item for item in catalog}
    aliases = {_key(item.name): item.name for item in catalog}
    aliases.update({_key(item.label): item.name for item in catalog if item.label})
    common = {
        "name": {"deal": "dealname", "company": "name", "contact": "firstname"}.get(plan.object),
        "owner": "hubspot_owner_id",
        "ownerid": "hubspot_owner_id",
        "stage": "dealstage",
        "dealstage": "dealstage",
        "close date": "closedate",
        "closing date": "closedate",
        "creation date": "createdate",
        "created date": "createdate",
        "company name": "name" if plan.object == "company" else "associatedcompanyid",
    }
    aliases.update({_key(alias): target for alias, target in common.items() if target in names})

    def resolve(field: str) -> str:
        if _key(field) in {"id", "recordid", "hubspotrecordid"}:
            return "id"
        canonical = aliases.get(_key(field))
        if canonical:
            return canonical
        labels = [item.label for item in catalog]
        nearby = get_close_matches(field, [*names, *labels], n=1, cutoff=0.58)
        suggestion = f" Did you mean '{nearby[0]}'?" if nearby else ""
        raise UnsupportedCRMField(f"I couldn't find the '{field}' field in HubSpot.{suggestion}")

    update: dict[str, Any] = {
        "properties": list(dict.fromkeys(resolve(item) for item in plan.properties)),
        "filters": [
            item.model_copy(update={"field": resolve(item.field)}) for item in plan.filters
        ],
        "sort": [item.model_copy(update={"field": resolve(item.field)}) for item in plan.sort],
        "aggregations": [
            item.model_copy(update={"field": resolve(item.field)}) for item in plan.aggregations
        ],
    }
    if plan.group_by and plan.group_by not in {"month", "quarter", "year"}:
        update["group_by"] = resolve(plan.group_by)
    if plan.date_range:
        update["date_range"] = plan.date_range.model_copy(
            update={"field": resolve(plan.date_range.field)}
        )
    if plan.group_by in {"month", "quarter", "year"}:
        update["group_date_field"] = resolve(plan.group_date_field)
    return plan.model_copy(update=update)


def execute_record_query(
    plan: CRMQueryPlan,
    records: list[Any],
    catalog: list[HubSpotPropertyDefinition],
) -> dict[str, Any]:
    """Apply validated conditions, sorting, grouping/aggregations and bounded paging."""
    plan = resolve_catalog_fields(plan, catalog)
    definitions = {item.name: item for item in catalog}

    def value(record: Any, field: str) -> Any:
        if field == "id":
            return getattr(record, "id", None)
        return (getattr(record, "properties", None) or {}).get(field)

    def typed(raw: Any, field: str, *, timezone_name: str | None = None) -> Any:
        if raw is None:
            return None
        kind = definitions.get(field).type if field in definitions else "string"
        text = str(raw)
        if kind in {"number", "enumeration"} and kind == "number":
            try:
                return Decimal(text)
            except InvalidOperation:
                return None
        if kind == "date" or "date" in kind or field.endswith("date"):
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except (TypeError, ValueError, OverflowError):
                try:
                    return date.fromisoformat(text[:10])
                except (TypeError, ValueError):
                    return None
            if timezone_name:
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(ZoneInfo(timezone_name))
            return parsed.date()
        return text.casefold()

    date_bounds = resolve_date_bounds(plan.date_range) if plan.date_range else (None, None)
    matched = []
    for record in records:
        keep = True
        for condition in plan.filters:
            raw, expected = value(record, condition.field), condition.value
            left, right = typed(raw, condition.field), typed(expected, condition.field)
            op = condition.operator
            if op == "contains":
                valid = raw is not None and expected.casefold() in str(raw).casefold()
            elif op == "in":
                valid = left is not None and left in {
                    typed(part.strip(), condition.field) for part in expected.split(",")
                }
            elif op in {"gt", "gte", "lt", "lte"}:
                valid = False
                if left is not None and right is not None:
                    valid = {
                        "gt": left > right,
                        "gte": left >= right,
                        "lt": left < right,
                        "lte": left <= right,
                    }[op]
            else:
                equal = left is not None and left == right
                valid = not equal if op == "neq" and left is not None else equal
            keep &= valid
        if keep and plan.date_range:
            date_value = typed(
                value(record, plan.date_range.field),
                plan.date_range.field,
                timezone_name=plan.date_range.timezone,
            )
            start, end = date_bounds
            keep = date_value is not None and (start is None or date_value >= start) and (
                end is None or date_value < end
            )
        if keep:
            matched.append(record)

    for sort in reversed(plan.sort):
        present = [
            record
            for record in matched
            if typed(value(record, sort.field), sort.field) is not None
        ]
        missing = [
            record
            for record in matched
            if typed(value(record, sort.field), sort.field) is None
        ]
        present.sort(
            key=lambda record: typed(value(record, sort.field), sort.field),
            reverse=sort.direction == "desc",
        )
        matched = present + missing

    if plan.group_by:
        buckets: dict[str, list[Any]] = defaultdict(list)
        for record in matched:
            raw = value(record, plan.group_by)
            if plan.group_by in {"month", "quarter", "year"}:
                field_date = typed(value(record, plan.group_date_field), plan.group_date_field)
                if field_date is None:
                    key = "Unknown"
                elif plan.group_by == "month":
                    key = field_date.strftime("%Y-%m")
                elif plan.group_by == "quarter":
                    key = f"Q{(field_date.month - 1) // 3 + 1} {field_date.year}"
                else:
                    key = str(field_date.year)
            else:
                key = str(raw) if raw not in {None, ""} else "Unknown"
            buckets[key].append(record)
        rows = [{"group": key, "count": len(items)} for key, items in sorted(buckets.items())]
        for aggregation in plan.aggregations:
            for row in rows:
                vals = [
                    typed(value(record, aggregation.field), aggregation.field)
                    for record in buckets[row["group"]]
                ]
                vals = [item for item in vals if item is not None]
                if aggregation.operation == "count":
                    result = len(buckets[row["group"]])
                elif aggregation.operation in {"sum", "avg"} and not all(
                    isinstance(item, Decimal) for item in vals
                ):
                    result = None
                elif not vals:
                    result = None
                elif aggregation.operation == "sum":
                    result = str(sum(vals, Decimal(0)))
                elif aggregation.operation == "avg":
                    result = str(sum(vals, Decimal(0)) / len(vals))
                elif aggregation.operation == "min":
                    result = str(min(vals))
                else:
                    result = str(max(vals))
                row[f"{aggregation.operation}_{aggregation.field}"] = result
        return {
            "matched_records": len(matched),
            "rows": rows,
            "total": len(rows),
            "offset": 0,
            "has_more": False,
            "columns": [
                {"key": key, "label": key.replace("_", " ").title()}
                for key in (rows[0].keys() if rows else ("group", "count"))
            ],
        }

    if plan.aggregations:
        row: dict[str, Any] = {"count": len(matched)}
        for aggregation in plan.aggregations:
            vals = [
                typed(value(record, aggregation.field), aggregation.field)
                for record in matched
            ]
            vals = [item for item in vals if item is not None]
            if aggregation.operation == "count":
                result_value: Any = len(matched)
            elif aggregation.operation in {"sum", "avg"} and not all(
                isinstance(item, Decimal) for item in vals
            ):
                result_value = None
            elif not vals:
                result_value = None
            elif aggregation.operation == "sum":
                result_value = str(sum(vals, Decimal(0)))
            elif aggregation.operation == "avg":
                result_value = str(sum(vals, Decimal(0)) / len(vals))
            elif aggregation.operation == "min":
                result_value = str(min(vals))
            else:
                result_value = str(max(vals))
            row[f"{aggregation.operation}_{aggregation.field}"] = result_value
        return {
            "matched_records": len(matched),
            "rows": [row],
            "total": 1,
            "offset": 0,
            "has_more": False,
            "columns": [
                {"key": key, "label": key.replace("_", " ").title()} for key in row
            ],
        }

    offset = plan.offset
    limit = plan.limit or 20
    page = matched[offset : offset + limit]
    fields = list(dict.fromkeys([*plan.properties]))
    if not fields:
        default_fields = {
            "name",
            "dealname",
            "firstname",
            "lastname",
            "email",
            "amount",
            "dealstage",
            "closedate",
            "createdate",
        }
        fields = [item.name for item in catalog if item.name in default_fields][:6]
    rows = [
        {
            "id": str(getattr(record, "id", "")),
            **{field: value(record, field) for field in fields},
        }
        for record in page
    ]
    return {
        "matched_records": len(matched),
        "rows": rows,
        "total": len(rows),
        "offset": offset,
        "has_more": offset + len(page) < len(matched),
        "columns": [
            {
                "key": field,
                "label": definitions[field].label if field in definitions else "Record ID",
            }
            for field in fields
        ],
    }
