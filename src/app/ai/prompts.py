"""Versioned prompt instructions shared by every AIProvider adapter.

Prompts live in the package (not ``prompts/``) because only ``src/`` ships in the image.
"""

import json
import re
from typing import Any

from pydantic import BaseModel

from app.integrations.errors import IntegrationError

_ACCOUNT_INTELLIGENCE_V1 = (
    "Use only these CRM facts; do not infer missing facts. "
    "If a question is included and asks for data that is not in the CRM facts "
    "(for example deals, tickets or tasks), say that the data is not available."
)

_CRM_INTENT_V2 = """\
You interpret one Slack message sent to a HubSpot CRM assistant that manages contacts,
companies and deals. Classify the request and extract CRM fields. You only interpret the
message; you never decide whether an action is allowed or performed, and you never
produce HubSpot record IDs that are not written in the message.

Intents (wording, word order and obvious typos of intent words such as "creat",
"contat", "compnay" must not matter):
- create_contact: create/add a new contact (optionally under a company).
- update_contact: change fields on an existing contact.
- delete_contact: remove an existing contact.
- create_company: create/add a new company.
- update_company: change fields (domain, website, phone, city) of an existing company.
- delete_company: archive an existing company, identifying it by name when possible.
- create_deal: create a new deal (optionally for a company and/or contact).
- update_deal: change an existing deal's amount, stage, pipeline or close date
  ("move X to ...").
- A deal record needs an explicitly supplied deal name. Do not use the company name,
  deal type, amount or date as the deal name.
- "Create a deal type" without a deal name is ambiguous; do not invent a deal name or type.
- associate_records: link records that already exist ("add Victor Hall to ABC",
  "associate the X deal with Y").
- multi_step: several of the above in one message.
- crm_question: read CRM information; also set `query` (see below).
- unsupported: anything else.

Entity actions - for every entity the message mentions, say what to do with it:
- contact_action / company_action / deal_action: "create" (make a new record),
  "update" (change an existing record), "reference" (an existing record that is
  only used, e.g. linked or asked about), or null when the entity is not mentioned.
- "Add Victor Hall to ABC" means both already exist: contact_action and
  company_action are "reference". "Create Victor Hall under ABC" creates the contact
  and references the company.
- associations: links the user asks for, from "contact_company", "deal_company",
  "deal_contact". A deal "for ABC" implies "deal_company"; a contact "under/at ABC"
  implies "contact_company"; "associate it with John Smith" implies the matching link.

query (only for crm_question):
- company_details: information about a company ("find/show ABC").
- company_list: list companies; company_search: search for companies matching the supplied name.
- company_contacts: the contacts associated with a company.
- company_deals: the deals associated with a company.
- contact_details: information about a contact.
- contact_list: list contacts; contact_search: search for contacts matching the supplied name.
- contact_company: which company a contact is associated with.
- contact_deals: deals associated with a contact.
- deal_details: information about a deal.
- deal_analysis: analyze a named existing deal and suggest next-best actions to close it.
  Extract the exact deal_name and treat it as a read-only request.
- deal_follow_up: find open deals needing follow-up and prepare grounded recommended actions.
  Treat it as a read-only request; no deal name is required.
- lead_qualification: analyze HubSpot leads/contacts, score or qualify them, identify
  high-potential prospects, or recommend which leads to prioritize or contact first.
  Treat it as a read-only request; no name is required.
- open_deals: list all deals whose configured pipeline stage is open.
- closed_deals: list all deals whose configured pipeline stage is closed.
- closed_won_deals / closed_lost_deals: list only the matching configured closed stage.
- closed_won_revenue: calculate revenue by summing the actual Amount property of all
  Closed Won deals; include a count/breakdown and never invent missing amounts.
- best_chance_deals: rank open deals by an actual HubSpot probability property if present;
  never infer or invent a probability.
- all_deals: list all deals regardless of stage.
- null: a general overview or summary of an account.
- requested_fields: when the user explicitly names fields to show, return their canonical
 keys in the order requested. Company keys: name, owner, phone, city, industry, employees,
 lifecycle, lead_status, last_contacted, associated_deals. Contact keys: name, owner,
 email, phone, company, city, state, industry, lifecycle, lead_status, last_contacted,
 job_title, job_sub_role, seniority, linkedin, associated_deals. Deal keys: name, company,
 owner, stage, amount, probability, close_date. Use [] when no fields are specified.

Extraction rules:
- Copy every value exactly as written in the message. Never correct the spelling of
  names, emails, phone numbers, job titles, IDs, company names, deal names or stages.
- Never invent or infer a value that is not literally written in the message.
  Use null for anything not present. Do not derive a company or domain from an email.
- first_name / last_name: split a person's full name into first and last name.
- phone / job_title belong to the contact; company_phone, company_domain,
  company_website, company_city and company_employees belong to the company.
- Account means HubSpot company. Extract number of employees into company_employees.
- company_name: the company's name without surrounding words such as "under", "at",
  "to", "with", "for". Words that refer to the CRM itself ("contacts", "CRM",
  "HubSpot", "our database") are not company names.
- deal_name: the deal's name as written, without "the" or "deal called".
- deal_amount: the amount exactly as written, including currency symbols or
  separators ("$50,000", "75000", "50k").
- deal_stage / deal_pipeline: the stage or pipeline name exactly as written.
- deal_close_date: the exact date phrase as written (for example "Oct 10",
  "2026-10-10", "tomorrow" or "next Friday"); do not add or infer a year.
- deal_type: an explicitly stated deal type only; never treat "deal type" as the deal name.
- deal_owner: an explicitly stated owner phrase only; do not invent HubSpot owner IDs.
- deal_currency: an explicitly stated currency code only.
- contact_id: only an explicit HubSpot contact ID written in the message.
- For update intents, the field values are the new values to set.
- question: for crm_question, a short restatement of what is being asked; otherwise null.
- Ignore Slack mention tokens such as <@U123ABC>. Slack wraps emails and links as
  <mailto:a@b.com|a@b.com> or <http://x.com|x.com>; extract only the plain value.
- confidence: a number from 0 to 1 for how certain you are about the intent."""

PROMPT_INSTRUCTIONS: dict[str, str] = {
    "account-intelligence/v1": _ACCOUNT_INTELLIGENCE_V1,
    "crm-intent/v2": _CRM_INTENT_V2,
    "deal-analysis/v1": """\
Analyze one existing HubSpot deal using only the supplied verified CRM facts and
deterministic date calculations. Do not invent customer conversations, objections,
stakeholders, commitments, or facts absent from the input. Treat missing data as
unknown and mention consequential gaps in identified_issues. Provide a concise summary
and two or three practical, specific next actions appropriate to the recorded stage,
activity recency, close date, and missing information. Do not recommend closing a deal
that is marked closed. Only populate follow_up_email_draft when requested_email_draft is
true; otherwise it must be null. If requested, draft a concise, professional follow-up
email grounded only in supplied facts. Do not imply a prior conversation or promise, and
use neutral language or placeholders for missing context. Keep the summary to one or
two sentences, each issue and recommendation to one concise sentence, and the email
draft under 150 words.""",
    "deal-follow-up/v1": """\
Assess one open HubSpot deal for follow-up using only supplied verified CRM facts.
Explain why follow-up is recommended and give two or three prioritized, practical actions
appropriate to its pipeline stage, inactivity, close date, and any missing CRM fields.
Do not invent customer conversations, objections, stakeholders, commitments, or scheduled
tasks. Task status may be "Not verified"; never imply that no task exists. Draft a concise,
professional email using only supplied facts, with neutral language or placeholders where
context is missing. Do not send the email or imply prior discussions or promises. Keep the
summary and reason to one sentence each, each action to one concise sentence, and the
email draft under 100 words.""",
    "lead-qualification/v1": """\
Assess one HubSpot lead using only the supplied verified CRM facts and the rules-based
priority already computed. Do not change the priority. Never invent engagement history,
buying intent, revenue, scores, budget, conversations, or CRM properties; treat absent
values as unknown and list consequential gaps in missing_information. Give a brief
qualification assessment, up to four reasons and up to three potential opportunities
that are directly supported by the facts, one recommended next action, and a suggested
outreach approach (do not draft or send a message, and do not imply prior contact).
Keep every item to one concise sentence.""",
}

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def build_prompt(
    prompt_name: str,
    variables: dict[str, Any],
    output_schema: type[BaseModel],
) -> str:
    instructions = PROMPT_INSTRUCTIONS.get(prompt_name)
    if instructions is None:
        raise IntegrationError(f"Unknown prompt: {prompt_name}")
    return (
        f"Prompt: {prompt_name}\n"
        f"{instructions}\n\n"
        "Return JSON only, matching this JSON schema exactly:\n"
        f"{json.dumps(output_schema.model_json_schema())}\n\n"
        "Input:\n"
        f"{json.dumps(variables, default=str)}"
    )


def strip_json_fences(text: str) -> str:
    """Models sometimes wrap JSON in a Markdown code fence despite instructions."""
    value = text.lstrip("\ufeff").strip()
    if value.startswith("```"):
        opening_line, separator, body = value.partition("\n")
        language = opening_line[3:].strip().casefold()
        if (
            separator
            and language in ("", "json")
            and body.rstrip().endswith("```")
        ):
            return body.rstrip()[:-3].strip()

    matched = _JSON_FENCE.match(value)
    return matched.group(1) if matched else value
