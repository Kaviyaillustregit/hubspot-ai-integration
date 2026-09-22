import logging
import re

from app.agent.schemas import (
    AgentRequest,
    AgentResponse,
    ContactCreateIntent,
    ContactDeleteIntent,
    ContactUpdateIntent,
    GroundedSummary,
)
from app.agent.tools import HubSpotToolRegistry
from app.ai.service import AIService
from app.integrations.errors import IntegrationError
from app.services.action_safety import ActionSafetyService

logger = logging.getLogger(__name__)

_ACCOUNT_PATTERN = re.compile(
    r"(?:about|for)\s+(.+?)[?.!]*$",
    re.IGNORECASE,
)

_CONTACT_CREATE_PATTERN = re.compile(
    r"\b(?:create|add)\s+(?:a\s+)?contact\b",
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
    r"^\s*confirm\s+([a-f0-9]{32})\s*$",
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
                    f"• First name: "
                    f"{contact_intent.firstname or 'Not provided'}\n"
                    f"• Last name: "
                    f"{contact_intent.lastname or 'Not provided'}\n\n"
                    f"Action ID: `{action_id}`\n"
                    f"Reply with `confirm {action_id}` to create this contact."
                ),
                request_id=request.request_id,
                tools_used=[],
            )

        contact_update_intent = self._contact_update_intent(request.message)

        if contact_update_intent is not None:
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
            )
        contact_delete_intent = self._contact_delete_intent(request.message)

        if contact_delete_intent is not None:
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
            )

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

            contacts = []
            tools_used = ["find_company"]

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
