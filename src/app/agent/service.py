import logging
import re

from app.agent.schemas import AgentRequest, AgentResponse, GroundedSummary
from app.agent.tools import HubSpotToolRegistry
from app.ai.service import AIService
from app.integrations.errors import IntegrationError

logger = logging.getLogger(__name__)
_ACCOUNT_PATTERN = re.compile(r"(?:about|for)\s+(.+?)[?.!]*$", re.IGNORECASE)


class AccountIntelligenceAgent:
    def __init__(self, tools: HubSpotToolRegistry, ai_service: AIService) -> None:
        self._tools = tools
        self._ai_service = ai_service

    async def respond(self, request: AgentRequest) -> AgentResponse:
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
