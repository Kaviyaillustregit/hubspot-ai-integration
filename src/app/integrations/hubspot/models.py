from pydantic import BaseModel, ConfigDict, Field, field_validator


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

class HubSpotContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    properties: dict[str, str | None]

class HubSpotContactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    properties: dict[str, str | None]

class HubSpotContactsPage(BaseModel):
    results: list[HubSpotContact]
    next_after: str | None = None


class HubSpotCompany(BaseModel):
    id: str
    properties: dict[str, str | None]


class HubSpotCompaniesPage(BaseModel):
    results: list[HubSpotCompany]
    next_after: str | None = None


class HubSpotAssociationType(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    category: str
    type_id: int = Field(validation_alias="typeId")
    label: str | None = None


class HubSpotContactCompanyAssociation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    company_id: str = Field(validation_alias="toObjectId")
    association_types: list[HubSpotAssociationType] = Field(
        validation_alias="associationTypes"
    )

    @field_validator("company_id", mode="before")
    @classmethod
    def normalize_company_id(cls, value: object) -> str:
        return str(value)


class HubSpotContactCompanyAssociations(BaseModel):
    results: list[HubSpotContactCompanyAssociation]

class HubSpotDeal(BaseModel):
    id: str
    properties: dict[str, str | None]


class HubSpotDealsPage(BaseModel):
    results: list[HubSpotDeal]
    next_after: str | None = None


class HubSpotPipelineStage(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    label: str
    display_order: int = Field(default=0, validation_alias="displayOrder")
    metadata: dict[str, str] = Field(default_factory=dict)


class HubSpotPipeline(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    label: str
    display_order: int = Field(default=0, validation_alias="displayOrder")
    stages: list[HubSpotPipelineStage] = Field(default_factory=list)
