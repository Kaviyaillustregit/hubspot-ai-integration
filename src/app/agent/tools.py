import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.integrations.errors import IntegrationError
from app.integrations.hubspot.associations import CRMObjectType
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import (
    HubSpotCompany,
    HubSpotContact,
    HubSpotContactCompanyAssociations,
    HubSpotDeal,
    HubSpotPipeline,
    HubSpotRecord,
)
from app.services.hubspot_associations import HubSpotAssociationsService
from app.services.hubspot_companies import HubSpotCompaniesService
from app.services.hubspot_contacts import HubSpotContactsService
from app.services.hubspot_deals import HubSpotDealsService

COMPANY_PROPERTIES = ("name", "domain", "website", "phone", "city", "industry", "description")
CONTACT_PROPERTIES = ("firstname", "lastname", "email", "phone", "jobtitle")
DEAL_PROPERTIES = ("dealname", "amount", "dealstage", "pipeline", "closedate")


@dataclass(frozen=True)
class AccountData:
    company: HubSpotCompany | None
    contacts: list[HubSpotContact]


ResolutionStatus = Literal["found", "not_found", "ambiguous"]


@dataclass(frozen=True)
class CompanyResolution:
    status: ResolutionStatus
    company: HubSpotCompany | None = None


@dataclass(frozen=True)
class ContactResolution:
    status: ResolutionStatus
    contact: HubSpotContact | None = None


@dataclass(frozen=True)
class DealResolution:
    status: ResolutionStatus
    deal: HubSpotDeal | None = None


# Generic words users and CRMs append to company names ("ABC company", "ABC Inc.").
_COMPANY_SUFFIX = re.compile(
    r"(?:[\s,]+(?:company|co|inc|incorporated|llc|ltd|limited|corp|corporation|plc|gmbh))+\.?$",
    re.IGNORECASE,
)


def normalize_company_name(name: str) -> str:
    core = " ".join(name.split()).strip(" .,")
    return _COMPANY_SUFFIX.sub("", core).strip(" .,")


def _same_text(left: str | None, right: str | None) -> bool:
    return " ".join((left or "").split()).casefold() == " ".join((right or "").split()).casefold()


_DEAL_SUFFIX = re.compile(r"\s+deal\.?$", re.IGNORECASE)


def normalize_deal_name(name: str) -> str:
    """Treat "ABC Enterprise Deal" and "ABC Enterprise" as the same deal name."""
    core = " ".join(name.split()).strip(" .,")
    return _DEAL_SUFFIX.sub("", core).strip(" .,").casefold()


class HubSpotToolRegistry:
    """Agent tools backed by the existing tenant OAuth services."""

    def __init__(
        self,
        companies: HubSpotCompaniesService,
        contacts: HubSpotContactsService,
        deals: HubSpotDealsService | None = None,
        associations: HubSpotAssociationsService | None = None,
    ) -> None:
        self._companies = companies
        self._contacts = contacts
        self._deals_service = deals
        self._associations_service = associations

    @property
    def _deals(self) -> HubSpotDealsService:
        if self._deals_service is None:
            raise IntegrationError("Deal tools are not configured")
        return self._deals_service

    @property
    def _associations(self) -> HubSpotAssociationsService:
        if self._associations_service is None:
            raise IntegrationError("Association tools are not configured")
        return self._associations_service

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

    async def resolve_company(self, tenant_id: str, name: str) -> CompanyResolution:
        """Find exactly one company whose name matches, ignoring case and generic suffixes."""
        core = normalize_company_name(name)
        if not core:
            return CompanyResolution("not_found")
        page = await self._companies.search_companies(
            self._context(tenant_id),
            query=core,
            limit=100,
            properties=("name", "domain", "industry", "description"),
        )
        needle = core.casefold()
        matches = {
            item.id: item
            for item in page.results
            if normalize_company_name(item.properties.get("name") or "").casefold() == needle
        }
        if not matches:
            return CompanyResolution("not_found")
        if len(matches) > 1:
            return CompanyResolution("ambiguous")
        return CompanyResolution("found", next(iter(matches.values())))

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
        company_id: str | None = None,
    ) -> HubSpotContact:
        if company_id is None:
            return await self._contacts.create_contact(
                self._context(tenant_id),
                properties=properties,
            )
        return await self._contacts.create_contact(
            self._context(tenant_id),
            properties=properties,
            company_id=company_id,
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

    async def get_company(self, tenant_id: str, company_id: str) -> HubSpotCompany:
        return await self._companies.get_company(
            self._context(tenant_id), company_id=company_id, properties=COMPANY_PROPERTIES
        )

    async def create_company(self, tenant_id: str, properties: dict[str, str]) -> HubSpotCompany:
        return await self._companies.create_company(
            self._context(tenant_id), properties=properties
        )

    async def update_company(
        self, tenant_id: str, company_id: str, properties: dict[str, str]
    ) -> HubSpotCompany:
        return await self._companies.update_company(
            self._context(tenant_id), company_id=company_id, properties=properties
        )

    async def resolve_contact(
        self,
        tenant_id: str,
        *,
        email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> ContactResolution:
        """Find exactly one existing contact by email, or by exact first/last name."""
        if email:
            contact = await self._contacts.find_contact_by_email(tenant_id, email)
            if contact is None:
                return ContactResolution("not_found")
            return ContactResolution("found", contact)
        if not first_name and not last_name:
            return ContactResolution("not_found")
        query = " ".join(part for part in (first_name, last_name) if part)
        page = await self._contacts.search_contacts(
            self._context(tenant_id), query=query, limit=100, properties=CONTACT_PROPERTIES
        )
        matches = {
            item.id: item
            for item in page.results
            if (not first_name or _same_text(item.properties.get("firstname"), first_name))
            and (not last_name or _same_text(item.properties.get("lastname"), last_name))
        }
        if not matches:
            return ContactResolution("not_found")
        if len(matches) > 1:
            return ContactResolution("ambiguous")
        return ContactResolution("found", next(iter(matches.values())))

    async def resolve_deal(self, tenant_id: str, name: str) -> DealResolution:
        """Find exactly one existing deal whose name matches, ignoring case and a "deal" suffix."""
        needle = normalize_deal_name(name)
        if not needle:
            return DealResolution("not_found")
        page = await self._deals.search_deals(
            self._context(tenant_id), query=name.strip(), limit=100, properties=DEAL_PROPERTIES
        )
        matches = {
            item.id: item
            for item in page.results
            if normalize_deal_name(item.properties.get("dealname") or "") == needle
        }
        if not matches:
            return DealResolution("not_found")
        if len(matches) > 1:
            return DealResolution("ambiguous")
        return DealResolution("found", next(iter(matches.values())))

    async def get_deal(self, tenant_id: str, deal_id: str) -> HubSpotDeal:
        return await self._deals.get_deal(
            self._context(tenant_id), deal_id=deal_id, properties=DEAL_PROPERTIES
        )

    async def create_deal(self, tenant_id: str, properties: dict[str, str]) -> HubSpotDeal:
        return await self._deals.create_deal(self._context(tenant_id), properties=properties)

    async def update_deal(
        self, tenant_id: str, deal_id: str, properties: dict[str, str]
    ) -> HubSpotDeal:
        return await self._deals.update_deal(
            self._context(tenant_id), deal_id=deal_id, properties=properties
        )

    async def deal_pipelines(self, tenant_id: str) -> list[HubSpotPipeline]:
        return await self._deals.list_pipelines(self._context(tenant_id))

    async def associate(
        self,
        tenant_id: str,
        *,
        from_type: CRMObjectType,
        from_id: str,
        to_type: CRMObjectType,
        to_id: str,
    ) -> None:
        await self._associations.associate(
            self._context(tenant_id),
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            to_id=to_id,
        )

    async def associated_records(
        self,
        tenant_id: str,
        *,
        from_type: CRMObjectType,
        from_id: str,
        to_type: CRMObjectType,
    ) -> list[HubSpotRecord]:
        properties = {
            "contacts": CONTACT_PROPERTIES,
            "companies": COMPANY_PROPERTIES,
            "deals": DEAL_PROPERTIES,
        }[to_type]
        return await self._associations.associated_records(
            self._context(tenant_id),
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            properties=properties,
        )

    @property
    def names(self) -> Sequence[str]:
        return (
            "find_company",
            "resolve_company",
            "get_company",
            "create_company",
            "update_company",
            "find_contacts",
            "resolve_contact",
            "create_contact",
            "update_contact",
            "delete_contact",
            "contact_company_associations",
            "contacts_for_company",
            "resolve_deal",
            "get_deal",
            "create_deal",
            "update_deal",
            "deal_pipelines",
            "associate",
            "associated_records",
        )
