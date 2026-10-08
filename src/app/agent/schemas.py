from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=3000)
    request_id: str = Field(min_length=1, max_length=128)
    # Source-message identity, used as the idempotency basis for direct CRM writes.
    channel_id: str | None = Field(default=None, max_length=128)
    message_ts: str | None = Field(default=None, max_length=64)
    event_id: str | None = Field(default=None, max_length=128)


CRMIntentName = Literal[
    "create_contact",
    "update_contact",
    "delete_contact",
    "crm_question",
    "unsupported",
    "create_company",
    "update_company",
    "delete_company",
    "create_deal",
    "update_deal",
    "associate_records",
    "multi_step",
]

# What the user wants done with each entity mentioned in the message.
EntityAction = Literal["create", "update", "reference"]
AssociationName = Literal["contact_company", "deal_company", "deal_contact"]
CRMQueryName = Literal[
    "company_details",
    "company_contacts",
    "company_deals",
    "contact_details",
    "contact_company",
    "deal_details",
    "open_deals",
    "closed_deals",
    "closed_won_deals",
    "closed_lost_deals",
    "closed_won_revenue",
    "best_chance_deals",
    "all_deals",
    "company_list",
    "company_search",
    "contact_list",
    "contact_search",
    "contact_deals",
]


class CRMIntentExtraction(BaseModel):
    """LLM interpretation of one message. Untrusted until validated by the agent."""

    model_config = ConfigDict(extra="forbid")

    intent: CRMIntentName
    # Contact fields.
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    job_title: str | None = Field(default=None, max_length=200)
    contact_id: str | None = Field(default=None, max_length=128)
    # Company fields.
    company_name: str | None = Field(default=None, max_length=200)
    company_domain: str | None = Field(default=None, max_length=253)
    company_website: str | None = Field(default=None, max_length=500)
    company_phone: str | None = Field(default=None, max_length=50)
    company_city: str | None = Field(default=None, max_length=100)
    company_employees: str | None = Field(default=None, max_length=9)
    # Deal fields. Amount, stage and pipeline are kept exactly as written.
    deal_name: str | None = Field(default=None, max_length=200)
    deal_amount: str | None = Field(default=None, max_length=50)
    deal_stage: str | None = Field(default=None, max_length=100)
    deal_pipeline: str | None = Field(default=None, max_length=100)
    # Structure of the request.
    contact_action: EntityAction | None = None
    company_action: EntityAction | None = None
    deal_action: EntityAction | None = None
    associations: list[AssociationName] = Field(default_factory=list, max_length=6)
    query: CRMQueryName | None = None
    requested_fields: list[str] = Field(default_factory=list, max_length=20)
    question: str | None = Field(default=None, max_length=3000)
    confidence: float = Field(ge=0, le=1)

class ContactCreateIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    firstname: str | None = Field(default=None, max_length=100)
    lastname: str | None = Field(default=None, max_length=100)

class ContactUpdateIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: str = Field(min_length=1, max_length=128)
    properties: dict[str, str | None] = Field(
        min_length=1,
    )

class ContactDeleteIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: str = Field(min_length=1, max_length=128)

class GroundedSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crm_facts: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)


class AgentResponse(BaseModel):
    status: str
    text: str
    request_id: str
    tools_used: list[str] = Field(default_factory=list)
    # Optional structured outcome for rich UIs (e.g. Slack App Home); `text` stays canonical.
    result: dict[str, object] | None = None
    # Per-record summaries of CRM writes for the web assistant's result cards.
    cards: list[dict[str, str]] = Field(default_factory=list)
