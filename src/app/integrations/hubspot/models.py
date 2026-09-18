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


class HubSpotContact(BaseModel):
    id: str
    properties: dict[str, str | None]


class HubSpotContactsPage(BaseModel):
    results: list[HubSpotContact]
    next_after: str | None = None