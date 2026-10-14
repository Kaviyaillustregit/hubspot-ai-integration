import hashlib
import logging
import re
from asyncio import Semaphore, gather
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

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
    HubSpotToolRegistry,
)
from app.ai.service import AIService
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.models import HubSpotCompany, HubSpotContact, HubSpotDeal
from app.services.action_safety import ActionSafetyService
from app.services.hubspot_contacts import HubSpotDuplicateContactError

logger = logging.getLogger(__name__)

_WRITE_INTENTS = frozenset({"create_contact", "update_contact", "delete_contact"}) | (
    OPERATION_INTENTS
)
_MIN_WRITE_CONFIDENCE = 0.6
_CONTACT_IDENTITY_PROPERTIES = frozenset({"firstname", "lastname", "email"})
_CAPABILITIES_TEXT = (
    "I can create, update, or delete HubSpot contacts and answer questions about "
    "companies and their contacts."
)


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
    r"\b(?:show|list|display|get|view|find)\b.*\b(?:all|every)\s+open\s+deals?\b",
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
        self._deal_follow_up_batch_size = deal_follow_up_batch_size
        self._lead_analysis_limit = lead_analysis_limit
        self._lead_ai_batch_size = lead_ai_batch_size

    async def respond(self, request: AgentRequest) -> AgentResponse:
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

        if _DEAL_FOLLOW_UP_REQUEST_PATTERN.search(request.message):
            return await self._review_deals_for_follow_up(request)

        if _LEAD_QUALIFICATION_REQUEST_PATTERN.search(request.message) and not (
            _CONTACT_CREATE_PATTERN.search(request.message)
            or _CONTACT_UPDATE_PATTERN.search(request.message)
            or _CONTACT_DELETE_PATTERN.search(request.message)
        ):
            return await self._qualify_leads(request)

        extraction = await self._extract_intent(request)

        if extraction is None:
            explicit_create = self._explicit_create_extraction(request.message)
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

        if extraction.intent == "unsupported":
            return self._safe("unsupported", _CAPABILITIES_TEXT, request)

        if (
            extraction.intent == "crm_question"
            and extraction.query is None
            and _ALL_CONTACTS_LIST_PATTERN.search(request.message)
        ):
            extraction = extraction.model_copy(update={"query": "contact_list"})

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
            all_deals, pipelines = await gather(
                self._tools.list_all_deals(
                    request.tenant_id,
                    properties=DEAL_FOLLOW_UP_PROPERTIES,
                ),
                self._tools.deal_pipelines(request.tenant_id),
            )
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
            contacts, has_more = await self._tools.list_contacts_limited(
                tenant_id, self._lead_analysis_limit
            )
            company_by_contact = await self._companies_for_contacts(tenant_id, contacts)
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
