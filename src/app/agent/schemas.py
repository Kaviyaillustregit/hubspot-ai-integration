from pydantic import BaseModel, ConfigDict, Field


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=3000)
    request_id: str = Field(min_length=1, max_length=128)


class GroundedSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crm_facts: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)


class AgentResponse(BaseModel):
    status: str
    text: str
    request_id: str
    tools_used: list[str] = Field(default_factory=list)
