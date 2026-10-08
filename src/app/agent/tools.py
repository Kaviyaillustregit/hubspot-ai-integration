import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal, TypeVar
from urllib.parse import quote

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

COMPANY_PROPERTIES = (
    "name",
    "hubspot_owner_id",
    "phone",
    "city",
    "industry",
    "numberofemployees",
    "lifecyclestage",
    "hs_lead_status",
    "notes_last_contacted",
)
CONTACT_PROPERTIES = (
    "firstname",
    "lastname",
    "hubspot_owner_id",
    "email",
    "phone",
    "city",
    "state",
    "country",
    "industry",
    "lifecyclestage",
    "hs_lead_status",
    "notes_last_contacted",
    "jobtitle",
    "hs_role",
    "hs_sub_role",
    "hs_seniority",
    "hs_linkedin_url",
    "associatedcompanyid",
)
DEAL_PROPERTIES = (
    "dealname",
    "amount",
    "dealstage",
    "pipeline",
    "closedate",
    "hs_probability",
    "hs_deal_stage_probability",
    "hubspot_owner_id",
)
DEAL_LIST_PROPERTIES = (
    *DEAL_PROPERTIES,
    "hs_probability",
    "hs_deal_stage_probability",
    "hubspot_owner_id",
)


@dataclass(frozen=True)
class AccountData:
    company: HubSpotCompany | None
    contacts: list[HubSpotContact]


ResolutionStatus = Literal["found", "not_found", "ambiguous"]
_T = TypeVar("_T")
_FUZZY_MATCH_THRESHOLD = 0.88
_FUZZY_MATCH_MARGIN = 0.04


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


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _exact_text_matches(
    records: Sequence[_T], needle: str, text: Callable[[_T], str]
) -> list[_T]:
    normalized_needle = _normalize_text(needle)
    if not normalized_needle:
        return []
    return [
        record
        for record in records
        if _normalize_text(text(record)) == normalized_needle
    ]


def _resolve_text_match(
    records: Sequence[_T], needle: str, text: Callable[[_T], str]
) -> tuple[ResolutionStatus, _T | None]:
    exact_matches = _exact_text_matches(records, needle, text)
    if exact_matches:
        if len(exact_matches) > 1:
            return "ambiguous", None
        return "found", exact_matches[0]

    normalized_needle = _normalize_text(needle)
    if not normalized_needle:
        return "not_found", None
    scored = [
        (
            SequenceMatcher(
                None, normalized_needle, _normalize_text(text(record)), autojunk=False
            ).ratio(),
            record,
        )
        for record in records
    ]
    scored = [item for item in scored if item[0] >= _FUZZY_MATCH_THRESHOLD]
    if not scored:
        return "not_found", None

    best_score = max(score for score, _ in scored)
    best_matches = [
        record for score, record in scored if best_score - score <= _FUZZY_MATCH_MARGIN
    ]
    if len(best_matches) > 1:
        return "ambiguous", None
    return "found", best_matches[0]


def _search_terms(value: str) -> list[str]:
    return list(dict.fromkeys(term for term in _normalize_text(value).split() if len(term) > 1))


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
        companies = await self._search_companies(tenant_id, name)
        match = _resolve_text_match(
            companies,
            normalize_company_name(name),
            lambda item: normalize_company_name(item.properties.get("name") or ""),
        )
        return match[1]

    async def resolve_company(self, tenant_id: str, name: str) -> CompanyResolution:
        """Find one company by normalized name or a confident, unambiguous near-match."""
        core = normalize_company_name(name)
        if not core:
            return CompanyResolution("not_found")
        initial = await self._search_companies(tenant_id, _normalize_text(core))
        candidates = {item.id: item for item in initial}
        exact_matches = _exact_text_matches(
            list(candidates.values()),
            core,
            lambda item: normalize_company_name(item.properties.get("name") or ""),
        )
        if len(exact_matches) > 1:
            return CompanyResolution("ambiguous")
        if exact_matches:
            return CompanyResolution("found", exact_matches[0])

        for term in _search_terms(core):
            candidates.update(
                (item.id, item) for item in await self._search_companies(tenant_id, term)
            )

        match = _resolve_text_match(
            list(candidates.values()),
            core,
            lambda item: normalize_company_name(item.properties.get("name") or ""),
        )
        return CompanyResolution(*match)

    async def find_contacts(self, tenant_id: str, query: str) -> list[HubSpotContact]:
        records = await self._search_contacts(tenant_id, query)
        needle = _normalize_text(query)
        return [
            item
            for item in records
            if needle in _normalize_text(
                " ".join(value or "" for value in item.properties.values())
            )
        ]

    async def _search_companies(self, tenant_id: str, query: str) -> list[HubSpotCompany]:
        records: list[HubSpotCompany] = []
        after: str | None = None
        seen: set[str] = set()
        while True:
            if after is None:
                page = await self._companies.search_companies(
                    self._context(tenant_id),
                    query=query,
                    limit=100,
                    properties=COMPANY_PROPERTIES,
                )
            else:
                page = await self._companies.search_companies(
                    self._context(tenant_id),
                    query=query,
                    limit=100,
                    after=after,
                    properties=COMPANY_PROPERTIES,
                )
            records.extend(page.results)
            if page.next_after is None:
                return records
            if page.next_after in seen:
                raise IntegrationError("HubSpot company search repeated a pagination cursor")
            seen.add(page.next_after)
            after = page.next_after

    async def _search_contacts(self, tenant_id: str, query: str) -> list[HubSpotContact]:
        records: list[HubSpotContact] = []
        after: str | None = None
        seen: set[str] = set()
        while True:
            if after is None:
                page = await self._contacts.search_contacts(
                    self._context(tenant_id),
                    query=query,
                    limit=100,
                    properties=CONTACT_PROPERTIES,
                )
            else:
                page = await self._contacts.search_contacts(
                    self._context(tenant_id),
                    query=query,
                    limit=100,
                    after=after,
                    properties=CONTACT_PROPERTIES,
                )
            records.extend(page.results)
            if page.next_after is None:
                return records
            if page.next_after in seen:
                raise IntegrationError("HubSpot contact search repeated a pagination cursor")
            seen.add(page.next_after)
            after = page.next_after

    async def list_all_companies(self, tenant_id: str) -> list[HubSpotCompany]:
        records: list[HubSpotCompany] = []
        after: str | None = None
        seen: set[str] = set()
        while True:
            page = await self._companies.list_companies(
                self._context(tenant_id),
                limit=100,
                after=after,
                properties=COMPANY_PROPERTIES,
            )
            records.extend(page.results)
            if page.next_after is None:
                return records
            if page.next_after in seen:
                raise IntegrationError("HubSpot company listing repeated a pagination cursor")
            seen.add(page.next_after)
            after = page.next_after

    async def list_all_contacts(self, tenant_id: str) -> list[HubSpotContact]:
        records: list[HubSpotContact] = []
        after: str | None = None
        seen: set[str] = set()
        while True:
            page = await self._contacts.list_contacts(
                self._context(tenant_id),
                limit=100,
                after=after,
                properties=CONTACT_PROPERTIES,
            )
            records.extend(page.results)
            if page.next_after is None:
                return records
            if page.next_after in seen:
                raise IntegrationError("HubSpot contact listing repeated a pagination cursor")
            seen.add(page.next_after)
            after = page.next_after

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
        page_records = await self.list_all_contacts(tenant_id)
        matches: list[HubSpotContact] = []
        for contact in page_records:
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

    async def get_contact(self, tenant_id: str, contact_id: str) -> HubSpotContact:
        return await self._contacts.get_contact(
            self._context(tenant_id), contact_id=contact_id, properties=CONTACT_PROPERTIES
        )

    async def search_companies(self, tenant_id: str, query: str) -> list[HubSpotCompany]:
        return await self._search_companies(tenant_id, query)

    async def search_contacts(self, tenant_id: str, query: str) -> list[HubSpotContact]:
        return await self._search_contacts(tenant_id, query)


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
        """Find one contact by email or a confident, unambiguous first/last-name match."""
        if email:
            contact = await self._contacts.find_contact_by_email(tenant_id, email)
            if contact is None:
                return ContactResolution("not_found")
            return ContactResolution("found", contact)
        if not first_name and not last_name:
            return ContactResolution("not_found")
        query = " ".join(part for part in (first_name, last_name) if part)
        candidates = {
            item.id: item for item in await self._search_contacts(tenant_id, _normalize_text(query))
        }

        def contact_name(item: HubSpotContact) -> str:
            return " ".join(
                part
                for part in (
                    item.properties.get("firstname") if first_name else None,
                    item.properties.get("lastname") if last_name else None,
                )
                if part
            )

        exact_matches = _exact_text_matches(list(candidates.values()), query, contact_name)
        if len(exact_matches) > 1:
            return ContactResolution("ambiguous")
        if exact_matches:
            return ContactResolution("found", exact_matches[0])

        for term in _search_terms(query):
            candidates.update(
                (item.id, item) for item in await self._search_contacts(tenant_id, term)
            )

        match = _resolve_text_match(list(candidates.values()), query, contact_name)
        return ContactResolution(*match)

    async def resolve_deal(self, tenant_id: str, name: str) -> DealResolution:
        """Find exactly one existing deal whose name matches, ignoring case and a "deal" suffix."""
        needle = normalize_deal_name(name)
        if not needle:
            return DealResolution("not_found")
        candidates = {item.id: item for item in await self._search_deals(tenant_id, needle)}

        def deal_name(item: HubSpotDeal) -> str:
            return normalize_deal_name(item.properties.get("dealname") or "")

        exact_matches = _exact_text_matches(list(candidates.values()), needle, deal_name)
        if len(exact_matches) > 1:
            return DealResolution("ambiguous")
        if exact_matches:
            return DealResolution("found", exact_matches[0])

        for term in _search_terms(needle):
            candidates.update(
                (item.id, item) for item in await self._search_deals(tenant_id, term)
            )

        match = _resolve_text_match(list(candidates.values()), needle, deal_name)
        return DealResolution(*match)

    async def _search_deals(self, tenant_id: str, query: str) -> list[HubSpotDeal]:
        records: list[HubSpotDeal] = []
        after: str | None = None
        seen: set[str] = set()
        while True:
            if after is None:
                page = await self._deals.search_deals(
                    self._context(tenant_id),
                    query=query,
                    limit=100,
                    properties=DEAL_PROPERTIES,
                )
            else:
                page = await self._deals.search_deals(
                    self._context(tenant_id),
                    query=query,
                    limit=100,
                    after=after,
                    properties=DEAL_PROPERTIES,
                )
            records.extend(page.results)
            if page.next_after is None:
                return records
            if page.next_after in seen:
                raise IntegrationError("HubSpot deal search repeated a pagination cursor")
            seen.add(page.next_after)
            after = page.next_after

    async def get_deal(self, tenant_id: str, deal_id: str) -> HubSpotDeal:
        return await self._deals.get_deal(
            self._context(tenant_id), deal_id=deal_id, properties=DEAL_PROPERTIES
        )

    async def list_all_deals(self, tenant_id: str) -> list[HubSpotDeal]:
        """Read every deal page, rejecting repeated cursors rather than truncating silently."""
        deals: list[HubSpotDeal] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page = await self._deals.list_deals(
                self._context(tenant_id),
                limit=100,
                after=after,
                properties=DEAL_LIST_PROPERTIES,
            )
            deals.extend(page.results)
            if page.next_after is None:
                return deals
            if page.next_after in seen_cursors or page.next_after == after:
                raise IntegrationError("HubSpot deal listing returned a repeated pagination cursor")
            seen_cursors.add(page.next_after)
            after = page.next_after

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

    async def hubspot_record_urls(
        self, tenant_id: str, records: Sequence[tuple[CRMObjectType, str]]
    ) -> list[str]:
        account_id = await self._contacts.get_hubspot_account_id(self._context(tenant_id))
        object_type_ids = {"contacts": "0-1", "companies": "0-2", "deals": "0-3"}
        return [
            f"https://app.hubspot.com/contacts/{quote(account_id, safe='')}"
            f"/record/{object_type_ids[object_type]}/{quote(record_id, safe='')}"
            for object_type, record_id in records
        ]

    @property
    def names(self) -> Sequence[str]:
        return (
            "find_company",
            "resolve_company",
            "list_all_companies",
            "search_companies",
            "get_company",
            "create_company",
            "update_company",
            "find_contacts",
            "list_all_contacts",
            "search_contacts",
            "get_contact",
            "resolve_contact",
            "create_contact",
            "update_contact",
            "delete_contact",
            "contact_company_associations",
            "contacts_for_company",
            "resolve_deal",
            "get_deal",
            "list_all_deals",
            "create_deal",
            "update_deal",
            "deal_pipelines",
            "associate",
            "associated_records",
        )
