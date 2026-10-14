"""Read-only lead qualification: deterministic, explainable scoring and Slack formatting."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

from app.agent.schemas import LeadAssessment
from app.integrations.hubspot.models import HubSpotCompany, HubSpotContact

LeadPriority = Literal["High", "Medium", "Low"]

DEFAULT_LEAD_ANALYSIS_LIMIT = 50
DEFAULT_LEAD_AI_LIMIT = 5
HIGH_PRIORITY_MIN_SCORE = 60
MEDIUM_PRIORITY_MIN_SCORE = 35
LEAD_SLACK_MESSAGE_MAX_LENGTH = 39_000

_SENIOR_TITLE_TERMS = (
    "chief", "ceo", "cto", "cfo", "coo", "cmo", "cio", "ciso", "cro", "founder",
    "owner", "president", "vp", "vice president", "head of", "director", "partner",
)
_MANAGER_TITLE_TERMS = ("manager", "lead", "principal", "senior", "sr.")
_LIFECYCLE_POINTS = {
    "opportunity": 25,
    "salesqualifiedlead": 25,
    "marketingqualifiedlead": 20,
    "lead": 10,
    "subscriber": 3,
    "customer": 3,
    "evangelist": 3,
}
_LEAD_STATUS_POINTS = {
    "OPEN_DEAL": 15,
    "CONNECTED": 15,
    "IN_PROGRESS": 12,
    "OPEN": 8,
    "ATTEMPTED_TO_CONTACT": 8,
    "NEW": 5,
    "BAD_TIMING": 2,
    "UNQUALIFIED": 0,
}
_ACTIVITY_PROPERTIES = (
    "notes_last_contacted",
    "hs_last_sales_activity_timestamp",
    "notes_last_updated",
)
_DISQUALIFYING_LEAD_STATUSES = frozenset({"UNQUALIFIED"})


@dataclass(frozen=True)
class ScoredLead:
    contact: HubSpotContact
    company: HubSpotCompany | None
    score: int
    priority: LeadPriority
    reasons: list[str]
    missing_fields: list[str]
    last_activity: date | None
    facts: dict[str, object] = field(default_factory=dict)

    @property
    def name(self) -> str:
        props = self.contact.properties
        full = " ".join(p for p in (props.get("firstname"), props.get("lastname")) if p)
        return full or props.get("email") or f"Contact {self.contact.id}"


def parse_hubspot_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        if value.isdigit():
            return datetime.fromtimestamp(int(value) / 1000, UTC).date()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, OverflowError, OSError):
        return None


def classify_priority(score: int, lead_status: str | None) -> LeadPriority:
    if (lead_status or "").upper() in _DISQUALIFYING_LEAD_STATUSES:
        return "Low"
    if score >= HIGH_PRIORITY_MIN_SCORE:
        return "High"
    if score >= MEDIUM_PRIORITY_MIN_SCORE:
        return "Medium"
    return "Low"


def score_lead(
    contact: HubSpotContact,
    company: HubSpotCompany | None,
    *,
    today: date,
) -> ScoredLead:
    """Score a lead from recorded CRM data only. Missing data earns no points."""
    props = contact.properties
    company_props = company.properties if company else {}
    score = 0
    reasons: list[str] = []
    missing: list[str] = []

    title = (props.get("jobtitle") or "").strip()
    seniority = (props.get("hs_seniority") or "").strip()
    title_text = f"{title} {seniority}".casefold()
    if not title and not seniority:
        missing.append("job title")
    elif any(term in title_text for term in _SENIOR_TITLE_TERMS):
        score += 25
        reasons.append(f"Senior role recorded ({title or seniority})")
    elif any(term in title_text for term in _MANAGER_TITLE_TERMS):
        score += 15
        reasons.append(f"Management-level role recorded ({title or seniority})")
    else:
        score += 8
        reasons.append(f"Job title recorded ({title or seniority})")

    lifecycle = (props.get("lifecyclestage") or "").strip()
    if not lifecycle:
        missing.append("lifecycle stage")
    else:
        points = _LIFECYCLE_POINTS.get(lifecycle.casefold(), 0)
        score += points
        reasons.append(f"Lifecycle stage: {lifecycle}")

    lead_status = (props.get("hs_lead_status") or "").strip()
    if not lead_status:
        missing.append("lead status")
    else:
        score += _LEAD_STATUS_POINTS.get(lead_status.upper(), 0)
        reasons.append(f"Lead status: {lead_status}")

    last_activity = parse_hubspot_date(props.get("notes_last_contacted"))
    activity_source = "notes_last_contacted"
    if last_activity is None:
        last_activity = parse_hubspot_date(props.get("notes_last_updated"))
        activity_source = "notes_last_updated"
    days_since: int | None = None
    if last_activity is None:
        missing.append("last activity date")
    else:
        days_since = max((today - last_activity).days, 0)
        if days_since <= 30:
            score += 15
            reasons.append(f"Recent recorded activity ({days_since} days ago)")
        elif days_since <= 90:
            score += 8
            reasons.append(f"Recorded activity {days_since} days ago")
        else:
            score += 3
            reasons.append(f"Last recorded activity was {days_since} days ago")

    if company is None:
        missing.append("associated company")
    else:
        score += 5
        if not company_props.get("industry"):
            missing.append("company industry")
        else:
            score += 3
        employees = _to_int(company_props.get("numberofemployees"))
        if employees is None:
            missing.append("company size")
        else:
            score += 7 if employees >= 50 else 3
            reasons.append(f"Company size recorded: {employees} employees")

    if props.get("email"):
        score += 5
    else:
        missing.append("email")
    if not props.get("hubspot_owner_id"):
        missing.append("contact owner")

    score = min(score, 100)
    priority = classify_priority(score, lead_status)
    if lead_status.upper() in _DISQUALIFYING_LEAD_STATUSES:
        reasons.append("Lead status is Unqualified, so priority is capped at Low")

    facts: dict[str, object] = {
        "contact_name": " ".join(
            p for p in (props.get("firstname"), props.get("lastname")) if p
        )
        or None,
        "job_title": title or None,
        "seniority": seniority or None,
        "lifecycle_stage": lifecycle or None,
        "lead_status": lead_status or None,
        "owner_id": props.get("hubspot_owner_id"),
        "last_activity_date": last_activity.isoformat() if last_activity else None,
        "last_activity_source": activity_source if last_activity else None,
        "days_since_last_activity": days_since,
        "company_name": company_props.get("name"),
        "company_industry": company_props.get("industry"),
        "company_employees": company_props.get("numberofemployees"),
        "rules_based_score": score,
        "priority": priority,
        "scoring_reasons": reasons,
        "missing_fields": missing,
        "engagement_history": "Only the last activity date is available; no detailed history",
    }
    return ScoredLead(contact, company, score, priority, reasons, missing, last_activity, facts)


def rank_leads(leads: list[ScoredLead]) -> list[ScoredLead]:
    return sorted(leads, key=lambda lead: (-lead.score, lead.contact.id))


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _value(value: object, max_length: int) -> str:
    if value is None or value == "" or value == []:
        return "Not provided"
    return str(value).replace("\n", " ")[:max_length]


@dataclass(frozen=True)
class LeadReportEntry:
    lead: ScoredLead
    hubspot_url: str
    assessment: LeadAssessment | None


def format_lead_report(
    *,
    leads: list[ScoredLead],
    entries: list[LeadReportEntry],
    analysis_limit: int,
    has_more: bool,
) -> str:
    counts = {p: sum(1 for lead in leads if lead.priority == p) for p in ("High", "Medium", "Low")}
    lines = [
        "*AI LEAD QUALIFICATION & PRIORITIZATION*",
        "",
        "*Summary*",
        f"• Total leads analyzed: {len(leads)}",
        f"• High-priority leads: {counts['High']}",
        f"• Medium-priority leads: {counts['Medium']}",
        f"• Low-priority leads: {counts['Low']}",
        "• Priority is derived from recorded HubSpot data (role, lifecycle stage, lead "
        "status, last activity, company profile, data completeness); it is not a HubSpot score.",
    ]
    if has_more:
        lines.append(
            f"• Only the first {analysis_limit} contacts were analyzed; more exist in HubSpot."
        )
    if not leads:
        lines.extend(["", "No contacts were found to analyze."])

    sections: list[str] = []
    for index, entry in enumerate(entries, start=1):
        lead = entry.lead
        company_name = _value(lead.facts.get("company_name"), 160)
        parts = [
            f"*{index}. {_value(lead.name, 160)}* — {lead.priority} priority",
            f"• Company: {company_name}",
            f"• Qualification reasoning: {_value('; '.join(lead.reasons), 600)}",
            f"• Missing information: {_value(', '.join(lead.missing_fields), 300)}",
        ]
        assessment = entry.assessment
        if assessment is None:
            parts.append("• AI assessment could not be generated for this lead.")
        else:
            opportunities = _value("; ".join(assessment.opportunities), 500)
            parts.extend(
                [
                    f"• AI assessment: {_value(assessment.qualification_assessment, 500)}",
                    f"• Potential opportunities: {opportunities}",
                    f"• Additional gaps: {_value('; '.join(assessment.missing_information), 400)}",
                    f"• Recommended next action: {_value(assessment.recommended_next_action, 400)}",
                    f"• Suggested outreach approach: {_value(assessment.outreach_approach, 500)}",
                ]
            )
        parts.append(f"• <{_value(entry.hubspot_url, 500)}|Open contact in HubSpot>")
        sections.append("\n".join(parts))

    message = "\n".join(lines)
    omitted = 0
    for section in sections:
        if len(message) + 2 + len(section) + 100 > LEAD_SLACK_MESSAGE_MAX_LENGTH:
            omitted += 1
            continue
        message += "\n\n" + section
    if omitted:
        message += f"\n\n{omitted} lead detail section(s) were omitted to fit Slack's limit."
    return message
