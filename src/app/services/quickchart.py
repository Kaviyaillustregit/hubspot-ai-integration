import json
import re
import urllib.parse
from decimal import Decimal, InvalidOperation

from app.integrations.hubspot.models import HubSpotDeal

QUICKCHART_BASE_URL = "https://quickchart.io/chart"
PALETTE = [
    "#38BDF8",
    "#818CF8",
    "#34D399",
    "#F472B6",
    "#FBBF24",
    "#A78BFA",
    "#2DD4BF",
    "#FB923C",
]


def generate_chart_url(chart_config: dict, width: int = 800, height: int = 420) -> str:
    """Generate a QuickChart image URL without including any API credentials."""
    config_json = json.dumps(chart_config, separators=(",", ":"))
    encoded_config = urllib.parse.quote(config_json)
    return (
        f"{QUICKCHART_BASE_URL}?version=3&c={encoded_config}"
        f"&w={width}&h={height}"
        "&bkg=%230B0F19&devicePixelRatio=2.0&f=png"
    )


def _chart_legend_options(position: str = "right", *, display: bool = True) -> dict:
    legend = {"display": display, "labels": {"color": "#E2E8F0"}}
    if display:
        legend["position"] = position
    return legend


def _title_options(title: str) -> dict:
    return {
        "display": True,
        "text": title.upper(),
        "color": "#F8FAFC",
        "font": {"size": 16, "weight": "bold"},
        "padding": 12,
    }


def _currency_tick_callback() -> str:
    return "function(value) { return '$' + Number(value).toLocaleString('en-US'); }"


def _count_tick_callback() -> str:
    return "function(value) { return Number(value).toLocaleString('en-US', { maximumFractionDigits: 0 }); }"


def build_chart_config(
    chart_type: str,
    title: str,
    labels: list[str],
    values: list[float],
    *,
    value_label: str = "Deal value",
) -> dict:
    clean_type = (chart_type or "bar").lower()
    normalized_type = "funnel" if clean_type in {"funnel", "pipeline", "sales pipeline"} else clean_type
    if normalized_type == "doughnut":
        return {
            "type": "doughnut",
            "data": {
                "labels": labels,
                "datasets": [{
                    "label": value_label,
                    "data": values,
                    "backgroundColor": PALETTE[: len(labels)],
                    "borderColor": "#0B0F19",
                    "borderWidth": 2,
                    "hoverOffset": 4,
                }],
            },
            "options": {
                "responsive": True,
                "plugins": {
                    "title": _title_options(title),
                    "legend": _chart_legend_options(),
                },
            },
        }
    if normalized_type == "pie":
        return {
            "type": "pie",
            "data": {
                "labels": labels,
                "datasets": [{
                    "label": value_label,
                    "data": values,
                    "backgroundColor": PALETTE[: len(labels)],
                    "borderColor": "#0B0F19",
                    "borderWidth": 2,
                }],
            },
            "options": {
                "responsive": True,
                "plugins": {
                    "title": _title_options(title),
                    "legend": _chart_legend_options(),
                },
            },
        }
    if normalized_type == "line":
        return {
            "type": "line",
            "data": {
                "labels": labels,
                "datasets": [{
                    "label": value_label,
                    "data": values,
                    "borderColor": "#38BDF8",
                    "backgroundColor": "rgba(56, 189, 248, 0.18)",
                    "borderWidth": 3,
                    "pointBackgroundColor": "#38BDF8",
                    "pointRadius": 4,
                    "fill": True,
                    "lineTension": 0.35,
                }],
            },
            "options": {
                "responsive": True,
                "plugins": {
                    "title": _title_options(title),
                    "legend": {"display": False},
                },
            },
        }
    if normalized_type == "funnel":
        return {
            "type": "bar",
            "data": {
                "labels": labels,
                "datasets": [{
                    "label": value_label,
                    "data": values,
                    "backgroundColor": PALETTE[: len(labels)],
                    "borderColor": "#0B0F19",
                    "borderWidth": 1,
                    "borderRadius": 4,
                    "barThickness": 22,
                }],
            },
            "options": {
                "responsive": True,
                "indexAxis": "y",
                "plugins": {
                    "title": _title_options(title),
                    "legend": {"display": True, "position": "bottom", "labels": {"color": "#E2E8F0"}},
                    "tooltip": {
                        "callbacks": {
                            "label": "function(context) { return context.label + ': $' + context.parsed.x.toLocaleString(); }"
                        }
                    },
                },
                "scales": {
                    "x": {
                        "ticks": {"fontColor": "#94A3B8", "display": True},
                        "title": {"display": True, "text": value_label, "color": "#E2E8F0"},
                        "beginAtZero": True,
                        "suggestedMin": 0,
                    },
                    "y": {
                        "ticks": {"fontColor": "#94A3B8", "display": True},
                        "title": {"display": True, "text": "Pipeline stage", "color": "#E2E8F0"},
                    },
                },
            },
        }
    is_count_chart = value_label.lower() != "deal value"
    return {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": value_label,
                "data": values,
                "backgroundColor": PALETTE[: len(labels)],
                "borderColor": "#38BDF8",
                "borderWidth": 1,
                "borderRadius": 4,
                "barThickness": 52,
            }],
        },
        "options": {
            "responsive": True,
            "layout": {"padding": {"left": 16, "right": 16, "top": 8, "bottom": 12}},
            "plugins": {
                "title": _title_options(title),
                "legend": {"display": False},
            },
            "scales": {
                "x": {
                    "ticks": {
                        "fontColor": "#94A3B8",
                        "display": True,
                        "maxRotation": 45,
                        "minRotation": 30,
                        "autoSkip": False,
                    },
                    "grid": {"display": False},
                },
                "y": {
                    "beginAtZero": True,
                    "min": 0,
                    "suggestedMin": 0,
                    "ticks": {
                        "display": True,
                        "fontColor": "#94A3B8",
                        "precision": 0 if is_count_chart else None,
                        "callback": _currency_tick_callback() if value_label == "Deal value" else _count_tick_callback(),
                    },
                    "grid": {"color": "rgba(148, 163, 184, 0.18)"},
                },
            },
        },
    }


class QuickChartService:
    @staticmethod
    def detect_chart_type(message: str) -> str | None:
        match = re.search(
            r"\b(bar|doughnut|pie|line|funnel)\s+chart\b|"
            r"\b(?:as|in)\s+(?:a|an)\s+(bar|doughnut|pie|line|funnel)\s+chart\b|"
            r"\b(?:sales\s+pipeline|pipeline)\s+chart\b",
            message,
            re.IGNORECASE,
        )
        if not match:
            return None
        matched = match.group(1) or match.group(2) or "funnel"
        lowered = matched.lower()
        if lowered in {"pipeline", "sales pipeline"}:
            return "funnel"
        return lowered

    @staticmethod
    def stage_distribution_chart(
        deals: list[HubSpotDeal],
        *,
        title: str,
        chart_type: str | None = None,
        stage_map: dict[str, str] | None = None,
        stage_order: list[str] | None = None,
    ) -> str | None:
        grouped: dict[str, int] = {}
        ordered_names: list[str] = []
        for deal in deals:
            stage_id = str(deal.properties.get("dealstage") or "unknown")
            label = (stage_map or {}).get(stage_id) or stage_id
            if not label or label == "unknown":
                label = "Unknown stage"
            if label not in grouped:
                grouped[label] = 0
                ordered_names.append(label)
            grouped[label] += 1

        if stage_order:
            ordered = []
            seen = set()
            for stage_id in stage_order:
                label = (stage_map or {}).get(stage_id)
                if label is None:
                    continue
                if label in grouped and label not in seen:
                    ordered.append(label)
                    seen.add(label)
            for label in ordered_names:
                if label not in seen:
                    ordered.append(label)
                    seen.add(label)
            ordered_names = ordered

        labels = [label for label in ordered_names if label in grouped]
        values = [float(grouped[label]) for label in labels]
        if not labels or not values:
            return None

        clean_type = (chart_type or "doughnut").lower().replace("sales pipeline", "pipeline")
        if clean_type not in {"bar", "line", "pie", "doughnut"}:
            clean_type = "doughnut"
        config = build_chart_config(clean_type, title, labels, values, value_label="Deal count")
        return generate_chart_url(config)

    @staticmethod
    def stage_distribution_summary(
        deals: list[HubSpotDeal],
        *,
        chart_type: str | None = None,
        stage_map: dict[str, str] | None = None,
        stage_order: list[str] | None = None,
    ) -> str:
        if not deals:
            return "No deals available for stage distribution."

        grouped: dict[str, int] = {}
        ordered_names: list[str] = []
        for deal in deals:
            stage_id = str(deal.properties.get("dealstage") or "unknown")
            label = (stage_map or {}).get(stage_id) or stage_id
            if not label or label == "unknown":
                label = "Unknown stage"
            if label not in grouped:
                grouped[label] = 0
                ordered_names.append(label)
            grouped[label] += 1

        if stage_order:
            ordered = []
            seen = set()
            for stage_id in stage_order:
                label = (stage_map or {}).get(stage_id)
                if label is None:
                    continue
                if label in grouped and label not in seen:
                    ordered.append(label)
                    seen.add(label)
            for label in ordered_names:
                if label not in seen:
                    ordered.append(label)
                    seen.add(label)
            ordered_names = ordered

        labels = [label for label in ordered_names if label in grouped]
        values = [grouped[label] for label in labels]
        if not labels:
            return f"{len(deals)} deals are in scope, but no stage data was available."

        top_label, top_value = max(zip(labels, values), key=lambda item: item[1])
        stage_summary = ", ".join(f"{label}: {value}" for label, value in zip(labels, values))
        return (
            f"{len(deals)} deals are distributed across {len(labels)} stage{'s' if len(labels) != 1 else ''}. "
            f"Largest stage: {top_label} ({top_value}). "
            f"Breakdown: {stage_summary}."
        )

    @staticmethod
    def chart_for_deals(
        deals: list[HubSpotDeal],
        *,
        title: str,
        chart_type: str | None = None,
        limit: int = 5,
        pipeline_stage_map: dict[str, str] | None = None,
        pipeline_stage_order: list[str] | None = None,
    ) -> str | None:
        resolved_type = (chart_type or "bar").lower().replace("sales pipeline", "pipeline")
        if resolved_type in {"pipeline", "funnel"}:
            return _chart_for_pipeline(deals, title=title, limit=limit, pipeline_stage_map=pipeline_stage_map, pipeline_stage_order=pipeline_stage_order)
        selected = sorted(
            deals,
            key=lambda deal: _deal_amount_value(deal) or Decimal("0"),
            reverse=True,
        )[:limit]
        if not selected:
            return None

        labels = []
        values = []
        for deal in selected:
            amount = _deal_amount_value(deal)
            if amount is None:
                continue
            name = deal.properties.get("dealname") or deal.id
            labels.append(str(name).strip() or deal.id)
            values.append(float(amount))

        if not labels or not values:
            return None

        if resolved_type not in {"bar", "doughnut", "pie", "line"}:
            resolved_type = "bar"
        config = build_chart_config(resolved_type, title, labels, values)
        return generate_chart_url(config)

    @staticmethod
    def chart_summary_for_deals(
        deals: list[HubSpotDeal],
        *,
        chart_type: str | None = None,
        pipeline_stage_map: dict[str, str] | None = None,
        pipeline_stage_order: list[str] | None = None,
    ) -> str:
        if not deals:
            return "No open deals available for charting."

        safe_deals = [deal for deal in deals if _deal_amount_value(deal) is not None]
        total_value = sum((_deal_amount_value(deal) or Decimal("0")) for deal in safe_deals)
        chart_kind = (chart_type or "bar").lower().replace("sales pipeline", "pipeline")
        if chart_kind in {"pipeline", "funnel"}:
            labels, values = _pipeline_stage_values(
                deals,
                pipeline_stage_map=pipeline_stage_map,
                pipeline_stage_order=pipeline_stage_order,
            )
            if labels:
                top_stage = max(zip(labels, values), key=lambda item: item[1])
                return (
                    f"{len(deals)} deals are in the pipeline with a total value of {format_currency(total_value)}. "
                    f"The largest stage is {top_stage[0]} at {format_currency(Decimal(str(top_stage[1])))}."
                )
        if safe_deals:
            top_deal = max(safe_deals, key=lambda deal: _deal_amount_value(deal) or Decimal("0"))
            top_name = top_deal.properties.get("dealname") or top_deal.id
            return (
                f"{len(safe_deals)} deals total {format_currency(total_value)} in value. "
                f"Top deal: {top_name} ({format_currency(_deal_amount_value(top_deal) or Decimal('0'))})."
            )
        return f"{len(deals)} deals are in scope; no valid monetary totals were available."


def _pipeline_stage_values(
    deals: list[HubSpotDeal],
    *,
    pipeline_stage_map: dict[str, str] | None = None,
    pipeline_stage_order: list[str] | None = None,
) -> tuple[list[str], list[float]]:
    grouped: dict[str, Decimal] = {}
    order: list[str] = []
    for deal in deals:
        stage_id = deal.properties.get("dealstage") or "unknown"
        stage_name = pipeline_stage_map.get(stage_id) if pipeline_stage_map else None
        if stage_name is None:
            stage_name = (stage_id or "Unknown stage").replace("-", " ").title()
        if stage_name not in grouped:
            grouped[stage_name] = Decimal("0")
            order.append(stage_name)
        amount = _deal_amount_value(deal)
        if amount is not None:
            grouped[stage_name] += amount

    if pipeline_stage_order:
        ordered_names = [pipeline_stage_map.get(stage_id, stage_id) for stage_id in pipeline_stage_order if pipeline_stage_map and stage_id in pipeline_stage_map]
        for name in ordered_names:
            if name not in grouped and name:
                grouped[name] = Decimal("0")
        ordered = [name for name in ordered_names if name in grouped]
        remaining = [name for name in order if name not in ordered]
        ordered.extend(remaining)
        order = ordered

    labels = [name for name in order if name in grouped]
    values = [float(grouped[name]) for name in labels]
    return labels, values


def _chart_for_pipeline(
    deals: list[HubSpotDeal],
    *,
    title: str,
    limit: int = 5,
    pipeline_stage_map: dict[str, str] | None = None,
    pipeline_stage_order: list[str] | None = None,
) -> str | None:
    labels, values = _pipeline_stage_values(
        deals,
        pipeline_stage_map=pipeline_stage_map,
        pipeline_stage_order=pipeline_stage_order,
    )
    if not labels or not values:
        return None
    if len(labels) > limit:
        labels = labels[:limit]
        values = values[:limit]
    config = build_chart_config("funnel", title, labels, values)
    return generate_chart_url(config)


def format_currency(value: Decimal) -> str:
    return f"${value:,.2f}"


def _deal_amount_value(deal: HubSpotDeal) -> Decimal | None:
    raw = deal.properties.get("amount")
    if raw is None or not str(raw).strip():
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None
