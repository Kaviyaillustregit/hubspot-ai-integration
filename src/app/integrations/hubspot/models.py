from pydantic import BaseModel, ConfigDict, Field


class HubSpotRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    properties: dict[str, str | None] = Field(default_factory=dict)


class HubSpotTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    due_date: str | None = None
    owner_id: str | None = None