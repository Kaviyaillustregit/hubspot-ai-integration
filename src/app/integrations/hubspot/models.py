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


class HubSpotContactCompanyAssociations(BaseModel):
    results: list[HubSpotContactCompanyAssociation]