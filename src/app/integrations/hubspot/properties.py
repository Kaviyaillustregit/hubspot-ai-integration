from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.errors import IntegrationError
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.http import HubSpotApiClient

CRMPropertyObject = Literal["contacts", "companies", "deals"]


class HubSpotPropertyDefinition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    label: str
    type: str = "string"
    field_type: str | None = Field(default=None, validation_alias="fieldType")
    description: str | None = None
    options: list[dict[str, str]] = Field(default_factory=list)


class _PropertiesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[HubSpotPropertyDefinition] = Field(default_factory=list)
    paging: dict[str, dict[str, str]] | None = None


class HubSpotPropertiesClient(HubSpotApiClient):
    """Read-only access to the non-sensitive HubSpot property catalog."""

    async def list_properties(
        self,
        context: TenantContext,
        access_token: str,
        *,
        object_type: CRMPropertyObject,
    ) -> list[HubSpotPropertyDefinition]:
        cursor: str | None = None
        properties: list[HubSpotPropertyDefinition] = []
        seen: set[str] = set()
        for _ in range(10):
            params = {"archived": "false", "dataSensitivity": "non_sensitive"}
            if cursor:
                params["after"] = cursor
            payload = await self._call(
                "GET",
                f"/crm/v3/properties/{object_type}",
                access_token,
                label=f"HubSpot {object_type} properties",
                params=params,
            )
            try:
                page = _PropertiesResponse.model_validate(payload)
            except (ValueError, TypeError) as exc:
                raise IntegrationError("HubSpot property catalog response was invalid") from exc
            properties.extend(page.results)
            next_cursor = page.paging.get("next", {}).get("after") if page.paging else None
            if not next_cursor:
                return properties
            if next_cursor in seen:
                raise IntegrationError("HubSpot property catalog repeated a pagination cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        raise IntegrationError("HubSpot property catalog exceeded its page limit")
