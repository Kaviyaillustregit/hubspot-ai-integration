"""Company, deal and association operations for the CRM agent.

Contact-only requests keep their original code paths in AccountIntelligenceAgent. This
module handles requests involving companies, deals or links between records. The LLM's
extraction only says *what* was asked; the order of operations, record resolution,
confirmation policy and safety bookkeeping are decided here, deterministically.
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.agent.extraction import ValidatedExtraction
from app.agent.schemas import AgentRequest, AgentResponse, CRMIntentExtraction, CRMQueryName
from app.agent.tools import HubSpotToolRegistry
from app.integrations.errors import (
    IntegrationAuthenticationError,
    IntegrationError,
    IntegrationNotFoundError,
    IntegrationPermissionError,
)
from app.integrations.hubspot.associations import CRMObjectType
from app.integrations.hubspot.models import (
    HubSpotCompany,
    HubSpotContact,
    HubSpotDeal,
    HubSpotPipeline,
    HubSpotPipelineStage,
    HubSpotRecord,
)
from app.services.action_safety import ActionSafetyService, ConfirmedAction
from app.services.hubspot_contacts import HubSpotDuplicateContactError

logger = logging.getLogger(__name__)

Entity = Literal["contact", "company", "deal"]

OPERATION_INTENTS = frozenset(
    {
        "create_company",
        "update_company",
        "create_deal",
        "update_deal",
        "associate_records",
        "multi_step",
    }
)
CONFIRMED_UPDATE_ACTIONS = frozenset({"update_company", "update_deal"})

_IMPLIED_ACTION: dict[str, tuple[Entity, str]] = {
    "create_contact": ("contact", "create"),
    "create_company": ("company", "create"),
    "create_deal": ("deal", "create"),
    "update_company": ("company", "update"),
    "update_deal": ("deal", "update"),
}
# Link name -> (from object type, from entity, to object type, to entity), in execution order.
_LINKS: dict[str, tuple[CRMObjectType, Entity, CRMObjectType, Entity]] = {
    "contact_company": ("contacts", "contact", "companies", "company"),
    "deal_company": ("deals", "deal", "companies", "company"),
    "deal_contact": ("deals", "deal", "contacts", "contact"),
}
_LIST_LIMIT = 20


class OperationError(Exception):
    """A request that cannot proceed. The message is safe to show to the user."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class _Plan:
    company: HubSpotCompany | None = None
    company_to_create: dict[str, str] | None = None
    contact: HubSpotContact | None = None
    contact_to_create: dict[str, str] | None = None
    deal: HubSpotDeal | None = None
    deal_to_create: dict[str, str] | None = None
    deal_stage_label: str | None = None
    deal_name_derived: bool = False
    lines: list[str] = field(default_factory=list)
    written: dict[str, str] = field(default_factory=dict)
    # (entity, label, record id) for requested creates that matched an existing record.
    reused: list[tuple[str, str, str]] = field(default_factory=list)
    # Structured summaries of what was written, for the web assistant's result cards.
    cards: list[dict[str, str]] = field(default_factory=list)
    step: str = ""


class CRMOperations:
    def __init__(self, tools: HubSpotToolRegistry, action_safety: ActionSafetyService) -> None:
        self._tools = tools
        self._safety = action_safety

    @staticmethod
    def handles(extraction: CRMIntentExtraction, validated: ValidatedExtraction) -> bool:
        """True when a request goes beyond the original contact-only flows."""
        if extraction.intent in OPERATION_INTENTS:
            return True
        if extraction.intent == "crm_question":
            return extraction.query is not None
        if extraction.intent == "create_contact":
            return (
                extraction.deal_action is not None
                or validated.deal_name is not None
                or extraction.company_action in ("create", "update")
                or extraction.contact_action in ("reference", "update")
                or any(link != "contact_company" for link in extraction.associations)
            )
        return False

    async def respond(
        self,
        request: AgentRequest,
        extraction: CRMIntentExtraction,
        validated: ValidatedExtraction,
        idempotency_key: str,
    ) -> AgentResponse:
        try:
            if extraction.intent == "crm_question" and extraction.query is not None:
                return await self._answer_query(request, extraction.query, validated)
            actions = _entity_actions(extraction, validated)
            if "update" in actions.values():
                return await self._propose_update(request, extraction, validated, actions)
            return await self._run_plan(request, extraction, validated, actions, idempotency_key)
        except OperationError as exc:
            return _response(request, exc.status, exc.message)

    # ---------------------------------------------------------------- plans (direct)

    async def _run_plan(
        self,
        request: AgentRequest,
        extraction: CRMIntentExtraction,
        validated: ValidatedExtraction,
        actions: dict[Entity, str | None],
        idempotency_key: str,
    ) -> AgentResponse:
        links = _requested_links(extraction, actions)
        _check_plan_inputs(validated, actions, links)

        claim = await self._safety.start_direct_action(
            idempotency_key=idempotency_key,
            tenant_id=request.tenant_id,
            actor_id=request.actor_id,
            action_type="crm_plan",
            resource_type="crm",
            payload={"request": request.message[:500], "links": links},
            request_fingerprint=hashlib.sha256(request.message.encode()).hexdigest(),
        )
        if not claim.claimed:
            return _response(request, "duplicate_request", "This message was already processed.")

        plan = _Plan()
        try:
            await self._prepare(request, validated, actions, plan)
        except OperationError as exc:
            await self._fail(request, claim.action_id, exc.status)
            return _response(request, exc.status, exc.message)
        except (IntegrationError, ValueError) as exc:
            status, message = _hubspot_failure(exc)
            await self._fail(request, claim.action_id, status)
            return _response(request, status, message)

        try:
            await self._execute(request, validated, links, plan)
        except (IntegrationError, ValueError) as exc:
            status, message = _hubspot_failure(exc)
            logger.warning(
                "CRM plan step failed",
                extra={"tenant_id": request.tenant_id, "step": plan.step, "status": status},
            )
            failed_line = f"❌ {plan.step} failed: {message}"
            if not plan.written:
                await self._fail(request, claim.action_id, status)
                return _response(request, status, "\n".join([*plan.lines, failed_line]))
            await self._safety.record_partial_action(
                action_id=claim.action_id,
                tenant_id=request.tenant_id,
                actor_id=request.actor_id,
                request_id=request.request_id,
                resource_type="crm",
                result={**plan.written, "failed_step": plan.step, "error_code": status},
            )
            return AgentResponse(
                status="partial",
                text="\n".join(
                    [
                        *plan.lines,
                        failed_line,
                        "",
                        "The steps marked ✅ were saved in HubSpot; nothing was rolled back.",
                    ]
                ),
                request_id=request.request_id,
                cards=plan.cards,
            )

        if not plan.written:
            # Every record asked for already existed and nothing needed linking: the request
            # is satisfied by the existing records, which is a success, not an error.
            try:
                await self._safety.complete_action(
                    action_id=claim.action_id,
                    tenant_id=request.tenant_id,
                    actor_id=request.actor_id,
                    request_id=request.request_id,
                    resource_type="crm",
                    resource_id=plan.reused[0][2] if plan.reused else None,
                    result={
                        f"existing_{entity}_id": record_id
                        for entity, _, record_id in plan.reused
                    },
                )
            except Exception:
                logger.exception(
                    "Existing-record outcome was not recorded",
                    extra={"tenant_id": request.tenant_id},
                )
            return _existing_records_response(request, plan.reused)

        try:
            await self._safety.complete_action(
                action_id=claim.action_id,
                tenant_id=request.tenant_id,
                actor_id=request.actor_id,
                request_id=request.request_id,
                resource_type="crm",
                resource_id=next(iter(plan.written.values()), None),
                result=dict(plan.written),
            )
        except Exception:
            logger.exception(
                "CRM plan succeeded but completion was not recorded",
                extra={"tenant_id": request.tenant_id},
            )

        return AgentResponse(
            status="ok",
            text="\n".join(plan.lines),
            request_id=request.request_id,
            tools_used=[],
            result={"kind": "crm_records", "title": "CRM Updated", **_single_create_summary(plan)},
            cards=plan.cards,
        )

    async def _prepare(
        self,
        request: AgentRequest,
        validated: ValidatedExtraction,
        actions: dict[Entity, str | None],
        plan: _Plan,
    ) -> None:
        """Resolve every record and check every value before anything is written."""
        tenant_id = request.tenant_id

        if actions["company"] is not None and validated.company_name:
            name = validated.company_name
            resolution = await self._tools.resolve_company(tenant_id, name)
            if resolution.status == "ambiguous":
                raise OperationError(
                    "company_ambiguous",
                    f"I found multiple companies matching {name}. Which one should I use? "
                    "Please give the company's exact name.",
                )
            if resolution.company is not None:
                plan.company = resolution.company
                if actions["company"] == "create":
                    label = _company_label(plan.company, name)
                    plan.reused.append(("company", label, plan.company.id))
                    plan.lines.append(
                        f"ℹ️ Company {label} already exists — used the existing record"
                    )
            elif actions["company"] == "create":
                plan.company_to_create = {"name": name, **validated.company_properties}
            else:
                raise OperationError(
                    "company_not_found",
                    f"I couldn't find a company named {name} in HubSpot. "
                    "If it's a new company, ask me to create it.",
                )

        if actions["contact"] == "create":
            existing = None
            if validated.properties.get("email"):
                found = await self._tools.resolve_contact(
                    tenant_id, email=validated.properties["email"]
                )
                existing = found.contact
            if existing is not None:
                plan.contact = existing
                label = _contact_label(existing, validated.properties) or existing.id
                plan.reused.append(("contact", label, existing.id))
                plan.lines.append(f"ℹ️ Contact {label} already exists — used the existing record")
            else:
                plan.contact_to_create = dict(validated.properties)
        elif actions["contact"] == "reference":
            plan.contact = await self._resolve_contact(request, validated)

        if actions["deal"] == "create":
            deal_name = validated.deal_name
            if deal_name is None:
                company_name = _company_label(plan.company, validated.company_name or "")
                deal_name = f"{company_name} Deal"
                plan.deal_name_derived = True
            existing_deal = await self._tools.resolve_deal(tenant_id, deal_name)
            if existing_deal.status != "not_found":
                deal_id = f" (ID `{existing_deal.deal.id}`)" if existing_deal.deal else ""
                raise OperationError(
                    "duplicate",
                    f"A deal named {deal_name} already exists in HubSpot{deal_id}, so I didn't "
                    "create another one.",
                )
            pipelines = await self._tools.deal_pipelines(tenant_id)
            pipeline, stage = resolve_pipeline_stage(
                pipelines,
                pipeline_label=validated.deal_pipeline,
                stage_label=validated.deal_stage,
            )
            if stage is None:
                raise OperationError(
                    "invalid_request",
                    f"The {pipeline.label} pipeline has no stages, so I can't place the deal.",
                )
            plan.deal_to_create = {
                "dealname": deal_name,
                "pipeline": pipeline.id,
                "dealstage": stage.id,
            }
            if validated.deal_amount is not None:
                plan.deal_to_create["amount"] = validated.deal_amount
            plan.deal_stage_label = stage.label
        elif actions["deal"] == "reference":
            plan.deal = await self._resolve_deal(request, validated.deal_name or "")

    async def _execute(
        self,
        request: AgentRequest,
        validated: ValidatedExtraction,
        links: list[str],
        plan: _Plan,
    ) -> None:
        tenant_id = request.tenant_id

        if plan.company_to_create is not None:
            plan.step = f"Creating company {plan.company_to_create['name']}"
            plan.company = await self._tools.create_company(tenant_id, plan.company_to_create)
            plan.written["company_id"] = plan.company.id
            plan.cards.append(_company_card_summary(plan.company))
            plan.lines.append(f"✅ Company created: {_company_label(plan.company, '')}")

        if plan.contact_to_create is not None:
            label = _contact_label(None, plan.contact_to_create)
            plan.step = f"Creating contact {label}"
            contact_properties: dict[str, str | None] = dict(plan.contact_to_create)
            plan.contact = await self._tools.create_contact(tenant_id, contact_properties)
            plan.written["contact_id"] = plan.contact.id
            plan.cards.append(
                _card(
                    "contact",
                    "Contact created",
                    label,
                    plan.contact_to_create.get("email"),
                    plan.contact.id,
                )
            )
            plan.lines.append(f"✅ Contact created: {label}")

        if plan.deal_to_create is not None:
            name = plan.deal_to_create["dealname"]
            plan.step = f"Creating deal {name}"
            plan.deal = await self._tools.create_deal(tenant_id, plan.deal_to_create)
            plan.written["deal_id"] = plan.deal.id
            amount = plan.deal_to_create.get("amount")
            details = " — ".join(
                part
                for part in (format_amount(amount) if amount else None, plan.deal_stage_label)
                if part
            )
            line = f"✅ Deal created: {name}" + (f" — {details}" if details else "")
            if plan.deal_name_derived:
                line += " (named automatically; no deal name was given)"
            plan.lines.append(line)
            deal_detail = " · ".join(
                part
                for part in (
                    f"Amount {format_amount(amount)}" if amount else None,
                    plan.deal_stage_label,
                )
                if part
            )
            plan.cards.append(_card("deal", "Deal created", name, deal_detail, plan.deal.id))

        records: dict[Entity, HubSpotCompany | HubSpotContact | HubSpotDeal | None] = {
            "company": plan.company,
            "contact": plan.contact,
            "deal": plan.deal,
        }
        for link in links:
            from_type, from_entity, to_type, to_entity = _LINKS[link]
            source, target = records[from_entity], records[to_entity]
            if source is None or target is None:
                continue
            source_label = _record_label(from_entity, source, validated)
            target_label = _record_label(to_entity, target, validated)
            plan.step = f"Associating {source_label} with {target_label}"
            await self._tools.associate(
                tenant_id,
                from_type=from_type,
                from_id=source.id,
                to_type=to_type,
                to_id=target.id,
            )
            plan.written[f"{link}_association"] = f"{source.id}->{target.id}"
            plan.lines.append(f"✅ {source_label} associated with {target_label}")
            plan.cards.append(
                {"kind": "link", "title": "Linked", "name": f"{source_label} ↔ {target_label}"}
            )

    # ------------------------------------------------------------ updates (confirmed)

    async def _propose_update(
        self,
        request: AgentRequest,
        extraction: CRMIntentExtraction,
        validated: ValidatedExtraction,
        actions: dict[Entity, str | None],
    ) -> AgentResponse:
        updates = [entity for entity, action in actions.items() if action == "update"]
        other_writes = [entity for entity, action in actions.items() if action == "create"]
        if len(updates) != 1 or other_writes or extraction.associations:
            raise OperationError(
                "needs_clarification",
                "Updates need your confirmation, so please send each update as its own "
                "request (for example: “Update the website of ABC Technologies to abc.com”).",
            )
        if updates[0] == "company":
            return await self._propose_company_update(request, validated)
        if updates[0] == "deal":
            return await self._propose_deal_update(request, validated)
        raise OperationError(
            "needs_clarification",
            "To update a contact, include the contact's HubSpot ID and the new values.",
        )

    async def _propose_company_update(
        self, request: AgentRequest, validated: ValidatedExtraction
    ) -> AgentResponse:
        if not validated.company_name:
            raise OperationError("missing_fields", "Which company should I update?")
        changes = dict(validated.company_properties)
        if not changes:
            raise OperationError(
                "missing_fields",
                f"What should I change on {validated.company_name}, and to what value? "
                "I can update a company's website, domain, phone number or city.",
            )
        try:
            company = await self._resolve_company(request, validated.company_name)
        except (IntegrationError, ValueError) as exc:
            raise OperationError(*_hubspot_failure(exc)) from exc
        label = _company_label(company, validated.company_name)
        summary = ", ".join(f"{key} → {value}" for key, value in changes.items())
        action_id = await self._safety.create_pending_action(
            tenant_id=request.tenant_id,
            actor_id=request.actor_id,
            action_type="update_company",
            resource_type="company",
            payload={
                "company_id": company.id,
                "company_name": label,
                "properties": changes,
                "summary": summary,
            },
        )
        return _pending_response(
            request,
            action_id,
            action_type="update_company",
            record=f"company {label}",
            text=(
                "I found a request to update this HubSpot company:\n"
                f"• Company: {label} (ID {company.id})\n"
                f"• Changes: {summary}\n\n"
                f"Action ID: `{action_id}`\n"
                f"Reply with `confirm {action_id}` to update this company."
            ),
        )

    async def _propose_deal_update(
        self, request: AgentRequest, validated: ValidatedExtraction
    ) -> AgentResponse:
        if not validated.deal_name:
            raise OperationError(
                "missing_fields", "Which deal should I update? Please include the deal's name."
            )
        if not (validated.deal_amount or validated.deal_stage or validated.deal_pipeline):
            raise OperationError(
                "missing_fields",
                f"What should I change on {validated.deal_name}? "
                "I can update a deal's amount, stage or pipeline.",
            )
        try:
            deal = await self._resolve_deal(request, validated.deal_name)
            changes: dict[str, str] = {}
            described: list[str] = []
            if validated.deal_amount is not None:
                changes["amount"] = validated.deal_amount
                described.append(f"amount → {format_amount(validated.deal_amount)}")
            if validated.deal_stage or validated.deal_pipeline:
                pipelines = await self._tools.deal_pipelines(request.tenant_id)
                pipeline, stage = resolve_pipeline_stage(
                    pipelines,
                    pipeline_label=validated.deal_pipeline,
                    stage_label=validated.deal_stage,
                    current_pipeline_id=deal.properties.get("pipeline"),
                )
                if stage is None:
                    raise OperationError(
                        "invalid_request", f"The {pipeline.label} pipeline has no stages."
                    )
                if pipeline.id != deal.properties.get("pipeline"):
                    changes["pipeline"] = pipeline.id
                    described.append(f"pipeline → {pipeline.label}")
                changes["dealstage"] = stage.id
                described.append(f"stage → {stage.label}")
        except (IntegrationError, ValueError) as exc:
            raise OperationError(*_hubspot_failure(exc)) from exc

        label = deal.properties.get("dealname") or validated.deal_name
        summary = ", ".join(described)
        action_id = await self._safety.create_pending_action(
            tenant_id=request.tenant_id,
            actor_id=request.actor_id,
            action_type="update_deal",
            resource_type="deal",
            payload={
                "deal_id": deal.id,
                "deal_name": label,
                "properties": changes,
                "summary": summary,
            },
        )
        return _pending_response(
            request,
            action_id,
            action_type="update_deal",
            record=f"deal {label}",
            text=(
                "I found a request to update this HubSpot deal:\n"
                f"• Deal: {label} (ID {deal.id})\n"
                f"• Changes: {summary}\n\n"
                f"Action ID: `{action_id}`\n"
                f"Reply with `confirm {action_id}` to update this deal."
            ),
        )

    async def execute_confirmed_update(
        self, request: AgentRequest, confirmed: ConfirmedAction
    ) -> AgentResponse:
        payload = confirmed.payload
        is_company = confirmed.action_type == "update_company"
        kind = "company" if is_company else "deal"
        record_id = str(payload[f"{kind}_id"])
        label = str(payload.get(f"{kind}_name") or record_id)
        properties = {str(key): str(value) for key, value in dict(payload["properties"]).items()}
        try:
            if is_company:
                await self._tools.update_company(request.tenant_id, record_id, properties)
            else:
                await self._tools.update_deal(request.tenant_id, record_id, properties)
        except (IntegrationError, ValueError) as exc:
            status, message = _hubspot_failure(exc)
            await self._safety.fail_action(
                action_id=confirmed.id,
                tenant_id=request.tenant_id,
                actor_id=request.actor_id,
                request_id=request.request_id,
                resource_type=kind,
                error_code=status,
            )
            return _response(request, status, f"I couldn't update {kind} {label}. {message}")

        await self._safety.complete_action(
            action_id=confirmed.id,
            tenant_id=request.tenant_id,
            actor_id=request.actor_id,
            request_id=request.request_id,
            resource_type=kind,
            resource_id=record_id,
            result={f"{kind}_id": record_id},
        )
        return AgentResponse(
            status="ok",
            text=(
                f"✅ {kind.capitalize()} {label} updated successfully in HubSpot.\n"
                f"• Changes: {payload.get('summary', ', '.join(properties))}"
            ),
            request_id=request.request_id,
            tools_used=[f"update_{kind}"],
            result={"kind": "crm_records", "title": f"{kind.capitalize()} Updated"},
        )

    # ------------------------------------------------------------------ read queries

    async def _answer_query(
        self, request: AgentRequest, query: CRMQueryName, validated: ValidatedExtraction
    ) -> AgentResponse:
        tenant_id = request.tenant_id
        try:
            if query in ("company_details", "company_contacts", "company_deals"):
                if not validated.company_name:
                    raise OperationError(
                        "missing_fields", "Which company? Please include the company's name."
                    )
                company = await self._resolve_company(request, validated.company_name)
                label = _company_label(company, validated.company_name)
                if query == "company_details":
                    record = await self._tools.get_company(tenant_id, company.id)
                    return _records_response(
                        request, "Company Details", _company_card(record)
                    )
                to_type: CRMObjectType = "contacts" if query == "company_contacts" else "deals"
                linked = await self._tools.associated_records(
                    tenant_id, from_type="companies", from_id=company.id, to_type=to_type
                )
                if to_type == "contacts":
                    lines = [_contact_line(record) for record in linked]
                    return _records_response(
                        request,
                        f"Contacts at {label}",
                        _list_text(lines, f"No contacts are associated with {label}."),
                    )
                stage_labels = await self._stage_labels(tenant_id) if linked else {}
                lines = [_deal_line(record, stage_labels) for record in linked]
                return _records_response(
                    request,
                    f"Deals for {label}",
                    _list_text(lines, f"No deals are associated with {label}."),
                )

            if query in ("contact_details", "contact_company"):
                contact = await self._resolve_contact(request, validated)
                if query == "contact_details":
                    return _records_response(request, "Contact Details", _contact_card(contact))
                linked = await self._tools.associated_records(
                    tenant_id, from_type="contacts", from_id=contact.id, to_type="companies"
                )
                label = _contact_label(contact, validated.properties)
                lines = [f"• {record.properties.get('name') or record.id}" for record in linked]
                return _records_response(
                    request,
                    f"Companies for {label}",
                    _list_text(lines, f"{label} isn't associated with any company."),
                )

            if not validated.deal_name:
                raise OperationError(
                    "missing_fields", "Which deal? Please include the deal's name."
                )
            deal = await self._resolve_deal(request, validated.deal_name)
            return _records_response(
                request,
                "Deal Details",
                _deal_card(deal, await self._stage_labels(tenant_id)),
            )
        except (IntegrationError, ValueError) as exc:
            return _response(request, *_hubspot_failure(exc))

    # ------------------------------------------------------------------- resolution

    async def _resolve_company(self, request: AgentRequest, name: str) -> HubSpotCompany:
        resolution = await self._tools.resolve_company(request.tenant_id, name)
        if resolution.status == "ambiguous":
            raise OperationError(
                "company_ambiguous",
                f"I found multiple companies matching {name}. Which one should I use? "
                "Please give the company's exact name.",
            )
        if resolution.company is None:
            raise OperationError(
                "company_not_found", f"I couldn't find a company named {name} in HubSpot."
            )
        return resolution.company

    async def _resolve_contact(
        self, request: AgentRequest, validated: ValidatedExtraction
    ) -> HubSpotContact:
        if validated.contact_id and not validated.properties:
            return HubSpotContact(id=validated.contact_id, properties={})
        label = _contact_label(None, validated.properties)
        if not label:
            raise OperationError(
                "missing_fields", "Which contact? Please include their name or email address."
            )
        resolution = await self._tools.resolve_contact(
            request.tenant_id,
            email=validated.properties.get("email"),
            first_name=validated.properties.get("firstname"),
            last_name=validated.properties.get("lastname"),
        )
        if resolution.status == "ambiguous":
            raise OperationError(
                "contact_ambiguous",
                f"I found multiple contacts named {label}. Please include their email "
                "address so I use the right one.",
            )
        if resolution.contact is None:
            raise OperationError(
                "contact_not_found",
                f"I couldn't find a contact named {label} in HubSpot. If this is a new "
                "contact, ask me to create them.",
            )
        return resolution.contact

    async def _resolve_deal(self, request: AgentRequest, name: str) -> HubSpotDeal:
        if not name:
            raise OperationError("missing_fields", "Which deal? Please include the deal's name.")
        resolution = await self._tools.resolve_deal(request.tenant_id, name)
        if resolution.status == "ambiguous":
            raise OperationError(
                "deal_ambiguous",
                f"I found multiple deals named {name}. Please tell me which one to use.",
            )
        if resolution.deal is None:
            raise OperationError("deal_not_found", f"I couldn't find a deal named {name}.")
        return resolution.deal

    async def _stage_labels(self, tenant_id: str) -> dict[str, str]:
        try:
            pipelines = await self._tools.deal_pipelines(tenant_id)
        except IntegrationError:
            return {}
        return {stage.id: stage.label for pipeline in pipelines for stage in pipeline.stages}

    async def _fail(self, request: AgentRequest, action_id: str, error_code: str) -> None:
        await self._safety.fail_action(
            action_id=action_id,
            tenant_id=request.tenant_id,
            actor_id=request.actor_id,
            request_id=request.request_id,
            resource_type="crm",
            error_code=error_code,
        )


# ----------------------------------------------------------------------- pure helpers


def _entity_actions(
    extraction: CRMIntentExtraction, validated: ValidatedExtraction
) -> dict[Entity, str | None]:
    actions: dict[Entity, str | None] = {
        "contact": extraction.contact_action,
        "company": extraction.company_action,
        "deal": extraction.deal_action,
    }
    implied = _IMPLIED_ACTION.get(extraction.intent)
    if implied is not None and actions[implied[0]] is None:
        actions[implied[0]] = implied[1]
    mentioned: dict[Entity, bool] = {
        "contact": bool(validated.properties or validated.contact_id),
        "company": validated.company_name is not None,
        "deal": validated.deal_name is not None,
    }
    for entity, present in mentioned.items():
        if present and actions[entity] is None:
            actions[entity] = "reference"
    return actions


def _requested_links(
    extraction: CRMIntentExtraction, actions: dict[Entity, str | None]
) -> list[str]:
    requested: set[str] = set(extraction.associations)
    # A new contact "under" a company and a new deal "for" a company are linked to it.
    if actions["contact"] == "create" and actions["company"] is not None:
        requested.add("contact_company")
    if actions["deal"] == "create" and actions["company"] is not None:
        requested.add("deal_company")
    if extraction.intent == "associate_records" and not requested:
        present = {entity for entity, action in actions.items() if action is not None}
        for name, (_, from_entity, _, to_entity) in _LINKS.items():
            if present == {from_entity, to_entity}:
                requested.add(name)
    return [name for name in _LINKS if name in requested]


def _check_plan_inputs(
    validated: ValidatedExtraction,
    actions: dict[Entity, str | None],
    links: list[str],
) -> None:
    creates = [entity for entity, action in actions.items() if action == "create"]
    if not creates and not links:
        raise OperationError(
            "needs_clarification",
            "I'm not sure what you'd like me to do with those records. Try something like "
            "“Associate Victor Hall with ABC Technologies”.",
        )
    if actions["company"] is not None and not validated.company_name:
        raise OperationError("missing_fields", "Which company? Please include its name.")
    if actions["contact"] == "create" and not (
        {"firstname", "lastname", "email"} & validated.properties.keys()
    ):
        raise OperationError(
            "missing_fields",
            "I need at least the contact's name or email address to create a contact.",
        )
    if actions["contact"] == "reference" and not (validated.properties or validated.contact_id):
        raise OperationError(
            "missing_fields", "Which contact? Please include their name or email address."
        )
    if actions["deal"] == "reference" and not validated.deal_name:
        raise OperationError("missing_fields", "Which deal? Please include the deal's name.")
    if actions["deal"] == "create" and not validated.deal_name and not validated.company_name:
        raise OperationError("missing_fields", "What should the new deal be called?")
    for link in links:
        _, from_entity, _, to_entity = _LINKS[link]
        for entity in (from_entity, to_entity):
            if actions[entity] is None:
                raise OperationError(
                    "missing_fields", f"Which {entity} should I link? Please include it."
                )
    if actions["company"] == "reference" and validated.company_properties:
        raise OperationError(
            "needs_clarification",
            "Changing a company's details needs your confirmation, so please send that "
            "update as its own request.",
        )
    if actions["deal"] == "reference" and (
        validated.deal_amount or validated.deal_stage or validated.deal_pipeline
    ):
        raise OperationError(
            "needs_clarification",
            "Changing a deal's amount or stage needs your confirmation, so please send that "
            "update as its own request.",
        )


def resolve_pipeline_stage(
    pipelines: list[HubSpotPipeline],
    *,
    pipeline_label: str | None,
    stage_label: str | None,
    current_pipeline_id: str | None = None,
) -> tuple[HubSpotPipeline, HubSpotPipelineStage | None]:
    """Map user wording to real pipeline/stage IDs; never guess an unknown stage."""
    if not pipelines:
        raise OperationError("unavailable", "HubSpot returned no deal pipelines.")

    if pipeline_label:
        pipeline = next(
            (
                item
                for item in pipelines
                if _normal(item.label) == _normal(pipeline_label) or item.id == pipeline_label
            ),
            None,
        )
        if pipeline is None:
            available = ", ".join(item.label for item in pipelines)
            raise OperationError(
                "invalid_request",
                f"I couldn't find a deal pipeline named “{pipeline_label}”. "
                f"Available pipelines: {available}.",
            )
    else:
        pipeline = (
            next((item for item in pipelines if item.id == current_pipeline_id), None)
            or next((item for item in pipelines if item.id == "default"), None)
            or min(pipelines, key=lambda item: item.display_order)
        )

    stages = sorted(pipeline.stages, key=lambda item: item.display_order)
    if not stage_label:
        return pipeline, stages[0] if stages else None
    stage = next(
        (
            item
            for item in stages
            if _normal(item.label) == _normal(stage_label) or item.id == stage_label
        ),
        None,
    )
    if stage is None:
        available = ", ".join(item.label for item in stages)
        raise OperationError(
            "invalid_request",
            f"“{stage_label}” isn't a stage in the {pipeline.label} pipeline. "
            f"Available stages: {available}.",
        )
    return pipeline, stage


def format_amount(amount: str) -> str:
    value = Decimal(amount)
    return f"{value:,.2f}" if value != value.to_integral_value() else f"{int(value):,}"


def _hubspot_failure(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, IntegrationPermissionError):
        scope = f" ({exc.scope})" if exc.scope else ""
        return (
            "insufficient_scope",
            f"HubSpot hasn't granted this app permission for that{scope}. A HubSpot admin "
            "needs to reconnect HubSpot so the app receives the updated permissions.",
        )
    if isinstance(exc, IntegrationAuthenticationError):
        return "hubspot_auth", "HubSpot rejected the app's credentials. Please reconnect HubSpot."
    if isinstance(exc, IntegrationNotFoundError):
        return "not_found", "HubSpot couldn't find that record."
    if isinstance(exc, HubSpotDuplicateContactError):
        return (
            "duplicate",
            f"A contact with the email {exc.email} already exists "
            f"(contact ID `{exc.contact_id}`).",
        )
    if isinstance(exc, IntegrationError):
        return "unavailable", "HubSpot returned an error. Please try again in a moment."
    return "hubspot_not_authorized", "HubSpot is not connected for this workspace."


def _normal(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def _company_label(company: HubSpotCompany | None, fallback: str) -> str:
    if company is not None and company.properties.get("name"):
        return str(company.properties["name"])
    return fallback


def _contact_label(
    contact: HubSpotContact | HubSpotRecord | None, fallback: dict[str, str]
) -> str:
    """Best human label: the record's name, else the user's name, else an email."""
    sources: list[dict[str, str | None]] = [dict(contact.properties)] if contact else []
    sources.append(dict(fallback))
    for source in sources:
        name = " ".join(str(source[key]) for key in ("firstname", "lastname") if source.get(key))
        if name:
            return name
    return next((str(source["email"]) for source in sources if source.get("email")), "")


def _record_label(
    entity: Entity,
    record: HubSpotCompany | HubSpotContact | HubSpotDeal,
    validated: ValidatedExtraction,
) -> str:
    if entity == "company":
        return str(record.properties.get("name") or validated.company_name or record.id)
    if entity == "contact":
        if not isinstance(record, HubSpotContact):
            return f"Contact {record.id}"
        label = _contact_label(record, validated.properties) or record.id
        return f"Contact {label}"
    return f"Deal {record.properties.get('dealname') or validated.deal_name or record.id}"


def _company_card(company: HubSpotCompany) -> str:
    properties = company.properties
    rows = [
        ("Domain", properties.get("domain")),
        ("Website", properties.get("website")),
        ("Phone", properties.get("phone")),
        ("City", properties.get("city")),
        ("Industry", properties.get("industry")),
    ]
    details = [f"• {label}: {value}" for label, value in rows if value]
    return "\n".join(
        [f"*{properties.get('name') or company.id}*", *details, f"• HubSpot ID: `{company.id}`"]
    )


def _contact_card(contact: HubSpotContact) -> str:
    properties = contact.properties
    rows = [
        ("Email", properties.get("email")),
        ("Phone", properties.get("phone")),
        ("Job title", properties.get("jobtitle")),
    ]
    details = [f"• {label}: {value}" for label, value in rows if value]
    name = _contact_label(contact, {}) or contact.id
    return "\n".join([f"*{name}*", *details, f"• HubSpot ID: `{contact.id}`"])


def _deal_card(deal: HubSpotDeal, stage_labels: dict[str, str]) -> str:
    properties = deal.properties
    amount = properties.get("amount")
    stage = properties.get("dealstage")
    rows = [
        ("Amount", format_amount(amount) if amount else None),
        ("Stage", stage_labels.get(stage, stage) if stage else None),
        ("Close date", (properties.get("closedate") or "")[:10] or None),
    ]
    details = [f"• {label}: {value}" for label, value in rows if value]
    return "\n".join(
        [f"*{properties.get('dealname') or deal.id}*", *details, f"• HubSpot ID: `{deal.id}`"]
    )


def _contact_line(record: HubSpotRecord) -> str:
    name = _contact_label(record, {}) or f"Contact {record.id}"
    email = record.properties.get("email")
    return f"• {name}" + (f" — {email}" if email else "")


def _deal_line(record: HubSpotRecord, stage_labels: dict[str, str]) -> str:
    properties = record.properties
    amount = properties.get("amount")
    stage = properties.get("dealstage")
    parts = [
        format_amount(amount) if amount else None,
        stage_labels.get(stage, stage) if stage else None,
    ]
    details = " — ".join(part for part in parts if part)
    return f"• {properties.get('dealname') or record.id}" + (f" — {details}" if details else "")


def _list_text(lines: list[str], empty: str) -> str:
    if not lines:
        return empty
    shown = lines[:_LIST_LIMIT]
    if len(lines) > _LIST_LIMIT:
        shown.append(f"…and {len(lines) - _LIST_LIMIT} more")
    return "\n".join(shown)


def _records_response(request: AgentRequest, title: str, text: str) -> AgentResponse:
    return AgentResponse(
        status="ok",
        text=text,
        request_id=request.request_id,
        tools_used=[],
        result={"kind": "crm_records", "title": title},
    )


def _pending_response(
    request: AgentRequest, action_id: str, *, action_type: str, record: str, text: str
) -> AgentResponse:
    return AgentResponse(
        status="pending_confirmation",
        text=text,
        request_id=request.request_id,
        tools_used=[],
        result={
            "kind": "pending_confirmation",
            "action_id": action_id,
            "action_type": action_type,
            "record_label": record,
        },
    )


def _card(
    kind: str, title: str, name: str, detail: str | None, record_id: str
) -> dict[str, str]:
    card = {"kind": kind, "title": title, "name": name, "hubspot_id": record_id}
    if detail:
        card["detail"] = detail
    return card


def _company_card_summary(company: HubSpotCompany) -> dict[str, str]:
    properties = company.properties
    detail = properties.get("city") or properties.get("domain") or properties.get("website")
    return _card(
        "company", "Company created", _company_label(company, company.id), detail, company.id
    )


def _single_create_summary(plan: _Plan) -> dict[str, str]:
    """A short headline for rich UIs when the request created exactly one record."""
    if len(plan.lines) != 1 or len(plan.written) != 1:
        return {}
    if plan.company is not None and "company_id" in plan.written:
        entity, label = "Company", _company_label(plan.company, plan.company.id)
    elif plan.deal is not None and "deal_id" in plan.written:
        entity, label = "Deal", str(plan.deal.properties.get("dealname") or plan.deal.id)
    elif plan.contact is not None and "contact_id" in plan.written:
        entity = "Contact"
        label = _contact_label(plan.contact, plan.contact_to_create or {}) or plan.contact.id
    else:
        return {}
    return {"title": f"{entity} created", "message": f"{label} was successfully added to HubSpot."}


def _existing_records_response(
    request: AgentRequest, reused: list[tuple[str, str, str]]
) -> AgentResponse:
    sentences = [
        f"{label} already exists in HubSpot, so I used the existing {entity} record."
        for entity, label, _ in reused
    ]
    if len(reused) == 1:
        title = f"{reused[0][0].capitalize()} already exists"
        text = f"{sentences[0]} No duplicate was created."
    else:
        title = "Records already exist"
        bullets = [f"• {sentence}" for sentence in sentences]
        text = "\n".join([*bullets, "No duplicates were created."])
    return AgentResponse(
        status="already_exists",
        text=text,
        request_id=request.request_id,
        tools_used=[],
        result={"kind": "existing_record", "title": title},
    )


def _response(request: AgentRequest, status: str, text: str) -> AgentResponse:
    return AgentResponse(status=status, text=text, request_id=request.request_id)
