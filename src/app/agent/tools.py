from collections.abc import Sequence
from dataclasses import dataclass

from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import (
    HubSpotCompany,
    HubSpotContact,
    HubSpotContactCompanyAssociations,
)
from app.services.hubspot_companies import HubSpotCompaniesService
from app.services.hubspot_contacts import HubSpotContactsService


@dataclass(frozen=True)
class AccountData:
    company: HubSpotCompany | None
    contacts: list[HubSpotContact]


class HubSpotToolRegistry:
    """Read-only agent tools backed by the existing tenant OAuth services."""

    def __init__(
        self, companies: HubSpotCompaniesService, contacts: HubSpotContactsService
    ) -> None:
        self._companies = companies
        self._contacts = contacts

    @staticmethod
    def _context(tenant_id: str) -> TenantContext:
        return TenantContext(tenant_id, "", "hubspot-oauth-token")

    async def find_company(self, tenant_id: str, name: str) -> HubSpotCompany | None:
        page = await self._companies.list_companies(
            self._context(tenant_id),
            limit=100,
            properties=("name", "domain", "industry", "description"),
        )
        needle = name.casefold().strip()
        return next(
            (
                item
                for item in page.results
                if (item.properties.get("name") or "").casefold() == needle
            ),
            None,
        )

    async def find_contacts(self, tenant_id: str, query: str) -> list[HubSpotContact]:
        page = await self._contacts.list_contacts(
            self._context(tenant_id),
            limit=100,
            properties=("firstname", "lastname", "email", "jobtitle"),
        )
        needle = query.casefold().strip()
        return [
            item
            for item in page.results
            if needle in " ".join(value or "" for value in item.properties.values()).casefold()
        ]

    async def contact_company_associations(
        self, tenant_id: str, contact_id: str
    ) -> HubSpotContactCompanyAssociations:
        return await self._companies.get_contact_company_associations(
            self._context(tenant_id), contact_id
        )

    async def contacts_for_company(
        self, tenant_id: str, company_id: str
    ) -> list[HubSpotContact]:
        # Existing APIs expose contact -> company associations. Keep this bounded for this slice.
        page = await self._contacts.list_contacts(
            self._context(tenant_id),
            limit=100,
            properties=("firstname", "lastname", "email", "jobtitle"),
        )
        matches: list[HubSpotContact] = []
        for contact in page.results:
            associations = await self.contact_company_associations(tenant_id, contact.id)
            if any(item.company_id == company_id for item in associations.results):
                matches.append(contact)
        return matches

    async def create_contact(
        self,
        tenant_id: str,
        properties: dict[str, str | None],
    ) -> HubSpotContact:
        return await self._contacts.create_contact(
            self._context(tenant_id),
            properties=properties,
        )

    async def update_contact(
        self,
        tenant_id: str,
        contact_id: str,
        properties: dict[str, str | None],
    ) -> HubSpotContact:
        return await self._contacts.update_contact(
            self._context(tenant_id),
            contact_id=contact_id,
            properties=properties,
        )

    async def delete_contact(
        self,
        tenant_id: str,
        contact_id: str,
    ) -> None:
        await self._contacts.delete_contact(
            self._context(tenant_id),
            contact_id=contact_id,
        )

    @property
    def names(self) -> Sequence[str]:
        return ("find_company", "find_contacts", "contact_company_associations")
