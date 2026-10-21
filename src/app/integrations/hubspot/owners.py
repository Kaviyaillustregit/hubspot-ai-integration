from pydantic import BaseModel, ConfigDict, Field

from app.integrations.errors import IntegrationError
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.http import HubSpotApiClient


class HubSpotOwner(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    first_name: str | None = Field(default=None, validation_alias="firstName")
    last_name: str | None = Field(default=None, validation_alias="lastName")
    email: str | None = None

    @property
    def display_name(self) -> str:
        return (
            " ".join(part for part in (self.first_name, self.last_name) if part)
            or self.email
            or self.id
        )


class _OwnersResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[HubSpotOwner] = Field(default_factory=list)
    paging: dict[str, dict[str, str]] | None = None


class HubSpotOwnersClient(HubSpotApiClient):
    """Read-only, cursor-paginated HubSpot owner lookup."""

    async def list_owners(self, context: TenantContext, access_token: str) -> list[HubSpotOwner]:
        cursor: str | None = None
        owners: list[HubSpotOwner] = []
        seen: set[str] = set()
        for _ in range(10):
            params: dict[str, str] = {"archived": "false", "limit": "500"}
            if cursor:
                params["after"] = cursor
            payload = await self._call(
                "GET", "/crm/v3/owners", access_token, label="HubSpot owners", params=params
            )
            try:
                page = _OwnersResponse.model_validate(payload)
            except (ValueError, TypeError) as exc:
                raise IntegrationError("HubSpot owner response was invalid") from exc
            owners.extend(page.results)
            next_cursor = page.paging.get("next", {}).get("after") if page.paging else None
            if not next_cursor:
                return owners
            if next_cursor in seen:
                raise IntegrationError("HubSpot owner listing repeated a pagination cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        raise IntegrationError("HubSpot owner listing exceeded its page limit")
