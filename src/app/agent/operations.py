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
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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
            return await _existing_records_response(request, plan.reused, self._tools)

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
            company_card = _company_card_summary(plan.company)
            company_card["hubspot_url"] = (
                await self._tools.hubspot_record_urls(tenant_id, [("companies", plan.company.id)])
            )[0]
            plan.cards.append(company_card)
            plan.lines.append(f"✅ Company created: {_company_label(plan.company, '')}")

        if plan.contact_to_create is not None:
            label = _contact_label(None, plan.contact_to_create)
            plan.step = f"Creating contact {label}"
            contact_properties: dict[str, str | None] = dict(plan.contact_to_create)
            plan.contact = await self._tools.create_contact(tenant_id, contact_properties)
            plan.written["contact_id"] = plan.contact.id
            contact_card = _card(
                "contact",
                "Contact created",
                label,
                plan.contact_to_create.get("email"),
                plan.contact.id,
            )
            contact_card["hubspot_url"] = (
                await self._tools.hubspot_record_urls(tenant_id, [("contacts", plan.contact.id)])
            )[0]
            plan.cards.append(contact_card)
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
            deal_card = _card("deal", "Deal created", name, deal_detail, plan.deal.id)
            deal_card["hubspot_url"] = (
                await self._tools.hubspot_record_urls(
                    tenant_id, [("deals", plan.deal.id)]
                )
            )[0]
            plan.cards.append(deal_card)

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
            "needs_clarification", "Contact update is handled by the contact agent."
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
        label = _company_label(company, validated.company_name or company.id)
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

        label = deal.properties.get("dealname") or validated.deal_name or deal.id
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
        properties = {
            str(key): str(value)
            for key, value in dict(payload.get("properties", {})).items()
        }
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
            result={
                "kind": "crm_records",
                "title": f"{kind.capitalize()} Updated",
                "hubspot_url": (
                    await self._tools.hubspot_record_urls(
                        request.tenant_id,
                        [("companies" if is_company else "deals", record_id)],
                    )
                )[0],
            },
        )

    # ------------------------------------------------------------------ read queries

    async def _answer_query(
        self, request: AgentRequest, query: CRMQueryName, validated: ValidatedExtraction
    ) -> AgentResponse:
        tenant_id = request.tenant_id
        try:
            if query == "company_list":
                companies = await self._tools.list_all_companies(tenant_id)
                deal_names = {
                    company.id: ", ".join(
                        str(deal.properties.get("dealname") or deal.id)
                        for deal in await self._tools.associated_records(
                            tenant_id,
                            from_type="companies",
                            from_id=company.id,
                            to_type="deals",
                        )
                    )
                    for company in companies
                }
                rows = [
                    {
                        "name": item.properties.get("name") or item.id,
                        "owner": item.properties.get("hubspot_owner_id") or "",
                        "phone": item.properties.get("phone") or "",
                        "city": item.properties.get("city") or "",
                        "industry": item.properties.get("industry") or "",
                        "employees": item.properties.get("numberofemployees") or "",
                        "lifecycle": item.properties.get("lifecyclestage") or "",
                        "lead_status": item.properties.get("hs_lead_status") or "",
                        "last_contacted": _date_value(
                            item.properties.get("notes_last_contacted")
                        ),
                        "associated_deals": deal_names[item.id],
                    }
                    for item in companies
                ]
                await _add_hubspot_urls(
                    self._tools, tenant_id, rows, [("companies", item.id) for item in companies]
                )
                return _table_response(
                    request,
                    "Companies",
                    f"Found {len(companies)} companies.",
                    [
                        ("name", "Name"),
                        ("owner", "Owner"),
                        ("phone", "Phone"),
                        ("city", "City"),
                        ("industry", "Industry"),
                        ("employees", "Number of Employees"),
                        ("lifecycle", "Lifecycle Stage"),
                        ("lead_status", "Lead Status"),
                        ("last_contacted", "Last Contacted"),
                        ("associated_deals", "Associated Deals"),
                    ],
                    rows,
                    tools=["list_all_companies", "associated_records"],
                    requested_fields=validated.requested_fields,
                )

            if query in ("company_search", "company_details"):
                if not validated.company_name:
                    raise OperationError(
                        "missing_fields", "Which company? Please include the company's name."
                    )
                if query == "company_search":
                    companies = await self._tools.search_companies(
                        tenant_id, validated.company_name or ""
                    )
                    company_search_rows: list[dict[str, str]] = []
                    for item in companies:
                        row = _company_row(item)
                        deals = await self._tools.associated_records(
                            tenant_id,
                            from_type="companies",
                            from_id=item.id,
                            to_type="deals",
                        )
                        row["associated_deals"] = ", ".join(
                            str(deal.properties.get("dealname") or deal.id) for deal in deals
                        )
                        company_search_rows.append(row)
                    await _add_hubspot_urls(
                        self._tools,
                        tenant_id,
                        company_search_rows,
                        [("companies", item.id) for item in companies],
                    )
                    return _table_response(
                        request,
                        "Company Search",
                        f"Found {len(companies)} matching companies.",
                        [
                            ("name", "Name"),
                            ("owner", "Owner"),
                            ("phone", "Phone"),
                            ("city", "City"),
                            ("industry", "Industry"),
                            ("employees", "Number of Employees"),
                            ("lifecycle", "Lifecycle Stage"),
                            ("lead_status", "Lead Status"),
                            ("last_contacted", "Last Contacted"),
                            ("associated_deals", "Associated Deals"),
                        ],
                        company_search_rows,
                        tools=["search_companies", "associated_records"],
                        requested_fields=validated.requested_fields,
                    )

                company = await self._resolve_company(request, validated.company_name)
                record = company
                associated = await self._tools.associated_records(
                    tenant_id, from_type="companies", from_id=company.id, to_type="deals"
                )
                row = _company_row(record)
                row["associated_deals"] = ", ".join(
                    str(deal.properties.get("dealname") or deal.id) for deal in associated
                )
                await _add_hubspot_urls(
                    self._tools, tenant_id, [row], [("companies", company.id)]
                )
                return _table_response(
                    request,
                    "Company Details",
                    "Company details from HubSpot.",
                    [
                        ("name", "Name"),
                        ("owner", "Owner"),
                        ("phone", "Phone"),
                        ("city", "City"),
                        ("industry", "Industry"),
                        ("employees", "Number of Employees"),
                        ("lifecycle", "Lifecycle Stage"),
                        ("lead_status", "Lead Status"),
                        ("last_contacted", "Last Contacted"),
                        ("associated_deals", "Associated Deals"),
                    ],
                    [row],
                    tools=["resolve_company", "get_company", "associated_records"],
                    requested_fields=validated.requested_fields,
                )

            if query in ("contact_list", "contact_search"):
                search_text = _contact_search_text(validated)
                if query == "contact_search" and not search_text:
                    raise OperationError(
                        "missing_fields", "Which contact? Please include a name or email."
                    )
                contacts = (
                    await self._tools.list_all_contacts(tenant_id)
                    if query == "contact_list"
                    else await self._tools.search_contacts(tenant_id, search_text)
                )
                companies_by_contact: dict[str, HubSpotRecord] = {}
                for contact in contacts:
                    associated_companies = await self._tools.associated_records(
                        tenant_id,
                        from_type="contacts",
                        from_id=contact.id,
                        to_type="companies",
                    )
                    if associated_companies:
                        companies_by_contact[contact.id] = associated_companies[0]
                    else:
                        company_id = contact.properties.get("associatedcompanyid")
                        if not company_id:
                            continue
                        company = await self._tools.get_company(tenant_id, company_id)
                        companies_by_contact[contact.id] = HubSpotRecord(
                            id=company.id, properties=company.properties
                        )
                deals_by_contact: dict[str, str] = {}
                for contact in contacts:
                    deals = await self._tools.associated_records(
                        tenant_id,
                        from_type="contacts",
                        from_id=contact.id,
                        to_type="deals",
                    )
                    deals_by_contact[contact.id] = ", ".join(
                        str(deal.properties.get("dealname") or deal.id) for deal in deals
                    )
                summary = (
                    f"Found {len(contacts)} contacts."
                    if query == "contact_list"
                    else f"Found {len(contacts)} matching contacts."
                )
                rows = [
                    _contact_row(
                        item,
                        companies_by_contact.get(item.id),
                        deals_by_contact[item.id],
                    )
                    for item in contacts
                ]
                await _add_hubspot_urls(
                    self._tools, tenant_id, rows, [("contacts", item.id) for item in contacts]
                )
                return _table_response(
                    request,
                    "Contacts" if query == "contact_list" else "Contact Search",
                    summary,
                    _contact_columns(),
                    rows,
                    tools=[
                        "list_all_contacts" if query == "contact_list" else "search_contacts",
                        "associated_records",
                    ],
                    requested_fields=validated.requested_fields,
                )

            if query in {
                "open_deals",
                "closed_deals",
                "closed_won_deals",
                "closed_lost_deals",
                "best_chance_deals",
                "all_deals",
            }:
                return await self._answer_deal_list(
                    request, query, validated.requested_fields
                )

            if query in ("company_contacts", "company_deals"):
                if not validated.company_name:
                    raise OperationError(
                        "missing_fields", "Which company? Please include the company's name."
                    )
                company = await self._resolve_company(request, validated.company_name)
                label = _company_label(company, validated.company_name)
                to_type: CRMObjectType = "contacts" if query == "company_contacts" else "deals"
                company_records = await self._tools.associated_records(
                    tenant_id, from_type="companies", from_id=company.id, to_type=to_type
                )
                if to_type == "contacts":
                    rows = [
                        {**_contact_row(record, None), "company": label}
                        for record in company_records
                    ]
                    await _add_hubspot_urls(
                        self._tools,
                        tenant_id,
                        rows,
                        [("contacts", record.id) for record in company_records],
                    )
                    return _table_response(
                        request,
                        f"Contacts at {label}",
                        f"Found {len(company_records)} associated contacts.",
                        _contact_columns(),
                        rows,
                        tools=["resolve_company", "associated_records"],
                        requested_fields=validated.requested_fields,
                    )
                stage_labels = (
                    await self._stage_labels(tenant_id) if company_records else {}
                )
                deal_rows = [
                    {**_deal_row(record, stage_labels), "company": label}
                    for record in company_records
                ]
                await _add_hubspot_urls(
                    self._tools,
                    tenant_id,
                    deal_rows,
                    [("deals", record.id) for record in company_records],
                )
                return _table_response(
                    request,
                    f"Deals for {label}",
                    f"Found {len(company_records)} associated deals.",
                    _deal_columns(),
                    deal_rows,
                    tools=["resolve_company", "associated_records"],
                    requested_fields=validated.requested_fields,
                )

            if query in ("contact_details", "contact_company", "contact_deals"):
                contact = await self._resolve_contact(request, validated)
                if query == "contact_details":
                    contact = await self._tools.get_contact(tenant_id, contact.id)
                    associated_companies = await self._tools.associated_records(
                        tenant_id, from_type="contacts", from_id=contact.id, to_type="companies"
                    )
                    associated_company = associated_companies[0] if associated_companies else None
                    deals = await self._tools.associated_records(
                        tenant_id, from_type="contacts", from_id=contact.id, to_type="deals"
                    )
                    row = _contact_row(contact, None)
                    row["company"] = (
                        str(associated_company.properties.get("name") or "")
                        if associated_company
                        else ""
                    )
                    row["associated_deals"] = ", ".join(
                        str(deal.properties.get("dealname") or deal.id) for deal in deals
                    )
                    await _add_hubspot_urls(
                        self._tools, tenant_id, [row], [("contacts", contact.id)]
                    )
                    return _table_response(
                        request,
                        "Contact Details",
                        "Contact details from HubSpot.",
                        _contact_columns(),
                        [row],
                        tools=["resolve_contact", "get_contact", "associated_records"],
                        requested_fields=validated.requested_fields,
                    )
                contact_records = await self._tools.associated_records(
                    tenant_id,
                    from_type="contacts",
                    from_id=contact.id,
                    to_type="deals" if query == "contact_deals" else "companies",
                )
                label = _contact_label(contact, validated.properties)
                if query == "contact_deals":
                    stage_labels = (
                        await self._stage_labels(tenant_id) if contact_records else {}
                    )
                    contact_deal_rows: list[dict[str, str]] = []
                    for associated_deal in contact_records:
                        associated_deal_companies = await self._tools.associated_records(
                            tenant_id,
                            from_type="deals",
                            from_id=associated_deal.id,
                            to_type="companies",
                        )
                        contact_deal_rows.append(
                            {
                                **_deal_row(associated_deal, stage_labels),
                                "company": (
                                    str(
                                        associated_deal_companies[0].properties.get("name")
                                        or associated_deal_companies[0].id
                                    )
                                    if associated_deal_companies
                                    else ""
                                ),
                            }
                        )
                    await _add_hubspot_urls(
                        self._tools,
                        tenant_id,
                        contact_deal_rows,
                        [("deals", record.id) for record in contact_records],
                    )
                    return _table_response(
                        request,
                        f"Deals for {label}",
                        f"Found {len(contact_records)} associated deals.",
                        _deal_columns(),
                        contact_deal_rows,
                        tools=["resolve_contact", "associated_records", "deal_pipelines"],
                        requested_fields=validated.requested_fields,
                    )
                rows = [
                    {"name": record.properties.get("name") or record.id}
                    for record in contact_records
                ]
                await _add_hubspot_urls(
                    self._tools,
                    tenant_id,
                    rows,
                    [("companies", record.id) for record in contact_records],
                )
                return _table_response(
                    request,
                    f"Companies for {label}",
                    f"Found {len(contact_records)} associated companies.",
                    [("name", "Name")],
                    rows,
                    tools=["resolve_contact", "associated_records"],
                    requested_fields=validated.requested_fields,
                )

            if not validated.deal_name:
                raise OperationError(
                    "missing_fields", "Which deal? Please include its name."
                )
            deal = await self._resolve_deal(request, validated.deal_name)
            associated_companies = await self._tools.associated_records(
                tenant_id, from_type="deals", from_id=deal.id, to_type="companies"
            )
            row = _deal_row(deal, await self._stage_labels(tenant_id))
            if associated_companies:
                row["company"] = str(
                    associated_companies[0].properties.get("name")
                    or associated_companies[0].id
                )
            await _add_hubspot_urls(self._tools, tenant_id, [row], [("deals", deal.id)])
            return _table_response(
                request,
                "Deal Details",
                "Deal details from HubSpot.",
                _deal_columns(),
                [row],
                tools=["resolve_deal", "get_deal", "associated_records"],
                requested_fields=validated.requested_fields,
            )
        except (IntegrationError, ValueError) as exc:
            return _response(request, *_hubspot_failure(exc))

    async def _answer_deal_list(
        self,
        request: AgentRequest,
        query: CRMQueryName,
        requested_fields: list[str] | None = None,
    ) -> AgentResponse:
        pipelines = await self._tools.deal_pipelines(request.tenant_id)
        stages = {
            stage.id: stage
            for pipeline in pipelines
            for stage in pipeline.stages
        }
        all_deals = await self._tools.list_all_deals(request.tenant_id)

        def is_closed(deal: HubSpotDeal) -> bool | None:
            stage = stages.get(deal.properties.get("dealstage") or "")
            if stage is None:
                return None
            configured = stage.metadata.get("isClosed", "").casefold()
            return configured == "true" if configured in {"true", "false"} else None

        def is_won(deal: HubSpotDeal) -> bool:
            stage = stages.get(deal.properties.get("dealstage") or "")
            if stage is None:
                return False
            configured = stage.metadata.get("isClosedWon")
            if configured is not None:
                return configured.casefold() == "true"
            try:
                return Decimal(stage.metadata["probability"]) == 1
            except (KeyError, InvalidOperation):
                return False

        def is_lost(deal: HubSpotDeal) -> bool:
            stage = stages.get(deal.properties.get("dealstage") or "")
            if stage is None:
                return False
            configured = stage.metadata.get("isClosedWon")
            if configured is not None:
                return configured.casefold() == "false"
            try:
                return Decimal(stage.metadata["probability"]) == 0
            except (KeyError, InvalidOperation):
                return False

        open_deals = [deal for deal in all_deals if is_closed(deal) is False]
        if query == "all_deals":
            deals = all_deals
        elif query in ("open_deals", "best_chance_deals"):
            deals = open_deals
        elif query == "closed_deals":
            deals = [deal for deal in all_deals if is_closed(deal) is True]
        elif query == "closed_won_deals":
            deals = [deal for deal in all_deals if is_closed(deal) is True and is_won(deal)]
        else:
            deals = [deal for deal in all_deals if is_closed(deal) is True and is_lost(deal)]

        probabilities = {deal.id: _deal_probability(deal) for deal in deals}
        if query == "best_chance_deals":
            deals.sort(
                key=lambda deal: (
                    probabilities[deal.id] is not None,
                    probabilities[deal.id] if probabilities[deal.id] is not None else Decimal(-1),
                ),
                reverse=True,
            )

        rows: list[dict[str, str]] = []
        for deal in deals:
            properties = deal.properties
            amount = properties.get("amount")
            associated = await self._tools.associated_records(
                request.tenant_id,
                from_type="deals",
                from_id=deal.id,
                to_type="companies",
            )
            rows.append(
                {
                    "name": properties.get("dealname") or deal.id,
                    "company": (
                        str(associated[0].properties.get("name") or "") if associated else ""
                    ),
                    "owner": properties.get("hubspot_owner_id") or "",
                    "stage": _stage_label(properties.get("dealstage"), stages),
                    "amount": format_amount(amount) if amount else "",
                    "probability": _format_probability(probabilities[deal.id]),
                    "close_date": (properties.get("closedate") or "")[:10],
                }
            )

        await _add_hubspot_urls(
            self._tools, request.tenant_id, rows, [("deals", deal.id) for deal in deals]
        )
        if query == "best_chance_deals":
            ranked = [
                (
                    f"{deal.properties.get('dealname') or deal.id} "
                    f"({_format_probability(probabilities[deal.id])})"
                )
                for deal in deals
                if probabilities[deal.id] is not None
            ]
            summary = (
                "Highest-probability open deals based on HubSpot's recorded probability: "
                + ", ".join(ranked[:3])
                + "."
                if ranked
                else "No open deals have a recorded HubSpot probability to rank."
            )
            title = "Open Deals by Probability"
        else:
            status_label = {
                "open_deals": "open",
                "closed_deals": "closed",
                "closed_won_deals": "closed-won",
                "closed_lost_deals": "closed-lost",
                "all_deals": "total",
            }[query]
            summary = f"Found {len(deals)} {status_label} deal{'s' if len(deals) != 1 else ''}."
            title = f"{status_label.capitalize()} Deals"
            if not deals:
                summary = f"No {status_label} deals were found."

        deal_columns = _columns_for_request(_deal_columns(), requested_fields or [])
        if requested_fields:
            for row in rows:
                for key, _ in deal_columns:
                    if key in requested_fields and not row.get(key):
                        row[key] = "—"

        return AgentResponse(
            status="ok",
            text=summary,
            request_id=request.request_id,
            tools_used=["deal_pipelines", "list_all_deals", "associated_records"],
            result={
                "kind": "crm_records",
                "title": title,
                "message": summary,
                "table": {
                    "columns": [
                        {"key": key, "label": label}
                        for key, label in deal_columns
                    ]
                    + [{"key": "view_url", "label": "View in HubSpot"}],
                    "rows": rows,
                },
            },
        )

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


def _date_value(value: str | None) -> str:
    if not value:
        return ""
    if value.isdigit() and len(value) == 13:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).date().isoformat()
    return value[:10]


def _company_row(company: HubSpotCompany) -> dict[str, str]:
    properties = company.properties
    return {
        "name": properties.get("name") or company.id,
        "owner": properties.get("hubspot_owner_id") or "",
        "phone": properties.get("phone") or "",
        "city": properties.get("city") or "",
        "industry": properties.get("industry") or "",
        "employees": properties.get("numberofemployees") or "",
        "lifecycle": properties.get("lifecyclestage") or "",
        "lead_status": properties.get("hs_lead_status") or "",
        "last_contacted": _date_value(properties.get("notes_last_contacted")),
    }


def _contact_columns() -> list[tuple[str, str]]:
    return [
        ("name", "Name"),
        ("owner", "Owner"),
        ("email", "Email"),
        ("phone", "Phone"),
        ("company", "Associated Company"),
        ("city", "City"),
        ("state", "State/Region"),
        ("industry", "Industry"),
        ("lifecycle", "Lifecycle Stage"),
        ("lead_status", "Lead Status"),
        ("last_contacted", "Last Contacted"),
        ("job_title", "Employment Role"),
        ("job_sub_role", "Job Sub Role"),
        ("seniority", "Job Seniority"),
        ("linkedin", "LinkedIn"),
        ("associated_deals", "Associated Deals"),
    ]


def _contact_row(
    contact: HubSpotContact | HubSpotRecord,
    company: HubSpotCompany | HubSpotRecord | None,
    associated_deals: str = "",
) -> dict[str, str]:
    properties = contact.properties
    return {
        "name": " ".join(
            part for part in (properties.get("firstname"), properties.get("lastname")) if part
        )
        or contact.id,
        "owner": properties.get("hubspot_owner_id") or "",
        "email": properties.get("email") or "",
        "phone": properties.get("phone") or "",
        "company": (
            company.properties.get("name") or company.id
            if company
            else properties.get("associatedcompanyid") or ""
        ),
        "city": properties.get("city") or "",
        "state": properties.get("state") or properties.get("country") or "",
        "industry": properties.get("industry") or "",
        "lifecycle": properties.get("lifecyclestage") or "",
        "lead_status": properties.get("hs_lead_status") or "",
        "last_contacted": _date_value(properties.get("notes_last_contacted")),
        "job_title": properties.get("jobtitle") or properties.get("hs_role") or "",
        "job_sub_role": properties.get("hs_sub_role") or "",
        "seniority": properties.get("hs_seniority") or "",
        "linkedin": properties.get("hs_linkedin_url") or "",
        "associated_deals": associated_deals,
    }


def _deal_columns() -> list[tuple[str, str]]:
    return [
        ("name", "Name"),
        ("company", "Account/Company"),
        ("owner", "Owner"),
        ("stage", "Stage Name"),
        ("amount", "Amount"),
        ("probability", "Probability"),
        ("close_date", "Close Date"),
    ]


def _deal_row(
    deal: HubSpotDeal | HubSpotRecord, stage_labels: dict[str, str]
) -> dict[str, str]:
    properties = deal.properties
    amount = properties.get("amount")
    stage = properties.get("dealstage")
    return {
        "name": properties.get("dealname") or deal.id,
        "company": properties.get("associatedcompanyid") or "",
        "owner": properties.get("hubspot_owner_id") or "",
        "stage": stage_labels.get(stage, stage) if stage else "",
        "amount": format_amount(amount) if amount else "",
        "probability": _format_probability(
            _deal_probability(HubSpotDeal(id=deal.id, properties=properties))
        ),
        "close_date": _date_value(properties.get("closedate")),
    }


def _contact_search_text(validated: ValidatedExtraction) -> str:
    return " ".join(
        value
        for value in (
            validated.properties.get("firstname"),
            validated.properties.get("lastname"),
            validated.properties.get("email"),
        )
        if value
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


def _stage_label(
    stage_id: str | None, stages: dict[str, HubSpotPipelineStage]
) -> str:
    if not stage_id:
        return ""
    return stages[stage_id].label if stage_id in stages else stage_id


def _deal_probability(deal: HubSpotDeal) -> Decimal | None:
    for property_name in ("hs_probability", "hs_deal_stage_probability"):
        raw = deal.properties.get(property_name)
        if raw is None:
            continue
        try:
            probability = Decimal(raw)
        except (InvalidOperation, ValueError):
            continue
        if probability.is_finite() and 0 <= probability <= 100:
            return probability * 100 if probability <= 1 else probability
    return None


def _format_probability(probability: Decimal | None) -> str:
    if probability is None:
        return ""
    displayed = probability.quantize(Decimal("0.1"))
    value = str(int(displayed)) if displayed == displayed.to_integral_value() else str(displayed)
    return f"{value}%"


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


def _table_response(
    request: AgentRequest,
    title: str,
    summary: str,
    columns: list[tuple[str, str]],
    rows: list[dict[str, str]],
    *,
    tools: list[str],
    requested_fields: list[str] | None = None,
) -> AgentResponse:
    requested_fields = requested_fields or []
    columns = _columns_for_request(columns, requested_fields)
    if requested_fields:
        for row in rows:
            for key in requested_fields:
                if any(column_key == key for column_key, _ in columns) and not row.get(key):
                    row[key] = "—"
    if not rows or all("view_url" in row for row in rows):
        columns = [*columns, ("view_url", "View in HubSpot")]
    return AgentResponse(
        status="ok",
        text=summary,
        request_id=request.request_id,
        tools_used=tools,
        result={
            "kind": "crm_records",
            "title": title,
            "message": summary,
            "table": {
                "columns": [{"key": key, "label": label} for key, label in columns],
                "rows": rows,
            },
        },
    )


def _columns_for_request(
    default_columns: list[tuple[str, str]], requested_fields: list[str]
) -> list[tuple[str, str]]:
    if not requested_fields:
        return default_columns
    labels = dict(default_columns)
    selected = [
        (key, labels[key])
        for key in dict.fromkeys(requested_fields)
        if key in labels
    ]
    return selected or default_columns


async def _add_hubspot_urls(
    tools: HubSpotToolRegistry,
    tenant_id: str,
    rows: list[dict[str, str]],
    records: list[tuple[CRMObjectType, str]],
) -> None:
    urls = await tools.hubspot_record_urls(tenant_id, records)
    for row, url in zip(rows, urls, strict=True):
        row["view_url"] = url


def _pending_response(
    request: AgentRequest,
    action_id: str,
    *,
    action_type: str,
    record: str,
    text: str,
) -> AgentResponse:
    result: dict[str, object] = {
        "kind": "pending_confirmation",
        "action_id": action_id,
        "action_type": action_type,
        "record_label": record,
    }
    return AgentResponse(
        status="pending_confirmation",
        text=text,
        request_id=request.request_id,
        tools_used=[],
        result=result,
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


async def _existing_records_response(
    request: AgentRequest,
    reused: list[tuple[str, str, str]],
    tools: HubSpotToolRegistry,
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
    object_types: dict[str, CRMObjectType] = {
        "company": "companies",
        "contact": "contacts",
        "deal": "deals",
    }
    urls = await tools.hubspot_record_urls(
        request.tenant_id,
        [(object_types[entity], record_id) for entity, _, record_id in reused],
    )
    cards = [
        {
            "kind": entity,
            "title": f"{entity.capitalize()} already exists",
            "name": label,
            "hubspot_id": record_id,
            "hubspot_url": url,
        }
        for (entity, label, record_id), url in zip(reused, urls, strict=True)
    ]
    return AgentResponse(
        status="already_exists",
        text=text,
        request_id=request.request_id,
        tools_used=[],
        result={"kind": "existing_record", "title": title},
        cards=cards,
    )


def _response(request: AgentRequest, status: str, text: str) -> AgentResponse:
    return AgentResponse(status=status, text=text, request_id=request.request_id)
