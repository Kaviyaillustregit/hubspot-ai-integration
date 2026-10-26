import contextvars
import hashlib
import logging
import re
from asyncio import Semaphore, gather
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from app.agent.crm_intelligence import (
    CRMIntelligencePlanner,
    CRMQueryPlan,
    _parse_decimal,
    _parse_hubspot_date,
    analyze_deals,
    build_chart_payload,
    plan_crm_query,
    unsupported_intelligence_message,
)
from app.agent.crm_query import DealFilterSpec, deal_status_query, detect_deal_filter
from app.agent.crm_query_intelligence import (
    UnsupportedCRMField,
    execute_record_query,
    resolve_catalog_fields,
)
from app.agent.extraction import (
    ExtractionValidationError,
    ValidatedExtraction,
    validate_extraction,
)
from app.agent.leads import (
    LeadReportEntry,
    ScoredLead,
    format_lead_report,
    rank_leads,
    score_lead,
)
from app.agent.operations import CONFIRMED_UPDATE_ACTIONS, OPERATION_INTENTS, CRMOperations
from app.agent.schemas import (
    AgentRequest,
    AgentResponse,
    ContactCreateIntent,
    ContactDeleteIntent,
    ContactUpdateIntent,
    CRMIntentExtraction,
    CRMQueryName,
    DealAnalysis,
    DealFollowUpAssessment,
    GroundedSummary,
    LeadAssessment,
)
from app.agent.tools import (
    DEAL_ANALYSIS_PROPERTIES,
    DEAL_FOLLOW_UP_PROPERTIES,
    DEAL_LIST_PROPERTIES,
    HubSpotToolRegistry,
)
from app.ai.service import AIService
from app.integrations.errors import (
    IntegrationError,
    IntegrationPermissionError,
    IntegrationRateLimitError,
    IntegrationTimeoutError,
)
from app.integrations.hubspot.models import HubSpotCompany, HubSpotContact, HubSpotDeal
from app.integrations.hubspot.properties import HubSpotPropertyDefinition
from app.services.action_safety import ActionSafetyService
from app.services.hubspot_contacts import HubSpotDuplicateContactError
from app.services.quickchart import build_chart_config, generate_chart_url

logger = logging.getLogger(__name__)

_current_progress: contextvars.ContextVar[Callable[[str], Awaitable[None]] | None] = (
    contextvars.ContextVar("current_progress", default=None)
)


async def _report_progress(status: str) -> None:
    callback = _current_progress.get()
    if callback is not None:
        try:
            await callback(status)
        except Exception:
            logger.warning("Progress callback failed", exc_info=True)

_STAGE_ANALYTICS_QUERIES = frozenset(
    {"all_deals", "open_deals", "closed_deals", "closed_won_deals", "closed_lost_deals"}
)
_STAGE_ANALYTICS_PATTERN = re.compile(
    r"\b(?:(?:each|every|per|by)\s+(?:deal\s+)?stages?|group(?:ed)?\s*by"
    r"|pipeline\s+(?:summary|breakdown)|stage\s+(?:breakdown|comparison|distribution))\b",
    re.IGNORECASE,
)
_DEAL_INTELLIGENCE_PATTERN = re.compile(
    r"\b(?:deal|deals|opportunit\w*|closing|close|closed|date|dated|quarter|q[1-4]|month|year|"
    r"latest|newest|oldest|earliest|recent|top|bottom|highest|lowest|group|breakdown|"
    r"pipeline|stage|owner|amount|value|created|creation|from|between|before|after)\b|"
    r"\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)

_WRITE_INTENTS = frozenset({"create_contact", "update_contact", "delete_contact"}) | (
    OPERATION_INTENTS
)
_MIN_WRITE_CONFIDENCE = 0.6
_CONTACT_IDENTITY_PROPERTIES = frozenset({"firstname", "lastname", "email"})
_STANDARD_QUERY_PROPERTIES = {
    "contact": (
        ("firstname", "First name", "string"),
        ("lastname", "Last name", "string"),
        ("email", "Email", "string"),
        ("phone", "Phone", "string"),
        ("company", "Company", "string"),
        ("jobtitle", "Job title", "string"),
        ("city", "City", "string"),
        ("state", "State/Region", "string"),
        ("country", "Country/Region", "string"),
        ("industry", "Industry", "string"),
        ("lifecyclestage", "Lifecycle stage", "enumeration"),
        ("hs_lead_status", "Lead status", "enumeration"),
        ("createdate", "Create date", "date"),
        ("lastmodifieddate", "Last modified date", "date"),
        ("hubspot_owner_id", "HubSpot owner", "number"),
    ),
    "company": (
        ("name", "Company name", "string"),
        ("domain", "Company domain", "string"),
        ("industry", "Industry", "string"),
        ("city", "City", "string"),
        ("state", "State/Region", "string"),
        ("country", "Country/Region", "string"),
        ("phone", "Phone", "string"),
        ("numberofemployees", "Number of employees", "number"),
        ("annualrevenue", "Annual revenue", "number"),
        ("lifecyclestage", "Lifecycle stage", "enumeration"),
        ("hs_lead_status", "Lead status", "enumeration"),
        ("createdate", "Create date", "date"),
        ("lastmodifieddate", "Last modified date", "date"),
        ("hubspot_owner_id", "HubSpot owner", "number"),
    ),
}
_CAPABILITIES_TEXT = (
    "I can create, update, or delete HubSpot contacts and answer questions about "
    "companies and their contacts."
)


def _requested_association(message: str, object_type: str) -> str | None:
    text = message.casefold()
    if object_type == "contact" and re.search(
        r"\b(?:associated\s+compan(?:y|ies)|company\s+association|contacts?\s+at\s+)",
        text,
    ):
        return "associated_company"
    if object_type == "company":
        if re.search(r"\bassociated\s+deals?\b", text):
            return "associated_deals"
        if re.search(r"\bassociated\s+contacts?\b", text):
            return "associated_contacts"
    return None


def _normalise_chart_type(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace("-", " ").replace("_", " ")
    if not text:
        return None
    if "pipeline" in text or "funnel" in text:
        return "funnel"
    if "doughnut" in text or "donut" in text:
        return "doughnut"
    if "pie" in text:
        return "pie"
    if "line" in text:
        return "line"
    if "bar" in text:
        return "bar"
    return None


def _infer_chart_type(message: str, *, query: str | None = None) -> str | None:
    text = message.casefold()
    if re.search(r"\b(?:sales\s+pipeline|pipeline)\b", text):
        return "funnel"
    if re.search(r"\b(?:distribution|breakdown|by\s+stage|stage\s+breakdown|deal\s+mix)\b", text):
        return "doughnut"
    if re.search(
        r"\b(?:trend|over\s+time|by\s+month|month\s+to\s+month|quarter|week)\b",
        text,
    ):
        return "line"
    if query in {
        "open_deals",
        "closed_deals",
        "closed_won_deals",
        "closed_lost_deals",
        "all_deals",
    }:
        return "bar"
    return None


@dataclass(frozen=True)
class _ContactDraft:
    properties: dict[str, str]
    company_name: str | None


@dataclass(frozen=True)
class _DealFollowUpCandidate:
    deal: HubSpotDeal
    pipeline: str
    stage: str
    activity_date: date
    inactive_days: int
    priority: str


@dataclass(frozen=True)
class _DealFollowUpResult:
    candidate: _DealFollowUpCandidate
    facts: dict[str, object]
    hubspot_url: str
    assessment: DealFollowUpAssessment | None


_ACCOUNT_PATTERN = re.compile(
    r"(?:about|for)\s+(.+?)[?.!]*$",
    re.IGNORECASE,
)

_CONTACT_CREATE_PATTERN = re.compile(
    r"\b(?:create|add)\s+(?:a\s+)?contact\b",
    re.IGNORECASE,
)
_AMBIGUOUS_DEAL_TYPE_CREATE_PATTERN = re.compile(
    r"\b(?:create|add|make)\s+(?:(?:a|an|new)\s+)*deal\s+type\b",
    re.IGNORECASE,
)
_EXPLICIT_CREATE_PATTERN = re.compile(
    r"^\s*(?:(?:please\s+)?(?:(?:can|could|would) you\s+(?:please\s+)?)"
    r"|(?:i want to|i need to|i(?:'d| would) like to)\s+)?"
    r"(?:create|add|make)\s+(?:(?:a|an)\s+)?(?:new\s+)?"
    r"(?P<entity>company|contact)\b"
    r"(?P<details>.*)",
    re.IGNORECASE,
)
_DEAL_CREATE_REQUEST_PATTERN = re.compile(
    r"^\s*(?:(?:please\s+)?(?:(?:can|could|would) you\s+)?"
    r"(?:i want to|i need to|i(?:'d| would) like to)\s+)?"
    r"(?:create|add|make)\s+(?:(?:a|an|new)\s+)?deal\b",
    re.IGNORECASE,
)
_CREATE_NAME_PATTERN = re.compile(
    r"\b(?:named|called|name)\s*[:=]?\s*(?P<name>.+?)"
    r"(?=\s*(?:[.,;!?]|$|\b(?:with|email|phone|website|domain|city|"
    r"under|associated\s+with)\b))",
    re.IGNORECASE,
)
_CONTACT_NAME_WITH_LAST_PATTERN = re.compile(
    r"\b(?:(?:named|called|name|first\s+name)\s+)?(?P<first>[\w'-]+)\s+and\s+"
    r"(?:the\s+)?last\s+name\s+(?P<last>[\w'-]+)\b",
    re.IGNORECASE,
)

_CONTACT_UPDATE_PATTERN = re.compile(
    r"\bupdate\s+(?:a\s+)?contact\b",
    re.IGNORECASE,
)

_CONTACT_DELETE_PATTERN = re.compile(
    r"\bdelete\s+(?:a\s+)?contact\b",
    re.IGNORECASE,
)

_CONFIRM_ACTION_PATTERN = re.compile(
    r"^\s*(?:<@[^>]+>\s*)?confirm\s+`?([a-f0-9]{32})`?\s*$",
    re.IGNORECASE,
)

_EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)

_CONTACT_ID_PATTERN = re.compile(
    r"\b(?:contact[_\s-]?id|id)\s*[:=]?\s*([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

_FIRSTNAME_PATTERN = re.compile(
    r"\bfirstname\s*[:=]?\s*([A-Za-z][A-Za-z'-]*)",
    re.IGNORECASE,
)

_LASTNAME_PATTERN = re.compile(
    r"\blastname\s*[:=]?\s*([A-Za-z][A-Za-z'-]*)",
    re.IGNORECASE,
)
_JOBTITLE_PATTERN = re.compile(
    r"\bjobtitle\s*[:=]?\s*(.+?)(?=\s+(?:email|firstname|lastname|jobtitle)\b|$)",
    re.IGNORECASE,
)
_ALL_CONTACTS_LIST_PATTERN = re.compile(
    r"\b(?:show|list|display|get|view|find)\b.*\b(?:all|every)\s+contacts\b",
    re.IGNORECASE,
)
_ALL_COMPANIES_LIST_PATTERN = re.compile(
    r"\b(?:show|list|display|get|view|find)\b.*\b(?:all|every)\s+compan(?:y|ies)\b",
    re.IGNORECASE,
)
_ALL_OPEN_DEALS_LIST_PATTERN = re.compile(
     r"\b(?:show|list|display|get|view|find)\b.*\b(?:all|every)\s+open\s+(?:deals?|(?:opportunit|oppurtunit)(?:y|ies))\b",  # noqa: E501
    re.IGNORECASE,
)
_ALL_DEALS_LIST_PATTERN = re.compile(
    r"\b(?:show|list|display|get|view|find)\b.*\b(?:all|every)\s+deals?\b",
    re.IGNORECASE,
)
_REVENUE_QUESTION_PATTERN = re.compile(
    r"\brevenue\b"
    r"|\bhow much\b.{0,100}\b(?:won|closed)\b"
    r"|\b(?:what|which)\b.{0,100}\b(?:value|amount)\b.{0,100}\b(?:won|closed)\b",
    re.IGNORECASE,
)
_EMAIL_DRAFT_REQUEST_PATTERN = re.compile(
    r"\b(?:draft|write|compose|prepare|generate|include|create)\b"
    r".{0,60}\b(?:follow[- ]?up\s+)?email\b|\bfollow[- ]?up\s+email\s+draft\b",
    re.IGNORECASE,
)
_DEAL_FOLLOW_UP_REQUEST_PATTERN = re.compile(
    r"\b(?:find|show|list|identify|review|analy[sz]e)\b"
    r".{0,100}\bdeals\b.{0,120}\b"
    r"(?:follow[- ]?up|next[- ]best\s+actions?|need(?:ing)?\s+follow[- ]?up)\b",
    re.IGNORECASE,
)
_LEAD_QUALIFICATION_REQUEST_PATTERN = re.compile(
    r"\b(?:leads?|prospects?)\b.{0,100}\b(?:qualif\w*|prioriti[sz]\w*|scor\w*|high[- ]potential)\b"
    r"|\b(?:qualif\w*|prioriti[sz]\w*|scor\w*|high[- ]potential)\b.{0,60}\b(?:leads?|prospects?)\b"
    r"|\bwhich\s+(?:leads?|prospects?)\b.{0,60}\b(?:contact|call|reach|prioriti[sz]e|focus)\b",
    re.IGNORECASE,
)
_LEAD_RETRIEVAL_REQUEST_PATTERN = re.compile(
    r"\b(?:show|list|display|get|find|who\s+are)\b.{0,80}"
    r"\b(?:our\s+)?(?:leads?|potential\s+customers?|prospects?)\b",
    re.IGNORECASE,
)
_LEAD_CONCURRENCY = 5
_SLACK_MESSAGE_MAX_LENGTH = 39_000


class AccountIntelligenceAgent:
    def __init__(
        self,
        tools: HubSpotToolRegistry,
        ai_service: AIService,
        action_safety: ActionSafetyService,
        *,
        deal_follow_up_batch_size: int = 10,
        lead_analysis_limit: int = 50,
        lead_ai_batch_size: int = 5,
    ) -> None:
        self._tools = tools
        self._ai_service = ai_service
        self._action_safety = action_safety
        self._operations = CRMOperations(tools, action_safety)
        self._crm_planner = CRMIntelligencePlanner(ai_service=ai_service)
        self._deal_follow_up_batch_size = deal_follow_up_batch_size
        self._lead_analysis_limit = lead_analysis_limit
        self._lead_ai_batch_size = lead_ai_batch_size

    @staticmethod
    def _supports_read_only_crm_intelligence(plan: CRMQueryPlan, message: str) -> bool:
        if plan.object != "deal" or unsupported_intelligence_message(plan) is not None:
            return False
        text = message.casefold()
        return bool(
            plan.group_by is not None
            or plan.aggregations
            or plan.filters
            or plan.sort
            or plan.date_range is not None
            or "pipeline" in text
            or "stage" in text
            or "summary" in text
            or "group" in text
            or "quarter" in text
            or "latest" in text
            or re.search(
                r"\b(?:show|list|display|find|get|retrieve)\b.*\b(?:deals?|opportunities)\b"
                r"|\b(?:all|latest|newest|oldest|earliest|top|bottom)\b.*"
                r"\b(?:deals?|opportunities)\b"
                r"|\bwhat\s+(?:are\s+)?(?:all\s+)?(?:the\s+)?"
                r"(?:deals?|opportunities)\b",
                text,
            )
        )

    @staticmethod
    def _recover_missing_date_range(plan: CRMQueryPlan, message: str) -> CRMQueryPlan:
        parsed_date_range = plan_crm_query(message).date_range
        if parsed_date_range is None:
            return plan
        if plan.date_range is not None:
            # Quarter names denote complete calendar quarters. Some planner responses
            # encode the final day as an exclusive end, dropping that day at analysis.
            # Canonicalize only an explicit quarter request; preserve other valid ranges.
            if re.search(r"\bq[1-4](?:\s+20\d{2})?\b", message, re.IGNORECASE):
                return plan.model_copy(update={"date_range": parsed_date_range})
            return plan
        return plan.model_copy(update={"date_range": parsed_date_range})

    @staticmethod
    def _is_unsupported_intelligence_request(plan: CRMQueryPlan) -> bool:
        if plan.object == "deal":
            return plan.group_by is not None and unsupported_intelligence_message(plan) is not None
        return plan.chart_requested

    async def _route_crm_intelligence(
        self,
        request: AgentRequest,
        extraction: CRMIntentExtraction,
    ) -> AgentResponse | None:
        if extraction.intent != "crm_question":
            return None
        non_deal_query = bool(
            re.search(
                r"\b(?:contact|contacts|company|companies|leads?|prospects?|potential\s+customers?)\b",
                request.message,
                re.I,
            )
            and re.search(
                r"\b(?:show|list|find|get|display|count|how many|who\s+are|group|"
                r"breakdown|sort|filter)\b",
                request.message,
                re.I,
            )
        )
        if extraction.query is not None and not (
            extraction.query in _STAGE_ANALYTICS_QUERIES
            and (
                _STAGE_ANALYTICS_PATTERN.search(request.message)
                or _DEAL_INTELLIGENCE_PATTERN.search(request.message)
            )
        ) and not (
            non_deal_query
            and extraction.query
            in {"company_list", "company_search", "contact_list", "contact_search"}
        ):
            return None
        if (
            extraction.query is None
            and not _DEAL_INTELLIGENCE_PATTERN.search(request.message)
            and not non_deal_query
        ):
            return None
        try:
            plan = await self._crm_planner.plan(
                request.message, conversation_context=request.conversation_context
            )
        except ValueError:
            return None
        plan = self._recover_missing_date_range(plan, request.message)
        deterministic_plan = plan_crm_query(request.message)
        if (
            _LEAD_RETRIEVAL_REQUEST_PATTERN.search(request.message)
            or re.search(r"\boverdue\b|\bactive\s+pipeline\b", request.message, re.I)
            or (
                deterministic_plan.object == "deal"
                and deterministic_plan.group_by is not None
                and re.search(
                    r"\b(?:group|summary|compare|chart|graph|visuali[sz]e?)\b",
                    request.message,
                    re.I,
                )
            )
            or (
                deterministic_plan.object in {"contact", "company"}
                and (deterministic_plan.group_by is not None or deterministic_plan.chart_requested)
            )
            or re.search(
                r"\btop\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
                request.message,
                re.I,
            )
        ):
            plan = deterministic_plan

        catalog = []
        if plan.object in {"company", "contact"}:
            if not getattr(self._tools, "property_discovery_available", False):
                if plan.group_by or plan.aggregations or plan.chart_requested:
                    message = unsupported_intelligence_message(plan) or (
                        "That analysis is not supported yet."
                    )
                    return AgentResponse(
                        status="ok",
                        text=message,
                        request_id=request.request_id,
                        tools_used=[],
                        result={"kind": "crm_intelligence_unsupported", "plan": plan.model_dump()},
                    )
                return None
            try:
                return await self._run_generic_crm_query(request, plan)
            except UnsupportedCRMField as exc:
                return AgentResponse(
                    status="ok",
                    text=str(exc),
                    request_id=request.request_id,
                    tools_used=[],
                    result={"kind": "crm_intelligence_unsupported", "plan": plan.model_dump()},
                )
            except IntegrationRateLimitError:
                return self._safe(
                    "hubspot_rate_limited",
                    "HubSpot is temporarily rate limiting requests. "
                    "Please wait briefly and try again.",
                    request,
                )
            except IntegrationTimeoutError:
                return self._safe(
                    "hubspot_timeout",
                    "HubSpot did not respond in time. Please try again shortly.",
                    request,
                )
            except IntegrationError:
                logger.exception("CRM query failed", extra={"tenant_id": request.tenant_id})
                return self._safe(
                    "unavailable",
                    "I couldn't retrieve those HubSpot records right now. "
                    "Check the requested fields or try again shortly.",
                    request,
                )
        if plan.object == "deal" and getattr(self._tools, "property_discovery_available", False):
            try:
                catalog = await self._tools.discover_properties(request.tenant_id, "deals")
                plan = await self._crm_planner.plan(
                    request.message,
                    conversation_context=request.conversation_context,
                    property_catalog=[
                        {"name": item.name, "label": item.label, "type": item.type}
                        for item in catalog
                    ],
                )
                plan = self._recover_missing_date_range(plan, request.message)
                plan = resolve_catalog_fields(plan, catalog)
            except IntegrationError:
                # Stage 1's standard-property path remains available for tenants whose
                # HubSpot grant does not include schema-read permission.
                catalog = []

        if self._is_unsupported_intelligence_request(plan) and not catalog:
            message = unsupported_intelligence_message(plan) or (
                "I can't run that analysis yet. CRM analytics currently cover deals only."
            )
            return AgentResponse(
                status="ok",
                text=message,
                request_id=request.request_id,
                tools_used=[],
                result={"kind": "crm_intelligence_unsupported", "plan": plan.model_dump()},
            )
        if not catalog and not self._supports_read_only_crm_intelligence(plan, request.message):
            return None

        try:
            deal_properties = tuple(dict.fromkeys((
                *DEAL_LIST_PROPERTIES,
                *plan.properties,
                *(item.field for item in plan.filters),
                *(item.field for item in plan.sort),
                *(item.field for item in plan.aggregations),
            )))
            await _report_progress("Retrieving CRM data")
            all_deals = await self._tools.list_all_deals(
                request.tenant_id, properties=deal_properties
            )
            pipelines = await self._tools.deal_pipelines(request.tenant_id)
            await _report_progress("Analyzing results")
            analysis, text = analyze_deals(plan, list(all_deals), pipelines, request.message)
            rows = analysis["rows"]
            await self._resolve_owner_labels(request.tenant_id, rows)
            if plan.group_by == "hubspot_owner_id":
                owner_groups = [
                    {"hubspot_owner_id": row.get("label")}
                    for row in rows
                    if row.get("label") not in {None, "", "Unassigned owner"}
                ]
                await self._resolve_owner_labels(request.tenant_id, owner_groups)
                unresolved_groups = [
                    item for item in rows
                    if item.get("label") not in {None, "", "Unassigned owner"}
                ]
                for row, owner_group in zip(
                    unresolved_groups,
                    owner_groups,
                    strict=False,
                ):
                    previous_label = row.get("label")
                    resolved_label = owner_group["hubspot_owner_id"]
                    if previous_label and resolved_label != previous_label:
                        text = text.replace(f"*{previous_label}*:", f"*{resolved_label}*:")
                    row["label"] = resolved_label
                    row["stage"] = row["label"]
            payload: dict[str, object] = {
                "kind": "crm_intelligence",
                "plan": plan.model_dump(),
                "rows": rows,
                "summary": analysis["summary"],
            }
            if analysis.get("details"):
                details = analysis["details"]
                await self._resolve_owner_labels(request.tenant_id, details)
                try:
                    detail_urls = await self._tools.hubspot_record_urls(
                        request.tenant_id,
                        [("deals", str(row["id"])) for row in details if row.get("id")],
                    )
                except Exception:
                    detail_urls = []
                for row, url in zip(details, detail_urls, strict=False):
                    row["view_url"] = url
                payload["details"] = details
                payload["details_table"] = {
                    "columns": [
                        {"key": key, "label": label}
                        for key, label in (
                            ("dealname", "Deal name"),
                            ("amount", "Amount"),
                            ("stage", "Deal stage"),
                            ("closedate", "Closing date"),
                            ("hubspot_owner_id", "Owner"),
                            ("view_url", "View in HubSpot"),
                        )
                    ],
                    "rows": details,
                }
                text += (
                    f"\n*Latest deal details* (showing {len(details)} of "
                    f"{analysis['summary']['matched_records']}):"
                )
                for row in details:
                    amount = row.get("amount")
                    currency = row.get("deal_currency_code")
                    amount_text = (
                        f"{currency} {amount}"
                        if amount and currency
                        else (amount or "Not available")
                    )
                    close = _parse_hubspot_date(str(row.get("closedate") or ""))
                    close_text = (
                        f"{close.strftime('%b')} {close.day}, {close.year}"
                        if close
                        else "Not available"
                    )
                    link = f" <{row['view_url']}|Open in HubSpot>" if row.get("view_url") else ""
                    detail = (
                        f"\n- *{row['dealname']}* - {row['stage']} - "
                        f"{amount_text} - closes {close_text} - "
                        f"owner {row.get('hubspot_owner_id') or 'Unassigned'}{link}"
                    )
                    if len(text) + len(detail) > _SLACK_MESSAGE_MAX_LENGTH - 80:
                        text += "\nAdditional deal details omitted to fit Slack's message limit."
                        break
                    text += detail
            grouped = plan.group_by is not None
            if not grouped and not plan.aggregations and rows:
                try:
                    urls = await self._tools.hubspot_record_urls(
                        request.tenant_id,
                        [("deals", str(row["id"])) for row in rows if row.get("id")],
                    )
                except Exception:
                    logger.warning(
                        "HubSpot deal link resolution failed",
                        extra={"tenant_id": request.tenant_id},
                    )
                    urls = []
                for row, url in zip(rows, urls, strict=False):
                    row["view_url"] = url
                property_labels = {
                    "dealname": "Deal name",
                    "amount": "Amount",
                    "dealstage": "Deal stage",
                    "stage": "Deal stage",
                    "pipeline": "Pipeline",
                    "hubspot_owner_id": "Owner ID",
                    "closedate": "Closing date",
                    "createdate": "Creation date",
                }
                property_labels.update(
                    {item.name: item.label for item in catalog}
                )
                requested = [
                    "dealname",
                    *[
                        "stage" if prop == "dealstage" else prop
                        for prop in plan.properties
                        if prop != "dealname"
                    ],
                ]
                columns = [
                    {"key": key, "label": property_labels[key]}
                    for key in dict.fromkeys(requested)
                    if key in property_labels and any(row.get(key) is not None for row in rows)
                ]
                if urls:
                    columns.append({"key": "view_url", "label": "View in HubSpot"})
                payload["table"] = {"columns": columns, "rows": rows}
                range_label = plan.date_range.relative if plan.date_range else None
                if range_label == "this quarter":
                    start = plan.date_range.start
                    if start:
                        start_date = date.fromisoformat(start)
                        quarter = (start_date.month - 1) // 3 + 1
                        range_label = f"Q{quarter} {start_date.year}"
                    heading = f"Deals closing this quarter ({range_label})"
                elif plan.date_range:
                    range_start = plan.date_range.start
                    range_end = plan.date_range.end
                    if range_start and range_end and date.fromisoformat(range_end) == (
                        date.fromisoformat(range_start) + timedelta(days=1)
                    ):
                        exact_date = date.fromisoformat(range_start)
                        heading = (
                            f"Deals closing on {exact_date.strftime('%b')} "
                            f"{exact_date.day}, {exact_date.year}"
                        )
                    elif range_label and re.fullmatch(r"q[1-4] \d{4}", range_label):
                        heading = f"Deals closing in {range_label.upper()}"
                    else:
                        heading = f"Deals closing ({range_label or 'requested date range'})"
                else:
                    heading = "Latest deals" if any(
                        item.field in {"closedate", "createdate"} and item.direction == "desc"
                        for item in plan.sort
                    ) else "Deals"
                text_lines = text.splitlines()
                text = (
                    f"*{heading}*: showing {len(rows)} of "
                    f"{analysis['summary']['matched_records']} deals"
                )
                if len(text_lines) > 1:
                    text += "\n" + "\n".join(text_lines[1:])
                for row in rows:
                    amount = row.get("amount")
                    currency_code = row.get("deal_currency_code")
                    if amount is not None and currency_code:
                        row["amount"] = f"{currency_code} {amount}"
                    for date_key in ("closedate", "createdate"):
                        raw_date = row.get(date_key)
                        if raw_date:
                            parsed = _parse_hubspot_date(
                                str(raw_date),
                                plan.date_range.timezone if plan.date_range else "UTC",
                            )
                            row[date_key] = (
                                f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"
                                if parsed
                                else raw_date
                            )
                record_links = [
                    f"<{row['view_url']}|Open>"
                    for row in rows
                    if row.get("view_url")
                ]
                if record_links:
                    links_line = "\n*HubSpot records:* " + ", ".join(record_links)
                    if len(text) + len(links_line) <= _SLACK_MESSAGE_MAX_LENGTH:
                        text += links_line
            await _report_progress("Preparing your response")
            if plan.chart_requested and rows and grouped:
                chart = build_chart_payload(plan, rows)
                payload.update(chart)
                payload["chart_url"] = generate_chart_url(chart["chart"])
                payload["chart_title"] = f"Deals by {plan.group_by or 'stage'}"
                text += "\n_Chart shown below._"
            elif plan.chart_requested and rows and not grouped and plan.sort:
                chart_rows = rows[: plan.limit or len(rows)]
                labels = [str(row.get("dealname") or "Untitled deal") for row in chart_rows]
                values = [
                    float(_parse_decimal(str(row.get("amount") or "0").split()[-1]) or 0)
                    for row in chart_rows
                ]
                chart = build_chart_config(
                    plan.chart_type or "bar", "Top deals by amount", labels, values,
                    value_label="Deal amount",
                )
                payload.update({
                    "chart": chart,
                    "chart_type": plan.chart_type or "bar",
                    "chart_url": generate_chart_url(chart),
                    "chart_title": "Top deals by amount",
                })
                text += "\n_Chart shown below._"
            return AgentResponse(
                status="ok",
                text=text,
                request_id=request.request_id,
                tools_used=["list_all_deals", "deal_pipelines"],
                result=payload,
            )
        except ValueError:
            return self._safe(
                "hubspot_not_authorized",
                "HubSpot is not connected for this workspace.",
                request,
                ["list_all_deals", "deal_pipelines"],
            )
        except IntegrationRateLimitError:
            logger.warning(
                "CRM intelligence query rate limited",
                extra={"tenant_id": request.tenant_id},
            )
            return self._safe(
                "hubspot_rate_limited",
                "HubSpot is temporarily rate limiting requests. Please wait briefly and try again.",
                request,
                ["list_all_deals", "deal_pipelines"],
            )
        except IntegrationTimeoutError:
            logger.warning(
                "CRM intelligence query timed out",
                extra={"tenant_id": request.tenant_id},
            )
            return self._safe(
                "hubspot_timeout",
                "HubSpot did not respond in time. Please try again shortly.",
                request,
                ["list_all_deals", "deal_pipelines"],
            )
        except UnsupportedCRMField as exc:
            return AgentResponse(
                status="ok", text=str(exc), request_id=request.request_id, tools_used=[],
                result={"kind": "crm_intelligence_unsupported", "plan": plan.model_dump()},
            )
        except IntegrationError:
            logger.exception(
                "CRM intelligence query failed",
                extra={"tenant_id": request.tenant_id},
            )
            return self._safe(
                "unavailable",
                "I couldn't retrieve the CRM summary right now.",
                request,
                ["list_all_deals", "deal_pipelines"],
            )

    async def _run_generic_crm_query(
        self, request: AgentRequest, initial_plan: CRMQueryPlan
    ) -> AgentResponse:
        object_type = {"company": "companies", "contact": "contacts"}[initial_plan.object]
        metadata_scope = f"crm.schemas.{object_type}.read"
        metadata_scope_missing = False
        try:
            catalog = await self._tools.discover_properties(request.tenant_id, object_type)
        except IntegrationPermissionError as exc:
            if exc.scope != metadata_scope:
                raise
            metadata_scope_missing = True
            catalog = [
                HubSpotPropertyDefinition(name=name, label=label, type=kind)
                for name, label, kind in _STANDARD_QUERY_PROPERTIES[initial_plan.object]
            ]
        catalog_payload = [
            {
                "name": item.name,
                "label": item.label,
                "type": item.type,
                "options": item.options,
            }
            for item in catalog
        ]
        plan = await self._crm_planner.plan(
            request.message,
            conversation_context=request.conversation_context,
            property_catalog=catalog_payload,
        )
        # Keep a deterministic initial intent if a follow-up planner returns another object.
        if plan.object not in {"company", "contact"}:
            plan = initial_plan
        if (
            initial_plan.group_by is not None
            or initial_plan.aggregations
            or _LEAD_RETRIEVAL_REQUEST_PATTERN.search(request.message)
        ):
            plan = initial_plan.model_copy(update={"properties": plan.properties})
        plan = _ground_explicit_query_properties(request.message, plan, catalog)
        association_key = _requested_association(request.message, plan.object)
        if association_key == "associated_company":
            plan = plan.model_copy(update={
                "properties": [
                    prop for prop in plan.properties
                    if prop.casefold() not in {"company", "company_name", "associatedcompanyid"}
                ]
            })
        try:
            plan = resolve_catalog_fields(plan, catalog)
        except UnsupportedCRMField as exc:
            if not metadata_scope_missing:
                raise
            raise UnsupportedCRMField(
                f"{exc} HubSpot property metadata permission ({metadata_scope}) is missing, "
                "so only standard properties can be queried."
            ) from exc
        required = list(dict.fromkeys([
            *plan.properties,
            *(item.field for item in plan.filters),
            *(item.field for item in plan.sort),
            *(item.field for item in plan.aggregations),
            *(
                [plan.group_by]
                if plan.group_by and plan.group_by not in {"month", "quarter", "year"}
                else []
            ),
            *([plan.date_range.field] if plan.date_range else []),
            "name" if plan.object == "company" else "firstname",
            "lastname" if plan.object == "contact" else "name",
        ]))
        known = {item.name for item in catalog}
        await _report_progress("Retrieving CRM data")
        records = await (
            self._tools.list_all_companies(
                request.tenant_id, properties=[field for field in required if field in known]
            )
            if plan.object == "company"
            else self._tools.list_all_contacts(
                request.tenant_id, properties=[field for field in required if field in known]
            )
        )
        await _report_progress("Analyzing results")
        result = execute_record_query(plan, records, catalog)
        rows = result["rows"]
        option_labels = {
            item.name: {option.get("value"): option.get("label") for option in item.options}
            for item in catalog
            if item.options
        }
        for row in rows:
            for field, labels in option_labels.items():
                raw_value = row.get(field)
                if isinstance(raw_value, str) and raw_value in labels and labels[raw_value]:
                    row[f"{field}_raw"] = raw_value
                    row[field] = labels[raw_value]
        await self._resolve_owner_labels(request.tenant_id, rows)
        record_type = "companies" if plan.object == "company" else "contacts"
        association_type = association_key
        if association_type and rows:
            target_type = {
                "associated_company": "companies",
                "associated_deals": "deals",
                "associated_contacts": "contacts",
            }[association_type]

            async def enrich_associations(row: dict) -> None:
                record_id = str(row.get("id") or "")
                try:
                    associated = await self._tools.associated_records(
                        request.tenant_id,
                        from_type="contacts" if plan.object == "contact" else "companies",
                        from_id=record_id,
                        to_type=target_type,
                    )
                    row[association_type] = ", ".join(
                        str(item.properties.get("name") or item.properties.get(
                            "dealname" if target_type == "deals" else "firstname"
                        ) or item.id)
                        for item in associated
                    ) if associated else f"No associated {target_type}"
                    urls = await self._tools.hubspot_record_urls(
                        request.tenant_id, [(target_type, item.id) for item in associated]
                    ) if associated else []
                    row[f"{association_type}_links"] = [
                        {
                            "name": str(item.properties.get("name") or item.properties.get(
                                "dealname" if target_type == "deals" else "firstname"
                            ) or item.id),
                            "url": url,
                        }
                        for item, url in zip(associated, urls, strict=False)
                    ]
                except IntegrationError:
                    logger.warning(
                        "CRM association lookup failed",
                        extra={"tenant_id": request.tenant_id, "record_type": plan.object},
                    )
                    row[association_type] = f"Could not verify associated {target_type}"
                    row[f"{association_type}_links"] = []

            await gather(*(enrich_associations(row) for row in rows))
            result["columns"].extend([
                {"key": association_type, "label": {
                    "associated_company": "Associated company",
                    "associated_deals": "Associated deals",
                    "associated_contacts": "Associated contacts",
                }[association_type]},
                {"key": f"{association_type}_links", "label": "Association links"},
            ])
        if not plan.group_by and not plan.aggregations and rows:
            urls = await self._tools.hubspot_record_urls(
                request.tenant_id,
                [(record_type, str(row["id"])) for row in rows if row.get("id")],
            )
            for row, url in zip(rows, urls, strict=False):
                row["view_url"] = url
            result["columns"].append({"key": "view_url", "label": "View in HubSpot"})
        await _report_progress("Preparing your response")
        text = (
            f"Found {result['matched_records']} {record_type}. "
            f"Showing {result['total']} records."
        )
        if result.get("has_more"):
            text += f" Use offset {result['offset'] + result['total']} to view the next page."
        text = _format_batch2_slack_table(text, result.get("columns", []), rows)
        chart_url = None
        should_chart = bool(
            result.get("rows")
            and (
                plan.chart_requested
                or (plan.group_by is not None and plan.object in {"contact", "company"})
            )
        )
        if should_chart:
            chart_rows = result["rows"]
            value_key = next(
                (
                    f"{item.operation}_{item.field}"
                    for item in plan.aggregations
                    if item.operation in {"sum", "avg", "min", "max"}
                ),
                "count",
            )
            labels = [str(row.get("group") or "Unknown") for row in chart_rows]
            values = []
            for row in chart_rows:
                try:
                    values.append(float(row.get(value_key) or 0))
                except (TypeError, ValueError):
                    values.append(0.0)
            chart_type = plan.chart_type or "bar"
            chart = build_chart_config(
                chart_type,
                f"{record_type.title()} by {plan.group_by or 'requested metric'}",
                labels,
                values,
                value_label=(
                    "Record count"
                    if value_key == "count"
                    else value_key.replace("_", " ").title()
                ),
            )
            chart_url = generate_chart_url(chart)
            text += "\n_Chart shown below._"
        payload = {
            "kind": "crm_intelligence", "plan": plan.model_dump(), "rows": rows,
            "summary": {
                "matched_records": result["matched_records"],
                "returned_records": result["total"],
            },
            "table": {"columns": result.get("columns", []), "rows": rows},
            "pagination": {
                "offset": result["offset"],
                "limit": plan.limit,
                "has_more": result["has_more"],
            },
        }
        if chart_url:
            payload.update({
                "chart": chart,
                "chart_type": plan.chart_type or "bar",
                "chart_url": chart_url,
                "chart_title": (
                    f"{record_type.title()} by {plan.group_by or 'requested metric'}"
                ),
            })
        return AgentResponse(
            status="ok", text=text, request_id=request.request_id,
            tools_used=[
                "standard_properties_fallback" if metadata_scope_missing else "discover_properties",
                f"list_all_{record_type}",
                "hubspot_record_urls",
            ],
            result=payload,
        )

    async def _resolve_owner_labels(self, tenant_id: str, rows: list[dict]) -> None:
        owner_ids = {
            str(row.get("hubspot_owner_id"))
            for row in rows
            if row.get("hubspot_owner_id") not in {None, ""}
        }
        if not owner_ids:
            return
        try:
            owners = await self._tools.list_owners(tenant_id)
        except IntegrationError:
            # Keep stable owner IDs visible when the tenant has not granted owner-read.
            return
        names = {owner.id: owner.display_name for owner in owners}
        for row in rows:
            owner_id = row.get("hubspot_owner_id")
            if owner_id is not None:
                row["hubspot_owner_id"] = names.get(str(owner_id), owner_id)


    async def respond(
        self,
        request: AgentRequest,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> AgentResponse:
        token = _current_progress.set(on_progress)
        try:
            return await self._respond_internal(request)
        finally:
            _current_progress.reset(token)

    async def _respond_internal(self, request: AgentRequest) -> AgentResponse:
        confirmation_action_id = self._confirmation_action_id(request.message)

        if confirmation_action_id is not None:
            confirmed_action = await self._action_safety.confirm_and_claim_action(
                action_id=confirmation_action_id,
                tenant_id=request.tenant_id,
                actor_id=request.actor_id,
                request_fingerprint=request.message,
            )

            if confirmed_action is None:
                return self._safe(
                    "invalid_confirmation",
                    (
                        "That action could not be confirmed. "
                        "It may be expired, already used, or not belong to you."
                    ),
                    request,
                )
            if confirmed_action.action_type == "update_contact":
                try:
                    contact_id = str(confirmed_action.payload["contact_id"])

                    properties = {
                        key: value
                        for key, value in confirmed_action.payload.items()
                        if key != "contact_id"
                    }

                    contact = await self._tools.update_contact(
                        request.tenant_id,
                        contact_id,
                        properties,
                    )

                    await self._action_safety.complete_action(
                        action_id=confirmed_action.id,
                        tenant_id=request.tenant_id,
                        actor_id=request.actor_id,
                        request_id=request.request_id,
                        resource_type="contact",
                        resource_id=contact.id,
                        result={
                            "contact_id": contact.id,
                        },
                    )

                    return AgentResponse(
                        status="ok",
                        text=(
                            "Contact updated successfully in HubSpot.\n"
                            f"• Contact ID: `{contact.id}`"
                        ),
                        request_id=request.request_id,
                        tools_used=["update_contact"],
                        result={
                            "kind": "crm_records",
                            "title": "Contact Updated",
                            "hubspot_url": (
                                await self._tools.hubspot_record_urls(
                                    request.tenant_id, [("contacts", contact.id)]
                                )
                            )[0],
                        },
                    )

                except IntegrationError:
                    await self._action_safety.fail_action(
                        action_id=confirmed_action.id,
                        tenant_id=request.tenant_id,
                        actor_id=request.actor_id,
                        request_id=request.request_id,
                        resource_type="contact",
                        error_code="integration_error",
                    )

                    logger.exception(
                        "Contact update failed",
                        extra={"tenant_id": request.tenant_id},
                    )

                    return self._safe(
                        "unavailable",
                        "I couldn't update the HubSpot contact right now.",
                        request,
                        ["update_contact"],
                    )
            if confirmed_action.action_type == "delete_contact":
                try:
                    contact_id = str(
                        confirmed_action.payload["contact_id"]
                    )

                    await self._tools.delete_contact(
                        request.tenant_id,
                        contact_id,
                    )

                    await self._action_safety.complete_action(
                        action_id=confirmed_action.id,
                        tenant_id=request.tenant_id,
                        actor_id=request.actor_id,
                        request_id=request.request_id,
                        resource_type="contact",
                        resource_id=contact_id,
                        result={
                            "contact_id": contact_id,
                        },
                    )

                    return AgentResponse(
                        status="ok",
                        text=(
                            "Contact deleted successfully in HubSpot.\n"
                            f"• Contact ID: `{contact_id}`"
                        ),
                        request_id=request.request_id,
                        tools_used=["delete_contact"],
                        result={
                            "kind": "crm_records",
                            "title": "Contact Archived",
                            "hubspot_url": (
                                await self._tools.hubspot_record_urls(
                                    request.tenant_id, [("contacts", contact_id)]
                                )
                            )[0],
                        },
                    )

                except IntegrationError:
                    await self._action_safety.fail_action(
                        action_id=confirmed_action.id,
                        tenant_id=request.tenant_id,
                        actor_id=request.actor_id,
                        request_id=request.request_id,
                        resource_type="contact",
                        error_code="integration_error",
                    )

                    logger.exception(
                        "Contact deletion failed",
                        extra={"tenant_id": request.tenant_id},
                    )

                    return self._safe(
                        "unavailable",
                        "I couldn't delete the HubSpot contact right now.",
                        request,
                        ["delete_contact"],
                    )
            if confirmed_action.action_type in CONFIRMED_UPDATE_ACTIONS:
                return await self._operations.execute_confirmed_update(request, confirmed_action)
            if confirmed_action.action_type != "create_contact":
                # Never fall through to contact creation for an action type we don't know.
                await self._action_safety.fail_action(
                    action_id=confirmed_action.id,
                    tenant_id=request.tenant_id,
                    actor_id=request.actor_id,
                    request_id=request.request_id,
                    resource_type=confirmed_action.resource_type,
                    error_code="unsupported_action",
                )
                return self._safe(
                    "unsupported",
                    "That action type can't be confirmed.",
                    request,
                )
            try:
                contact = await self._tools.create_contact(
                    request.tenant_id,
                    confirmed_action.payload,
                )
                await self._action_safety.complete_action(
                    action_id=confirmed_action.id,
                    tenant_id=request.tenant_id,
                    actor_id=request.actor_id,
                    request_id=request.request_id,
                    resource_type="contact",
                    resource_id=contact.id,
                    result={
                        "contact_id": contact.id,
                    },
                )

                return AgentResponse(
                    status="ok",
                    text=(
                        "Contact created successfully in HubSpot.\n"
                        f"• Contact ID: `{contact.id}`\n"
                        f"• Email: "
                        f"{contact.properties.get('email') or 'Not provided'}\n"
                        f"• First name: "
                        f"{contact.properties.get('firstname') or 'Not provided'}\n"
                        f"• Last name: "
                        f"{contact.properties.get('lastname') or 'Not provided'}"
                    ),
                    request_id=request.request_id,
                    tools_used=["create_contact"],
                )
            except ValueError:
                await self._action_safety.fail_action(
                    action_id=confirmed_action.id,
                    tenant_id=request.tenant_id,
                    actor_id=request.actor_id,
                    request_id=request.request_id,
                    resource_type="contact",
                    error_code="duplicate",
                )
                return self._safe(
                    "duplicate",
                    "A contact with that email already exists in HubSpot.",
                    request,
                    ["create_contact"],
                )
            except IntegrationError:
                await self._action_safety.fail_action(
                    action_id=confirmed_action.id,
                    tenant_id=request.tenant_id,
                    actor_id=request.actor_id,
                    request_id=request.request_id,
                    resource_type="contact",
                    error_code="integration_error",
                )
                logger.exception(
                    "Contact creation failed",
                    extra={"tenant_id": request.tenant_id},
                )
                return self._safe(
                    "unavailable",
                    "I couldn't create the HubSpot contact right now.",
                    request,
                    ["create_contact"],
                )

        if _AMBIGUOUS_DEAL_TYPE_CREATE_PATTERN.search(request.message):
            return self._safe(
                "needs_clarification",
                "Did you mean to create a deal record? If so, what should it be called?",
                request,
            )

        await _report_progress("Understanding your request")

        if _DEAL_FOLLOW_UP_REQUEST_PATTERN.search(request.message):
            return await self._review_deals_for_follow_up(request)

        if _LEAD_QUALIFICATION_REQUEST_PATTERN.search(request.message) and not (
            _CONTACT_CREATE_PATTERN.search(request.message)
            or _CONTACT_UPDATE_PATTERN.search(request.message)
            or _CONTACT_DELETE_PATTERN.search(request.message)
        ):
            return await self._qualify_leads(request)

        if _LEAD_RETRIEVAL_REQUEST_PATTERN.search(request.message):
            return await self._respond_to_extraction(
                request,
                CRMIntentExtraction(
                    intent="crm_question", query="contact_list", confidence=1.0,
                    question=request.message,
                ),
            )

        company_for_contact = re.search(
            r"\bwhich\s+company\s+does\s+(?P<name>[A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,3})\s+work\s+for\b",
            request.message,
            re.I,
        )
        if company_for_contact:
            name_parts = company_for_contact.group("name").split()
            if len(name_parts) >= 2:
                return await self._respond_to_extraction(
                    request,
                    CRMIntentExtraction(
                        intent="crm_question", query="contact_company", confidence=1.0,
                        first_name=name_parts[0], last_name=" ".join(name_parts[1:]),
                        question=request.message,
                    ),
                )

        deal_contacts_match = re.search(
            r"\b(?:who\s+is\s+the\s+contact\s+person\s+for|which\s+contacts?\s+are\s+associated\s+with)\s+(?P<deal>.+?)\s+deal\b",
            request.message,
            re.I,
        )
        if deal_contacts_match:
            return await self._respond_to_extraction(
                request,
                CRMIntentExtraction(
                    intent="crm_question", query="deal_contacts", confidence=1.0,
                    deal_name=deal_contacts_match.group("deal").strip(),
                    question=request.message,
                ),
            )

        deal_owner_match = re.search(
            r"\bwho\s+owns\s+(?P<deal>.+?)\s+deal\b", request.message, re.I
        )
        if deal_owner_match:
            return await self._respond_to_extraction(
                request,
                CRMIntentExtraction(
                    intent="crm_question", query="deal_details", confidence=1.0,
                    deal_name=deal_owner_match.group("deal").strip(),
                    question=request.message,
                ),
            )

        extraction = await self._extract_intent(request)

        if extraction is None:
            explicit_create = self._explicit_create_extraction(request.message)
            is_legacy_deal_list = bool(
                _ALL_DEALS_LIST_PATTERN.search(request.message)
                or _ALL_OPEN_DEALS_LIST_PATTERN.search(request.message)
            )
            if (
                explicit_create is None
                and not _DEAL_CREATE_REQUEST_PATTERN.search(request.message)
                and not is_legacy_deal_list
            ):
                try:
                    fallback_plan = await self._crm_planner.plan(request.message)
                except ValueError:
                    fallback_plan = None
                if fallback_plan is not None and self._supports_read_only_crm_intelligence(
                    fallback_plan,
                    request.message,
                ):
                    return await self._respond_to_extraction(
                        request,
                        CRMIntentExtraction(
                            intent="crm_question",
                            confidence=1.0,
                            question=request.message,
                        ),
                    )

            if explicit_create is not None:
                return await self._respond_to_extraction(request, explicit_create)
            query: CRMQueryName
            if _ALL_CONTACTS_LIST_PATTERN.search(request.message):
                query = "contact_list"
            elif _ALL_COMPANIES_LIST_PATTERN.search(request.message):
                query = "company_list"
            elif _ALL_OPEN_DEALS_LIST_PATTERN.search(request.message):
                query = "open_deals"
            elif _DEAL_FOLLOW_UP_REQUEST_PATTERN.search(request.message):
                query = "deal_follow_up"
            elif _ALL_DEALS_LIST_PATTERN.search(request.message):
                query = "all_deals"
            else:
                return await self._respond_with_rules(request)
            return await self._respond_to_extraction(
                request,
                CRMIntentExtraction(
                    intent="crm_question",
                    query=query,
                    confidence=1.0,
                ),
                skip_intelligence=True,
            )

        explicit_create = self._explicit_create_extraction(request.message)
        if explicit_create is not None and extraction.intent in {
            "crm_question",
            "unsupported",
            explicit_create.intent,
        }:
            reclassified = extraction.intent != explicit_create.intent
            extraction = extraction.model_copy(
                update={
                    "intent": explicit_create.intent,
                    "confidence": (
                        explicit_create.confidence if reclassified else extraction.confidence
                    ),
                    "first_name": explicit_create.first_name or extraction.first_name,
                    "last_name": explicit_create.last_name or extraction.last_name,
                    "company_name": explicit_create.company_name or extraction.company_name,
                    "email": explicit_create.email or extraction.email,
                    "query": None,
                    "question": None,
                    "contact_action": explicit_create.contact_action,
                    "company_action": explicit_create.company_action,
                    "deal_action": None,
                    "associations": [],
                }
            )

        return await self._respond_to_extraction(request, extraction)

    async def _extract_intent(self, request: AgentRequest) -> CRMIntentExtraction | None:
        try:
            return await self._ai_service.generate(
                prompt_name="crm-intent/v2",
                variables={"message": request.message},
                output_schema=CRMIntentExtraction,
            )
        except Exception:
            # Intent extraction must never take the CRM down; fall back to the rule parser.
            logger.warning(
                "CRM intent extraction failed; using rule-based fallback",
                extra={"tenant_id": request.tenant_id},
                exc_info=True,
            )
            return None

    async def _respond_to_extraction(
        self,
        request: AgentRequest,
        extraction: CRMIntentExtraction,
        *,
        skip_intelligence: bool = False,
    ) -> AgentResponse:
        # The LLM only interprets the message. Which writes run directly and which need
        # confirmation is decided here: create runs directly; update/delete stay pending.
        if (
            extraction.intent == "crm_question"
            and _REVENUE_QUESTION_PATTERN.search(request.message)
        ):
            extraction = extraction.model_copy(
                update={"query": "closed_won_revenue"}
            )

        if extraction.intent == "crm_question" and extraction.chart_requested:
            resolved_query = extraction.query or deal_status_query(request.message)
            resolved_chart_type = _normalise_chart_type(extraction.chart_type)
            if resolved_chart_type is None:
                resolved_chart_type = _infer_chart_type(request.message, query=resolved_query)
            extraction = extraction.model_copy(
                update={
                    "intent": "crm_question",
                    "query": resolved_query,
                    "chart_type": resolved_chart_type,
                }
            )

        if extraction.intent == "unsupported":
            return self._safe("unsupported", _CAPABILITIES_TEXT, request)

        intelligence_response = (
            None
            if skip_intelligence
            else await self._route_crm_intelligence(request, extraction)
        )
        if intelligence_response is not None:
            return intelligence_response

        if (
            extraction.intent == "crm_question"
            and extraction.query is None
            and _ALL_CONTACTS_LIST_PATTERN.search(request.message)
        ):
            extraction = extraction.model_copy(update={"query": "contact_list"})

        # Detect structured deal filters for read queries. Keep any explicit deal_stage
        # extracted by the AI service even when the query is already set.
        if extraction.intent == "crm_question":
            deal_filter = detect_deal_filter(request.message)
            if extraction.deal_stage:
                stage_value = extraction.deal_stage.strip()
                if deal_filter is None:
                    deal_filter = DealFilterSpec(stages=[stage_value])
                else:
                    merged_stages = list(dict.fromkeys([*deal_filter.stages, stage_value]))
                    deal_filter = deal_filter.model_copy(update={"stages": merged_stages})
            if deal_filter is not None:
                extraction = extraction.model_copy(
                    update={
                        "query": extraction.query or deal_status_query(request.message),
                        "deal_filter": deal_filter.model_dump(),
                    }
                )

        if (
            extraction.intent in _WRITE_INTENTS
            and extraction.confidence < _MIN_WRITE_CONFIDENCE
        ):
            return self._safe(
                "needs_clarification",
                "I'm not sure what you'd like me to change in HubSpot. "
                "Could you rephrase the request?",
                request,
            )

        try:
            validated = validate_extraction(request.message, extraction)
        except ExtractionValidationError as exc:
            return self._safe("invalid_request", exc.user_message, request)

        if extraction.intent == "crm_question" and extraction.query == "deal_analysis":
            return await self._analyze_deal(request, validated.deal_name)
        if extraction.intent == "crm_question" and extraction.query == "deal_follow_up":
            return await self._review_deals_for_follow_up(request)
        if extraction.intent == "crm_question" and extraction.query == "lead_qualification":
            return await self._qualify_leads(request)

        # Companies, deals and links between records; contact-only requests continue below.
        if CRMOperations.handles(extraction, validated):
            return await self._operations.respond(
                request, extraction, validated, self._idempotency_key(request)
            )

        if validated.intent == "create_contact":
            if not _CONTACT_IDENTITY_PROPERTIES & validated.properties.keys():
                return self._safe(
                    "missing_fields",
                    "I need at least the contact's name or email address to create a contact.",
                    request,
                )
            return await self._create_contact_directly(
                request,
                _ContactDraft(validated.properties, validated.company_name),
            )

        if validated.intent == "update_contact":
            contact_id = await self._resolve_contact_id(request, validated)
            if isinstance(contact_id, AgentResponse):
                return contact_id
            if validated.company_name is not None:
                return self._safe(
                    "unsupported",
                    "Changing a contact's company isn't supported yet.",
                    request,
                )
            properties: dict[str, str | None] = dict(validated.properties)
            if validated.contact_id is None:
                has_name = bool(properties.get("firstname") or properties.get("lastname"))
                properties.pop("firstname", None)
                properties.pop("lastname", None)
                if not has_name:
                    properties.pop("email", None)
            if not properties:
                return self._safe(
                    "missing_fields",
                    "Tell me which contact fields to update and their new values.",
                    request,
                )
            return await self._propose_contact_update(
                request,
                ContactUpdateIntent(
                    contact_id=contact_id,
                    properties=properties,
                ),
            )

        if validated.intent == "delete_contact":
            contact_id = await self._resolve_contact_id(request, validated)
            if isinstance(contact_id, AgentResponse):
                return contact_id
            return await self._propose_contact_delete(
                request, ContactDeleteIntent(contact_id=contact_id)
            )

        return await self._answer_crm_question(
            request,
            validated.company_name or self._company_name(request.message),
            extraction.question,
        )

    async def _analyze_deal(self, request: AgentRequest, deal_name: str | None) -> AgentResponse:
        if not deal_name:
            return self._safe(
                "missing_fields",
                "Which deal should I analyze? Please include its name.",
                request,
            )

        try:
            await _report_progress("Retrieving CRM data")
            resolution = await self._tools.resolve_deal(request.tenant_id, deal_name)
            if resolution.status == "ambiguous":
                return self._safe(
                    "ambiguous",
                    f"I found multiple deals matching {deal_name}. Please be more specific.",
                    request,
                    ["resolve_deal"],
                )
            if resolution.deal is None:
                return self._safe(
                    "not_found",
                    f"I couldn't find a deal named {deal_name} in HubSpot.",
                    request,
                    ["resolve_deal"],
                )

            deal = await self._tools.get_deal(
                request.tenant_id,
                resolution.deal.id,
                properties=DEAL_ANALYSIS_PROPERTIES,
            )
            properties = deal.properties
            pipelines = await self._tools.deal_pipelines(request.tenant_id)
            pipeline = next(
                (item for item in pipelines if item.id == properties.get("pipeline")),
                None,
            )
            stage = next(
                (
                    item
                    for item in pipeline.stages
                    if item.id == properties.get("dealstage")
                ),
                None,
            ) if pipeline is not None else None
            if pipeline is None or stage is None:
                return self._safe(
                    "stage_unknown",
                    "I couldn't verify that this deal is open from its HubSpot pipeline "
                    "stage, so I won't generate closing recommendations.",
                    request,
                    ["resolve_deal", "get_deal", "deal_pipelines"],
                )
            await _report_progress("Analyzing results")
            is_closed = stage.metadata.get("isClosed", "").casefold()
            if is_closed == "true":
                return self._safe(
                    "closed_deal",
                    "This deal is already closed, so I won't generate closing recommendations.",
                    request,
                    ["resolve_deal", "get_deal", "deal_pipelines"],
                )
            if is_closed != "false":
                return self._safe(
                    "stage_unknown",
                    "I couldn't verify that this deal is open from its HubSpot pipeline "
                    "stage, so I won't generate closing recommendations.",
                    request,
                    ["resolve_deal", "get_deal", "deal_pipelines"],
                )

            today = datetime.now(UTC).date()
            activity_date = _hubspot_date(properties.get("notes_last_updated"))
            close_date = _hubspot_date(properties.get("closedate"))
            inactivity_days = (
                max((today - activity_date).days, 0) if activity_date is not None else None
            )
            close_date_overdue = close_date < today if close_date is not None else None
            missing_fields = [
                label
                for property_name, label in (
                    ("amount", "amount"),
                    ("hubspot_owner_id", "owner"),
                    ("closedate", "close date"),
                )
                if not properties.get(property_name)
            ]
            if activity_date is None:
                missing_fields.append("last activity date")
            if close_date is None and properties.get("closedate"):
                missing_fields.append("valid close date")

            facts: dict[str, object] = {
                "deal_name": properties.get("dealname") or deal_name,
                "amount": properties.get("amount"),
                "stage": stage.label,
                "pipeline": pipeline.label,
                "owner_id": properties.get("hubspot_owner_id"),
                "close_date": close_date.isoformat() if close_date else None,
                "last_activity_date": activity_date.isoformat() if activity_date else None,
                "inactivity_days": inactivity_days,
                "close_date_overdue": close_date_overdue,
                "days_past_close_date": (
                    (today - close_date).days
                    if close_date is not None and close_date_overdue
                    else 0 if close_date is not None else None
                ),
                "missing_fields": missing_fields,
                "stage_is_closed": False,
            }
            request_email_draft = bool(_EMAIL_DRAFT_REQUEST_PATTERN.search(request.message))
            await _report_progress("Preparing your response")
            analysis = await self._ai_service.generate(
                prompt_name="deal-analysis/v1",
                variables={
                    "crm_facts": facts,
                    "requested_email_draft": request_email_draft,
                },
                output_schema=DealAnalysis,
            )
            hubspot_url = (
                await self._tools.hubspot_record_urls(
                    request.tenant_id, [("deals", deal.id)]
                )
            )[0]
            return AgentResponse(
                status="ok",
                text=_format_deal_analysis(
                    analysis,
                    facts,
                    hubspot_url,
                    include_email_draft=request_email_draft,
                ),
                request_id=request.request_id,
                tools_used=[
                    "resolve_deal",
                    "get_deal",
                    "deal_pipelines",
                    "deal_analysis",
                    "hubspot_record_urls",
                ],
                result={
                    "kind": "deal_analysis",
                    "deal": facts,
                    "hubspot_url": hubspot_url,
                    "summary": analysis.summary,
                    "identified_issues": analysis.identified_issues,
                    "recommendations": analysis.recommendations,
                    "follow_up_email_draft": (
                        analysis.follow_up_email_draft if request_email_draft else None
                    ),
                },
            )
        except ValueError:
            return self._safe(
                "hubspot_not_authorized",
                "HubSpot is not connected for this workspace.",
                request,
            )
        except IntegrationError:
            logger.exception(
                "Deal analysis integration failed",
                extra={"tenant_id": request.tenant_id},
            )
            return self._safe(
                "unavailable",
                "I couldn't analyze the HubSpot deal right now.",
                request,
            )

    async def _review_deals_for_follow_up(self, request: AgentRequest) -> AgentResponse:
        try:
            await _report_progress("Retrieving CRM data")
            all_deals, pipelines = await gather(
                self._tools.list_all_deals(
                    request.tenant_id,
                    properties=DEAL_FOLLOW_UP_PROPERTIES,
                ),
                self._tools.deal_pipelines(request.tenant_id),
            )
            await _report_progress("Analyzing results")
        except ValueError:
            return self._safe(
                "hubspot_not_authorized",
                "HubSpot is not connected for this workspace.",
                request,
            )
        except IntegrationError:
            logger.exception(
                "Deal follow-up data retrieval failed",
                extra={"tenant_id": request.tenant_id},
            )
            return self._safe(
                "unavailable",
                "I couldn't retrieve HubSpot deals and pipeline stages right now.",
                request,
                ["list_all_deals", "deal_pipelines"],
            )

        pipeline_stages = {
            (pipeline.id, stage.id): (pipeline, stage)
            for pipeline in pipelines
            for stage in pipeline.stages
        }
        today = datetime.now(UTC).date()
        open_deals: list[_DealFollowUpCandidate] = []
        open_deal_count = 0
        unknown_stage_count = 0
        unknown_activity: list[HubSpotDeal] = []
        recently_active = 0

        for deal in all_deals:
            properties = deal.properties
            pipeline_stage = pipeline_stages.get(
                (properties.get("pipeline") or "", properties.get("dealstage") or "")
            )
            if pipeline_stage is None:
                unknown_stage_count += 1
                continue
            pipeline, stage = pipeline_stage
            is_closed = stage.metadata.get("isClosed", "").casefold()
            if is_closed == "true":
                continue
            if is_closed != "false":
                unknown_stage_count += 1
                continue

            open_deal_count += 1
            activity_date = _hubspot_date(properties.get("notes_last_updated"))
            if activity_date is None:
                unknown_activity.append(deal)
                continue

            inactive_days = max((today - activity_date).days, 0)
            if inactive_days <= 2:
                recently_active += 1
                continue
            if inactive_days >= 14:
                priority = "High"
            elif inactive_days >= 7:
                priority = "Medium"
            else:
                priority = "Low"
            open_deals.append(
                _DealFollowUpCandidate(
                    deal=deal,
                    pipeline=pipeline.label,
                    stage=stage.label,
                    activity_date=activity_date,
                    inactive_days=inactive_days,
                    priority=priority,
                )
            )

        priority_order = {"High": 0, "Medium": 1, "Low": 2}
        open_deals.sort(
            key=lambda item: (priority_order[item.priority], -item.inactive_days, item.deal.id)
        )
        follow_up_count = len(open_deals)
        selected = open_deals[: self._deal_follow_up_batch_size]
        unknown_to_show = unknown_activity[: min(self._deal_follow_up_batch_size, 5)]
        url_records: list[tuple[Literal["deals"], str]] = [
            ("deals", candidate.deal.id) for candidate in selected
        ] + [("deals", deal.id) for deal in unknown_to_show]

        try:
            record_urls = (
                await self._tools.hubspot_record_urls(request.tenant_id, url_records)
                if url_records
                else []
            )
        except ValueError:
            return self._safe(
                "hubspot_not_authorized",
                "HubSpot is not connected for this workspace.",
                request,
            )
        except IntegrationError:
            logger.exception(
                "HubSpot deal URL generation failed",
                extra={"tenant_id": request.tenant_id},
            )
            return self._safe(
                "unavailable",
                "I couldn't generate HubSpot links for the follow-up report.",
                request,
                ["hubspot_record_urls"],
            )

        selected_urls = record_urls[: len(selected)]
        unknown_urls = record_urls[len(selected):]
        await _report_progress("Preparing your response")
        analysis_results: list[_DealFollowUpResult] = []
        ai_failures = 0
        for candidate, hubspot_url in zip(selected, selected_urls, strict=True):
            properties = candidate.deal.properties
            close_date = _hubspot_date(properties.get("closedate"))
            missing_fields = [
                label
                for key, label in (
                    ("amount", "amount"),
                    ("hubspot_owner_id", "owner"),
                )
                if not properties.get(key)
            ]
            if close_date is None:
                missing_fields.append("valid close date")
            facts: dict[str, object] = {
                "deal_name": properties.get("dealname") or candidate.deal.id,
                "amount": properties.get("amount"),
                "pipeline": candidate.pipeline,
                "stage": candidate.stage,
                "owner_id": properties.get("hubspot_owner_id"),
                "close_date": close_date.isoformat() if close_date else None,
                "close_date_overdue": close_date < today if close_date else None,
                "last_activity_date": candidate.activity_date.isoformat(),
                "inactivity_days": candidate.inactive_days,
                "priority": candidate.priority,
                "follow_up_task_status": "Not verified",
                "missing_fields": missing_fields,
            }
            try:
                assessment = await self._ai_service.generate(
                    prompt_name="deal-follow-up/v1",
                    variables={"crm_facts": facts},
                    output_schema=DealFollowUpAssessment,
                )
            except IntegrationError:
                ai_failures += 1
                logger.exception(
                    "AI deal follow-up assessment failed",
                    extra={
                        "tenant_id": request.tenant_id,
                        "deal_id": candidate.deal.id,
                    },
                )
                assessment = None

            analysis_results.append(
                _DealFollowUpResult(candidate, facts, hubspot_url, assessment)
            )

        report_text = _format_deal_follow_up_report(
            open_deal_count=open_deal_count,
            follow_up_count=follow_up_count,
            analyzed=analysis_results,
            unknown_activity=[
                (deal, url) for deal, url in zip(unknown_to_show, unknown_urls, strict=True)
            ],
            unknown_activity_count=len(unknown_activity),
            recently_active_count=recently_active,
            unknown_stage_count=unknown_stage_count,
            batch_size=self._deal_follow_up_batch_size,
        )
        return AgentResponse(
            status="partial" if ai_failures else "ok",
            text=report_text,
            request_id=request.request_id,
            tools_used=[
                "list_all_deals",
                "deal_pipelines",
                "deal_follow_up",
                *(["deal_follow_up_ai"] if analysis_results else []),
                *(["hubspot_record_urls"] if url_records else []),
            ],
            result={
                "kind": "deal_follow_up",
                "open_deals_reviewed": open_deal_count,
                "deals_needing_follow_up": follow_up_count,
                "deals_ai_analyzed": len(analysis_results) - ai_failures,
                "deals_not_ai_analyzed_due_to_limit": max(
                    0, follow_up_count - len(selected)
                ),
                "unknown_activity_count": len(unknown_activity),
                "recently_active_count": recently_active,
                "unknown_stage_count": unknown_stage_count,
                "deals": [
                    {
                        **item.facts,
                        "deal_id": item.candidate.deal.id,
                        "hubspot_url": item.hubspot_url,
                        "task_status": "Not verified",
                        "summary": item.assessment.summary if item.assessment else None,
                        "reason": item.assessment.reason if item.assessment else None,
                        "recommendations": (
                            item.assessment.recommendations if item.assessment else []
                        ),
                        "follow_up_email_draft": (
                            item.assessment.follow_up_email_draft if item.assessment else None
                        ),
                    }
                    for item in analysis_results
                ],
            },
        )

    async def _qualify_leads(self, request: AgentRequest) -> AgentResponse:
        tenant_id = request.tenant_id
        try:
            await _report_progress("Retrieving CRM data")
            contacts, has_more = await self._tools.list_contacts_limited(
                tenant_id, self._lead_analysis_limit
            )
            company_by_contact = await self._companies_for_contacts(tenant_id, contacts)
            await _report_progress("Analyzing results")
        except ValueError:
            return self._safe(
                "hubspot_not_authorized",
                "HubSpot is not connected for this workspace.",
                request,
            )
        except IntegrationError:
            logger.exception("Lead data retrieval failed", extra={"tenant_id": tenant_id})
            return self._safe(
                "unavailable",
                "I couldn't retrieve HubSpot leads right now.",
                request,
                ["list_contacts_limited"],
            )

        today = datetime.now(UTC).date()
        leads = rank_leads(
            [
                score_lead(contact, company_by_contact.get(contact.id), today=today)
                for contact in contacts
            ]
        )
        selected = [lead for lead in leads if lead.priority != "Low"][
            : self._lead_ai_batch_size
        ] or leads[: min(self._lead_ai_batch_size, 3)]

        try:
            urls = (
                await self._tools.hubspot_record_urls(
                    tenant_id, [("contacts", lead.contact.id) for lead in selected]
                )
                if selected
                else []
            )
        except ValueError:
            return self._safe(
                "hubspot_not_authorized",
                "HubSpot is not connected for this workspace.",
                request,
            )
        except IntegrationError:
            logger.exception(
                "HubSpot contact URL generation failed", extra={"tenant_id": tenant_id}
            )
            return self._safe(
                "unavailable",
                "I couldn't generate HubSpot links for the lead report.",
                request,
                ["hubspot_record_urls"],
            )

        await _report_progress("Preparing your response")
        assessments = await gather(*(self._assess_lead(request, lead) for lead in selected))
        entries = [
            LeadReportEntry(lead, url, assessment)
            for lead, url, assessment in zip(selected, urls, assessments, strict=True)
        ]
        ai_failures = sum(item.assessment is None for item in entries)
        counts = {
            p: sum(1 for lead in leads if lead.priority == p) for p in ("High", "Medium", "Low")
        }
        return AgentResponse(
            status="partial" if ai_failures else "ok",
            text=format_lead_report(
                leads=leads,
                entries=entries,
                analysis_limit=self._lead_analysis_limit,
                has_more=has_more,
            ),
            request_id=request.request_id,
            tools_used=[
                "list_contacts_limited",
                "contact_company_associations",
                "lead_qualification",
                *(["lead_qualification_ai"] if entries else []),
                *(["hubspot_record_urls"] if entries else []),
            ],
            result={
                "kind": "lead_qualification",
                "leads_analyzed": len(leads),
                "high_priority": counts["High"],
                "medium_priority": counts["Medium"],
                "low_priority": counts["Low"],
                "more_contacts_not_analyzed": has_more,
                "leads": [
                    {
                        **item.lead.facts,
                        "contact_id": item.lead.contact.id,
                        "hubspot_url": item.hubspot_url,
                        "assessment": (
                            item.assessment.model_dump() if item.assessment else None
                        ),
                    }
                    for item in entries
                ],
            },
        )

    async def _companies_for_contacts(
        self, tenant_id: str, contacts: list[HubSpotContact]
    ) -> dict[str, HubSpotCompany]:
        """Primary associated company per contact, fetching each company once."""
        semaphore = Semaphore(_LEAD_CONCURRENCY)

        async def company_id_for(contact: HubSpotContact) -> str | None:
            async with semaphore:
                associations = await self._tools.contact_company_associations(
                    tenant_id, contact.id
                )
            return associations.results[0].company_id if associations.results else None

        company_ids = await gather(*(company_id_for(contact) for contact in contacts))
        unique_ids = sorted({cid for cid in company_ids if cid})

        async def fetch(company_id: str) -> HubSpotCompany:
            async with semaphore:
                return await self._tools.get_company(tenant_id, company_id)

        fetched = await gather(*(fetch(c) for c in unique_ids))
        companies = dict(zip(unique_ids, fetched, strict=True))
        return {
            contact.id: companies[cid]
            for contact, cid in zip(contacts, company_ids, strict=True)
            if cid
        }

    async def _assess_lead(
        self, request: AgentRequest, lead: ScoredLead
    ) -> LeadAssessment | None:
        try:
            return await self._ai_service.generate(
                prompt_name="lead-qualification/v1",
                variables={"crm_facts": lead.facts},
                output_schema=LeadAssessment,
            )
        except IntegrationError:
            logger.exception(
                "AI lead assessment failed",
                extra={"tenant_id": request.tenant_id, "contact_id": lead.contact.id},
            )
            return None

    async def _resolve_contact_id(
        self, request: AgentRequest, validated: ValidatedExtraction
    ) -> str | AgentResponse:
        if validated.contact_id:
            return validated.contact_id

        first_name = validated.properties.get("firstname")
        last_name = validated.properties.get("lastname")
        email = validated.properties.get("email")
        if not first_name and not last_name and not email:
            return self._safe(
                "missing_fields",
                "Which contact? Please include their name or email address.",
                request,
            )

        try:
            resolution = await self._tools.resolve_contact(
                request.tenant_id,
                first_name=first_name,
                last_name=last_name,
            )
            if resolution.contact is None and email:
                resolution = await self._tools.resolve_contact(request.tenant_id, email=email)
        except ValueError:
            return self._safe(
                "hubspot_not_authorized",
                "HubSpot is not connected for this workspace.",
                request,
            )
        except IntegrationError:
            logger.exception("Contact lookup failed", extra={"tenant_id": request.tenant_id})
            return self._safe(
                "unavailable", "I couldn't look up the HubSpot contact right now.", request
            )

        if resolution.status == "ambiguous":
            return self._safe(
                "contact_ambiguous",
                "I found multiple matching contacts. Please include the contact's email address.",
                request,
                ["resolve_contact"],
            )
        if resolution.contact is None:
            return self._safe(
                "contact_not_found",
                "I couldn't find that contact in HubSpot.",
                request,
                ["resolve_contact"],
            )
        return resolution.contact.id

    async def _respond_with_rules(self, request: AgentRequest) -> AgentResponse:
        """Deterministic parser used when LLM intent extraction is unavailable."""
        contact_intent = self._contact_create_intent(request.message)

        if contact_intent is not None:
            properties = {
                key: value
                for key, value in {
                    "email": contact_intent.email,
                    "firstname": contact_intent.firstname,
                    "lastname": contact_intent.lastname,
                }.items()
                if value
            }
            return await self._create_contact_directly(
                request,
                _ContactDraft(properties, None),
            )

        contact_update_intent = self._contact_update_intent(request.message)

        if contact_update_intent is not None:
            return await self._propose_contact_update(request, contact_update_intent)

        contact_delete_intent = self._contact_delete_intent(request.message)

        if contact_delete_intent is not None:
            return await self._propose_contact_delete(request, contact_delete_intent)

        company_name = self._company_name(request.message)

        if company_name is None:
            return self._safe(
                "unsupported",
                "Ask for information about a named company.",
                request,
            )

        try:
            company = await self._tools.find_company(
                request.tenant_id,
                company_name,
            )

            if company is None:
                return self._safe(
                    "not_found",
                    "I couldn't find that company in HubSpot.",
                    request,
                    ["find_company"],
                )

            return await self._summarize_company(request, company, ["find_company"])

        except ValueError:
            return self._safe(
                "hubspot_not_authorized",
                "HubSpot is not connected for this workspace.",
                request,
            )

        except IntegrationError:
            logger.exception(
                "Account intelligence integration failed",
                extra={"tenant_id": request.tenant_id},
            )
            return self._safe(
                "unavailable",
                "I couldn't retrieve account information right now.",
                request,
            )

    async def _answer_crm_question(
        self,
        request: AgentRequest,
        company_name: str | None,
        question: str | None,
    ) -> AgentResponse:
        if company_name is None:
            return self._safe(
                "unsupported",
                "Which company would you like to know about? I can currently answer "
                "questions about companies and their contacts.",
                request,
            )

        try:
            await _report_progress("Retrieving CRM data")
            resolution = await self._tools.resolve_company(request.tenant_id, company_name)

            if resolution.status == "ambiguous":
                return self._safe(
                    "ambiguous",
                    f"I found multiple companies matching {company_name}. "
                    "Could you be more specific?",
                    request,
                    ["resolve_company"],
                )

            if resolution.company is None:
                return self._safe(
                    "not_found",
                    f"I couldn't find a company named {company_name} in HubSpot.",
                    request,
                    ["resolve_company"],
                )

            return await self._summarize_company(
                request,
                resolution.company,
                ["resolve_company"],
                question=question or request.message,
            )

        except ValueError:
            return self._safe(
                "hubspot_not_authorized",
                "HubSpot is not connected for this workspace.",
                request,
            )

        except IntegrationError:
            logger.exception(
                "Account intelligence integration failed",
                extra={"tenant_id": request.tenant_id},
            )
            return self._safe(
                "unavailable",
                "I couldn't retrieve account information right now.",
                request,
            )

    async def _summarize_company(
        self,
        request: AgentRequest,
        company: HubSpotCompany,
        tools_used: list[str],
        *,
        question: str | None = None,
    ) -> AgentResponse:
        contacts = []
        tools_used = list(tools_used)

        if self._needs_contacts(request.message):
            contacts = await self._tools.contacts_for_company(
                request.tenant_id,
                company.id,
            )
            tools_used.extend(
                [
                    "find_contacts",
                    "contact_company_associations",
                ]
            )

        facts = {
            "company": company.model_dump(),
            "contacts": [item.model_dump() for item in contacts],
        }

        variables: dict[str, object] = {"crm": facts}
        if question is not None:
            variables["question"] = question

        await _report_progress("Preparing your response")
        summary = await self._ai_service.generate(
            prompt_name="account-intelligence/v1",
            variables=variables,
            output_schema=GroundedSummary,
        )

        return AgentResponse(
            status="ok",
            text=self._format(summary),
            request_id=request.request_id,
            tools_used=tools_used,
        )

    async def _create_contact_directly(
        self,
        request: AgentRequest,
        draft: _ContactDraft,
    ) -> AgentResponse:
        claim = await self._action_safety.start_direct_action(
            idempotency_key=self._idempotency_key(request),
            tenant_id=request.tenant_id,
            actor_id=request.actor_id,
            action_type="create_contact",
            resource_type="contact",
            payload={**draft.properties, "company_name": draft.company_name},
            request_fingerprint=hashlib.sha256(request.message.encode()).hexdigest(),
        )

        if not claim.claimed:
            return self._safe(
                "duplicate_request",
                "This message was already processed.",
                request,
            )

        tools_used: list[str] = []
        company: HubSpotCompany | None = None

        try:
            if draft.company_name is not None:
                tools_used.append("resolve_company")
                resolution = await self._tools.resolve_company(
                    request.tenant_id,
                    draft.company_name,
                )

                if resolution.status == "ambiguous":
                    await self._fail_direct_action(request, claim.action_id, "company_ambiguous")
                    return self._safe(
                        "company_ambiguous",
                        f"I found multiple companies matching {draft.company_name}, "
                        "so I didn't create the contact.",
                        request,
                        tools_used,
                    )

                if resolution.company is None:
                    await self._fail_direct_action(request, claim.action_id, "company_not_found")
                    return self._safe(
                        "company_not_found",
                        f"I couldn't find a company named {draft.company_name}, "
                        "so I didn't create the contact.",
                        request,
                        tools_used,
                    )

                company = resolution.company

            tools_used.append("create_contact")
            contact = await self._tools.create_contact(
                request.tenant_id,
                dict(draft.properties),
                company_id=company.id if company is not None else None,
            )

        except HubSpotDuplicateContactError as exc:
            await self._fail_direct_action(request, claim.action_id, "duplicate")
            return self._safe(
                "duplicate",
                f"A contact with the email {exc.email} already exists in HubSpot "
                f"(contact ID `{exc.contact_id}`), so I didn't create a new one.",
                request,
                tools_used,
            )

        except ValueError:
            # Raised when the tenant has no usable HubSpot OAuth connection.
            await self._fail_direct_action(request, claim.action_id, "hubspot_not_authorized")
            return self._safe(
                "hubspot_not_authorized",
                "HubSpot is not connected for this workspace.",
                request,
                tools_used,
            )

        except IntegrationError:
            await self._fail_direct_action(request, claim.action_id, "integration_error")
            logger.exception(
                "Contact creation failed",
                extra={"tenant_id": request.tenant_id},
            )
            return self._safe(
                "unavailable",
                "I couldn't create the HubSpot contact right now.",
                request,
                tools_used,
            )

        try:
            await self._action_safety.complete_action(
                action_id=claim.action_id,
                tenant_id=request.tenant_id,
                actor_id=request.actor_id,
                request_id=request.request_id,
                resource_type="contact",
                resource_id=contact.id,
                result={
                    "contact_id": contact.id,
                    "company_id": company.id if company is not None else None,
                },
            )
        except Exception:
            # The contact exists in HubSpot, so report success; the action record needs review.
            logger.exception(
                "Created contact but could not record completion",
                extra={"tenant_id": request.tenant_id},
            )

        display_name = (
            " ".join(
                value
                for value in (
                    draft.properties.get("firstname"),
                    draft.properties.get("lastname"),
                )
                if value
            )
            or draft.properties.get("email")
            or contact.id
        )

        result: dict[str, object] = {
            "kind": "contact_created",
            "contact_id": contact.id,
            "name": display_name,
        }
        result["hubspot_url"] = (
            await self._tools.hubspot_record_urls(
                request.tenant_id, [("contacts", contact.id)]
            )
        )[0]
        if draft.properties.get("email"):
            result["email"] = draft.properties["email"]

        if company is not None:
            company_label = company.properties.get("name") or draft.company_name
            result["company_name"] = company_label or company.id
            summary = (
                f"Contact {display_name} was created successfully "
                f"and associated with {company_label}."
            )
        else:
            summary = f"Contact {display_name} was created successfully in HubSpot."

        return AgentResponse(
            status="ok",
            text=f"{summary}\n• Contact ID: `{contact.id}`",
            request_id=request.request_id,
            tools_used=tools_used,
            result=result,
        )

    async def _fail_direct_action(
        self,
        request: AgentRequest,
        action_id: str,
        error_code: str,
    ) -> None:
        await self._action_safety.fail_action(
            action_id=action_id,
            tenant_id=request.tenant_id,
            actor_id=request.actor_id,
            request_id=request.request_id,
            resource_type="contact",
            error_code=error_code,
        )

    async def _propose_contact_update(
        self,
        request: AgentRequest,
        contact_update_intent: ContactUpdateIntent,
    ) -> AgentResponse:
        action_id = await self._action_safety.create_pending_action(
            tenant_id=request.tenant_id,
            actor_id=request.actor_id,
            action_type="update_contact",
            resource_type="contact",
            payload={
                "contact_id": contact_update_intent.contact_id,
                **contact_update_intent.properties,
            },
        )

        return AgentResponse(
            status="pending_confirmation",
            text=(
                "I found a request to update this HubSpot contact:\n"
                f"• Contact ID: {contact_update_intent.contact_id}\n"
                f"• Fields: "
                f"{', '.join(contact_update_intent.properties.keys())}\n\n"
                f"Action ID: `{action_id}`\n"
                f"Reply with `confirm {action_id}` to update this contact."
            ),
            request_id=request.request_id,
            tools_used=[],
            result={
                "kind": "pending_confirmation",
                "action_id": action_id,
                "action_type": "update_contact",
                "contact_id": contact_update_intent.contact_id,
            },
        )

    async def _propose_contact_delete(
        self,
        request: AgentRequest,
        contact_delete_intent: ContactDeleteIntent,
    ) -> AgentResponse:
        action_id = await self._action_safety.create_pending_action(
            tenant_id=request.tenant_id,
            actor_id=request.actor_id,
            action_type="delete_contact",
            resource_type="contact",
            payload={
                "contact_id": contact_delete_intent.contact_id,
            },
        )

        return AgentResponse(
            status="pending_confirmation",
            text=(
                "I found a request to delete this HubSpot contact:\n"
                f"• Contact ID: {contact_delete_intent.contact_id}\n\n"
                f"Action ID: `{action_id}`\n"
                f"Reply with `confirm {action_id}` to delete this contact."
            ),
            request_id=request.request_id,
            tools_used=[],
            result={
                "kind": "pending_confirmation",
                "action_id": action_id,
                "action_type": "delete_contact",
                "contact_id": contact_delete_intent.contact_id,
            },
        )

    @staticmethod
    def _idempotency_key(request: AgentRequest) -> str:
        if request.channel_id and request.message_ts:
            return f"slack-message:{request.channel_id}:{request.message_ts}"
        if request.event_id:
            return f"slack-event:{request.event_id}"
        return f"request:{request.request_id}"

    @staticmethod
    def _confirmation_action_id(message: str) -> str | None:
        matched = _CONFIRM_ACTION_PATTERN.match(message)
        return matched.group(1) if matched else None

    @staticmethod
    def _contact_create_intent(
        message: str,
    ) -> ContactCreateIntent | None:
        if not _CONTACT_CREATE_PATTERN.search(message):
            return None

        email_match = _EMAIL_PATTERN.search(message)

        if email_match is None:
            return None

        firstname_match = _FIRSTNAME_PATTERN.search(message)
        lastname_match = _LASTNAME_PATTERN.search(message)

        return ContactCreateIntent(
            email=email_match.group(0),
            firstname=(firstname_match.group(1) if firstname_match else None),
            lastname=(lastname_match.group(1) if lastname_match else None),
        )

    @staticmethod
    def _explicit_create_extraction(message: str) -> CRMIntentExtraction | None:
        matched = _EXPLICIT_CREATE_PATTERN.search(message)
        if matched is None:
            return None

        entity = matched.group("entity").casefold()
        details = matched.group("details")
        name_match = _CREATE_NAME_PATTERN.search(details)
        name = name_match.group("name").strip(" \t'\"`.,;:!?") if name_match else None
        email_match = _EMAIL_PATTERN.search(details)
        firstname_match = _FIRSTNAME_PATTERN.search(details)
        lastname_match = _LASTNAME_PATTERN.search(details)

        first_name = firstname_match.group(1) if firstname_match else None
        last_name = lastname_match.group(1) if lastname_match else None
        contact_name_match = (
            _CONTACT_NAME_WITH_LAST_PATTERN.search(details) if entity == "contact" else None
        )
        if contact_name_match is not None:
            first_name = contact_name_match.group("first")
            last_name = contact_name_match.group("last")
        if entity == "contact" and name and not first_name and not last_name:
            parts = name.split(maxsplit=1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else None

        return CRMIntentExtraction(
            intent="create_company" if entity == "company" else "create_contact",
            confidence=0.95,
            first_name=first_name,
            last_name=last_name,
            email=email_match.group(0) if email_match else None,
            company_name=name if entity == "company" else None,
            company_action="create" if entity == "company" else None,
            contact_action="create" if entity == "contact" else None,
        )

    @staticmethod
    def _contact_update_intent(
        message: str,
    ) -> ContactUpdateIntent | None:
        if not _CONTACT_UPDATE_PATTERN.search(message):
            return None

        contact_id_match = _CONTACT_ID_PATTERN.search(message)

        if contact_id_match is None:
            return None

        properties: dict[str, str | None] = {}

        firstname_match = _FIRSTNAME_PATTERN.search(message)
        lastname_match = _LASTNAME_PATTERN.search(message)
        email_match = _EMAIL_PATTERN.search(message)
        jobtitle_match = _JOBTITLE_PATTERN.search(message)

        if firstname_match:
            properties["firstname"] = firstname_match.group(1)

        if lastname_match:
            properties["lastname"] = lastname_match.group(1)

        if email_match:
            properties["email"] = email_match.group(0)

        if jobtitle_match:
            properties["jobtitle"] = jobtitle_match.group(1).strip()

        if not properties:
            return None

        return ContactUpdateIntent(
            contact_id=contact_id_match.group(1),
            properties=properties,
        )

    @staticmethod
    def _contact_delete_intent(
        message: str,
    ) -> ContactDeleteIntent | None:
        if not _CONTACT_DELETE_PATTERN.search(message):
            return None

        contact_id_match = _CONTACT_ID_PATTERN.search(message)

        if contact_id_match is None:
            return None

        return ContactDeleteIntent(
            contact_id=contact_id_match.group(1),
        )

    @staticmethod
    def _company_name(message: str) -> str | None:
        matched = _ACCOUNT_PATTERN.search(message.strip())
        return matched.group(1).strip(" '\"") if matched else None

    @staticmethod
    def _needs_contacts(message: str) -> bool:
        return any(word in message.casefold() for word in ("contact", "people", "stakeholder"))

    @staticmethod
    def _format(summary: GroundedSummary) -> str:
        facts = "\n".join(f"• {fact}" for fact in summary.crm_facts) or "• No CRM facts returned."

        observations = (
            "\n".join(f"• {item}" for item in summary.observations) or "• No observations."
        )

        return f"*CRM facts*\n{facts}\n\n*AI observations/suggestions*\n{observations}"

    @staticmethod
    def _safe(
        status: str,
        text: str,
        request: AgentRequest,
        tools: list[str] | None = None,
    ) -> AgentResponse:
        return AgentResponse(
            status=status,
            text=text,
            request_id=request.request_id,
            tools_used=tools or [],
        )


def _hubspot_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        if value.isdigit():
            return datetime.fromtimestamp(int(value) / 1000, UTC).date()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, OverflowError, OSError):
        return None


def _format_deal_analysis(
    analysis: DealAnalysis,
    facts: dict[str, object],
    hubspot_url: str,
    *,
    include_email_draft: bool,
) -> str:
    inactivity_days = facts["inactivity_days"]
    close_date_overdue = facts["close_date_overdue"]
    if isinstance(inactivity_days, int):
        activity_days = str(inactivity_days)
        activity_status = (
            "Active today" if inactivity_days == 0 else f"Inactive for {inactivity_days} days"
        )
    else:
        activity_days = "Unknown"
        activity_status = "Unknown (no activity date in HubSpot)"
    if close_date_overdue is True:
        close_status = f"Overdue by {facts['days_past_close_date']} days"
    elif close_date_overdue is False:
        close_status = "Not overdue"
    else:
        close_status = "Unknown (no valid close date in HubSpot)"

    lines = [
        f"*Deal closing analysis: {facts['deal_name']}*",
        f"• Amount: {facts['amount'] or 'Not provided'}",
        f"• Pipeline / stage: {facts['pipeline']} / {facts['stage']}",
        f"• Owner ID: {facts['owner_id'] or 'Not assigned'}",
        f"• Close date: {facts['close_date'] or 'Not provided'} ({close_status})",
        f"• Last activity date: {facts['last_activity_date'] or 'Unknown'}",
        f"• Inactive days: {activity_days}",
        f"• Activity status: {activity_status}",
        f"• HubSpot: {hubspot_url}",
        "",
        f"*Summary*\n{analysis.summary}",
        "",
        "*Identified issues*",
    ]
    if analysis.identified_issues:
        lines.extend(f"• {issue}" for issue in analysis.identified_issues)
    else:
        lines.append("• No additional issues identified.")
    lines.extend(["", "*Next best actions*"])
    lines.extend(
        f"{index}. {recommendation}"
        for index, recommendation in enumerate(analysis.recommendations, start=1)
    )
    if include_email_draft and analysis.follow_up_email_draft:
        lines.extend(["", "*Follow-up email draft*"])
        lines.append(analysis.follow_up_email_draft)
    return "\n".join(lines)


def _format_deal_follow_up_report(
    *,
    open_deal_count: int,
    follow_up_count: int,
    analyzed: list[_DealFollowUpResult],
    unknown_activity: list[tuple[HubSpotDeal, str]],
    unknown_activity_count: int,
    recently_active_count: int,
    unknown_stage_count: int,
    batch_size: int,
) -> str:
    ai_successes = sum(item.assessment is not None for item in analyzed)
    lines = [
        "*AI-powered deal follow-up*",
        f"• Open deals reviewed: {open_deal_count}",
        f"• Deals needing follow-up: {follow_up_count}",
        f"• AI assessments generated: {ai_successes}/{len(analyzed)}",
        f"• AI analysis batch limit: {batch_size}",
        f"• Recently active (0–2 days): {recently_active_count}",
        f"• Activity date unknown (not counted as inactive): {unknown_activity_count}",
        f"• Deals excluded because stage was unverified: {unknown_stage_count}",
        "• Existing follow-up tasks: Not verified (task retrieval is not available)",
    ]
    if follow_up_count == 0:
        lines.extend(
            ["", "No deals with a known activity date met the 3+ day follow-up threshold."]
        )
    elif len(analyzed) < follow_up_count:
        lines.extend(
            [
                "",
                f"{follow_up_count - len(analyzed)} additional deal(s) need follow-up "
                "but were not AI-analyzed because of the batch limit.",
            ]
        )

    entries: list[str] = []
    for item in analyzed:
        candidate = item.candidate
        facts = item.facts
        close_date = facts["close_date"]
        close_status = (
            "Overdue"
            if facts["close_date_overdue"] is True
            else "Not overdue"
            if facts["close_date_overdue"] is False
            else "Unknown"
        )
        deal_name = _slack_value(facts["deal_name"], 160)
        deal_lines = [
            f"*{deal_name}* — {candidate.priority} priority",
            f"• Amount: {_slack_value(facts['amount'], 80)}",
            f"• Pipeline / stage: {_slack_value(facts['pipeline'], 100)} / "
            f"{_slack_value(facts['stage'], 100)}",
            f"• Owner ID: {_slack_value(facts['owner_id'], 80)}",
            f"• Expected close date: {_slack_value(close_date, 40)} ({close_status})",
            f"• Last activity: {candidate.activity_date.isoformat()} "
            f"({candidate.inactive_days} inactive days)",
            f"• Missing CRM fields: {_slack_value(facts['missing_fields'], 240)}",
            "• Upcoming follow-up task: Not verified",
            f"• <{_slack_value(item.hubspot_url, 500)}|Open in HubSpot>",
        ]
        if item.assessment is None:
            deal_lines.extend(["", "AI recommendations could not be generated for this deal."])
        else:
            deal_lines.extend(
                [
                    "",
                    f"*Assessment*\n{item.assessment.summary}",
                    f"*Why follow-up is recommended*\n{item.assessment.reason}",
                    "*Prioritized next best actions*",
                    *(
                        f"{index}. {recommendation}"
                        for index, recommendation in enumerate(
                            item.assessment.recommendations,
                            start=1,
                        )
                    ),
                    "*Suggested email draft*",
                    item.assessment.follow_up_email_draft,
                ]
            )
        entries.append("\n".join(deal_lines))

    if unknown_activity:
        unknown_entries = [
            f"*Activity unknown:* {_slack_value(deal.properties.get('dealname') or deal.id, 160)} "
            f"• <{_slack_value(url, 500)}|Open in HubSpot>"
            for deal, url in unknown_activity
        ]
        entries.append("\n".join(["*Deals with missing activity dates*", *unknown_entries]))
        if unknown_activity_count > len(unknown_activity):
            entries.append(
                f"{unknown_activity_count - len(unknown_activity)} additional deal(s) "
                "have no activity date."
            )

    message = "\n".join(lines)
    omitted_entries = 0
    for entry_text in entries:
        separator = "\n\n"
        omission_note = "\n\nAdditional deal details were omitted to fit Slack's message limit."
        if len(message) + len(separator) + len(entry_text) + len(omission_note) > (
            _SLACK_MESSAGE_MAX_LENGTH
        ):
            omitted_entries += 1
            continue
        message += separator + entry_text
    if omitted_entries:
        note = (
            f"\n\n{omitted_entries} deal detail section(s) were omitted to fit "
            "Slack's message limit."
        )
        message = message[: _SLACK_MESSAGE_MAX_LENGTH - len(note)] + note
    return message


def _slack_value(value: object, max_length: int) -> str:
    if value is None or value == "":
        return "Not provided"
    return str(value).replace("\n", " ")[:max_length]


def _ground_explicit_query_properties(
    message: str,
    plan: CRMQueryPlan,
    catalog: list[HubSpotPropertyDefinition],
) -> CRMQueryPlan:
    """Retain catalog fields explicitly named by the user when the planner omits them."""
    positions: dict[str, int] = {}
    for definition in catalog:
        starts = [
            match.start()
            for label in (definition.label, definition.name)
            if label
            if (match := re.search(rf"(?<![\w]){re.escape(label)}(?![\w])", message, re.I))
        ]
        if starts:
            positions[definition.name] = min(starts)
    properties = list(dict.fromkeys([*plan.properties, *positions]))
    properties.sort(key=lambda name: positions.get(name, float("inf")))
    return plan.model_copy(update={"properties": properties})


def _format_batch2_slack_table(
    summary: str,
    columns: list[dict[str, str]],
    rows: list[dict],
    *,
    max_length: int = 39_000,
) -> str:
    """Render the Batch 2 table as compact Slack mrkdwn without dropping columns."""
    if not rows or not columns:
        return summary[:max_length]

    lines = [summary]
    included = 0
    for row in rows:
        cells: list[str] = []
        for column in columns:
            key = column["key"]
            label = column["label"]
            value = row.get(key)
            if key == "view_url" and value:
                rendered = f"<{value}|Open in HubSpot>"
            elif value is None or (isinstance(value, str) and not value.strip()):
                rendered = "Not available"
            else:
                rendered = str(value).replace("\n", " ").replace("\r", " ")
            cells.append(f"*{label}:* {rendered}")
        row_text = " | ".join(cells)
        if len("\n".join([*lines, row_text])) > max_length:
            break
        lines.append(row_text)
        included += 1

    if included < len(rows):
        notice = "Additional rows omitted to fit Slack's message limit."
        while lines and len("\n".join([*lines, notice])) > max_length:
            lines.pop()
        lines.append(notice)
    return "\n".join(lines)[:max_length]
