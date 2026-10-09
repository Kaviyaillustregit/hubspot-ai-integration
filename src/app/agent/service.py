import hashlib
import logging
import re
from dataclasses import dataclass

from app.agent.extraction import (
    ExtractionValidationError,
    ValidatedExtraction,
    validate_extraction,
)
from app.agent.operations import CONFIRMED_UPDATE_ACTIONS, OPERATION_INTENTS, CRMOperations
from app.agent.schemas import (
    AgentRequest,
    AgentResponse,
    ContactCreateIntent,
    ContactDeleteIntent,
    ContactUpdateIntent,
    CRMIntentExtraction,
    GroundedSummary,
)
from app.agent.tools import HubSpotToolRegistry
from app.ai.service import AIService
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.models import HubSpotCompany
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


class AccountIntelligenceAgent:
    def __init__(
        self,
        tools: HubSpotToolRegistry,
        ai_service: AIService,
        action_safety: ActionSafetyService,
    ) -> None:
        self._tools = tools
        self._ai_service = ai_service
        self._action_safety = action_safety
        self._operations = CRMOperations(tools, action_safety)

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

        extraction = await self._extract_intent(request)

        if extraction is None:
            explicit_create = self._explicit_create_extraction(request.message)
            if explicit_create is not None:
                return await self._respond_to_extraction(request, explicit_create)
            if _ALL_CONTACTS_LIST_PATTERN.search(request.message):
                query = "contact_list"
            elif _ALL_COMPANIES_LIST_PATTERN.search(request.message):
                query = "company_list"
            elif _ALL_OPEN_DEALS_LIST_PATTERN.search(request.message):
                query = "open_deals"
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
