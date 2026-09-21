import logging
import re

from app.agent.schemas import (
    AgentRequest,
    AgentResponse,
    ContactCreateIntent,
    GroundedSummary,
)
from app.agent.tools import HubSpotToolRegistry
from app.ai.service import AIService
from app.integrations.errors import IntegrationError
from app.services.action_safety import ActionSafetyService

logger = logging.getLogger(__name__)
_ACCOUNT_PATTERN = re.compile(r"(?:about|for)\s+(.+?)[?.!]*$", re.IGNORECASE)
_CONTACT_CREATE_PATTERN = re.compile(
    r"\b(?:create|add)\s+(?:a\s+)?contact\b",
    re.IGNORECASE,
)

_CONFIRM_ACTION_PATTERN = re.compile(
    r"^\s*confirm\s+([a-f0-9]{32})\s*$",
    re.IGNORECASE,
)

_EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
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

class AccountIntelligenceAgent:
    def __init__(
        self,
        tools: HubSpotToolRegistry,
        ai_service: AIService,
        action_safety: ActionSafetyService
    ) -> None:
        self._tools = tools
        self._ai_service = ai_service
        self._action_safety = action_safety

    async def respond(self, request: AgentRequest) -> AgentResponse:
        contact_intent = self._contact_create_intent(request.message)

        if contact_intent is not None:
            action_id = await self._action_safety.create_pending_action(
                tenant_id=request.tenant_id,
                actor_id=request.actor_id,
                action_type="create_contact",
                resource_type="contact",
                payload={
                    "email": contact_intent.email,
                    "firstname": contact_intent.firstname,
                    "lastname": contact_intent.lastname,
                },
            )

            return AgentResponse(
                status="pending_confirmation",
                text=(
                    "I found a request to create this HubSpot contact:\n"
                    f"• Email: {contact_intent.email}\n"
                    f"• First name: {contact_intent.firstname or 'Not provided'}\n"
                    f"• Last name: {contact_intent.lastname or 'Not provided'}\n\n"
                    f"Action ID: `{action_id}`\n"
                    "Confirmation is required before I create it."
                ),
                request_id=request.request_id,
                tools_used=[],
            )
        company_name = self._company_name(request.message)
        if company_name is None:
            return self._safe("unsupported", "Ask for information about a named company.", request)
        try:
            company = await self._tools.find_company(request.tenant_id, company_name)
            if company is None:
                return self._safe(
                    "not_found",
                    "I couldn't find that company in HubSpot.",
                    request,
                    ["find_company"],
                )
            contacts = []
            tools_used = ["find_company"]
            if self._needs_contacts(request.message):
                contacts = await self._tools.contacts_for_company(request.tenant_id, company.id)
                tools_used.extend(["find_contacts", "contact_company_associations"])
            facts = {
                "company": company.model_dump(),
                "contacts": [item.model_dump() for item in contacts],
            }
            summary = await self._ai_service.generate(
                prompt_name="account-intelligence/v1",
                variables={"crm": facts},
                output_schema=GroundedSummary,
            )
            return AgentResponse(
                status="ok",
                text=self._format(summary),
                request_id=request.request_id,
                tools_used=tools_used,
            )
        except ValueError:
            return self._safe(
                "hubspot_not_authorized", "HubSpot is not connected for this workspace.", request
            )
        except IntegrationError:
            logger.exception(
                "Account intelligence integration failed", extra={"tenant_id": request.tenant_id}
            )
            return self._safe(
                "unavailable", "I couldn't retrieve account information right now.", request
            )
    @staticmethod
    def _confirmation_action_id(message: str) -> str | None:
        matched = _CONFIRM_ACTION_PATTERN.match(message)
        return matched.group(1) if matched else None
    @staticmethod
    def _contact_create_intent(message: str) -> ContactCreateIntent | None:
        if not _CONTACT_CREATE_PATTERN.search(message):
            return None

        email_match = _EMAIL_PATTERN.search(message)
        if email_match is None:
            return None

        firstname_match = _FIRSTNAME_PATTERN.search(message)
        lastname_match = _LASTNAME_PATTERN.search(message)

        return ContactCreateIntent(
            email=email_match.group(0),
            firstname=firstname_match.group(1) if firstname_match else None,
            lastname=lastname_match.group(1) if lastname_match else None,
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
        status: str, text: str, request: AgentRequest, tools: list[str] | None = None
    ) -> AgentResponse:
        return AgentResponse(
            status=status, text=text, request_id=request.request_id, tools_used=tools or []
        )
