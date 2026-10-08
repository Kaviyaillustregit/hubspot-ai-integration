from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.integrations.errors import IntegrationError
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.http import HubSpotApiClient
from app.integrations.hubspot.models import HubSpotRecord

CRMObjectType = Literal["contacts", "companies", "deals"]


class _AssociatedObject(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    to_object_id: str = Field(validation_alias="toObjectId")

    @field_validator("to_object_id", mode="before")
    @classmethod
    def _as_string(cls, value: object) -> str:
        return str(value)


class _AssociationsPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[_AssociatedObject] = Field(default_factory=list)
    paging: dict[str, dict[str, str]] | None = None


class _BatchReadResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[HubSpotRecord] = Field(default_factory=list)


class HubSpotAssociationsClient(HubSpotApiClient):
    """HubSpot v4 associations between CRM records, plus batch reads of linked records."""

    async def associate(
        self,
        context: TenantContext,
        access_token: str,
        *,
        from_type: CRMObjectType,
        from_id: str,
        to_type: CRMObjectType,
        to_id: str,
    ) -> None:
        # The "default" endpoint creates HubSpot's standard unlabeled association.
        await self._call(
            "PUT",
            f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}",
            access_token,
            label="HubSpot association",
        )

    async def list_associated_ids(
        self,
        context: TenantContext,
        access_token: str,
        *,
        from_type: CRMObjectType,
        from_id: str,
        to_type: CRMObjectType,
        limit: int = 100,
    ) -> list[str]:
        record_ids: list[str] = []
        after: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, str | int] = {"limit": limit}
            if after:
                params["after"] = after
            payload = await self._call(
                "GET",
                f"/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}",
                access_token,
                label="HubSpot associations",
                params=params,
            )
            try:
                page = _AssociationsPage.model_validate(payload or {})
            except (ValueError, TypeError) as exc:
                raise IntegrationError("HubSpot associations response was invalid") from exc
            record_ids.extend(item.to_object_id for item in page.results)
            next_after = page.paging.get("next", {}).get("after") if page.paging else None
            if next_after is None:
                return list(dict.fromkeys(record_ids))
            if next_after in seen:
                raise IntegrationError("HubSpot associations repeated a pagination cursor")
            seen.add(next_after)
            after = next_after

    async def read_records(
        self,
        context: TenantContext,
        access_token: str,
        *,
        object_type: CRMObjectType,
        record_ids: Sequence[str],
        properties: Sequence[str],
    ) -> list[HubSpotRecord]:
        if not record_ids:
            return []
        payload = await self._call(
            "POST",
            f"/crm/v3/objects/{object_type}/batch/read",
            access_token,
            label="HubSpot batch read",
            json_body={
                "inputs": [{"id": record_id} for record_id in record_ids],
                "properties": list(properties),
            },
        )
        try:
            return _BatchReadResponse.model_validate(payload).results
        except (ValueError, TypeError) as exc:
            raise IntegrationError("HubSpot batch read response was invalid") from exc
