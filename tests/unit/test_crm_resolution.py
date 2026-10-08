import pytest

from app.agent.tools import HubSpotToolRegistry
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotCompany,
    HubSpotContact,
    HubSpotContactsPage,
    HubSpotDeal,
    HubSpotDealsPage,
)


class Companies:
    def __init__(self, records: list[HubSpotCompany]) -> None:
        self.records = records

    async def search_companies(self, context, *, query, limit, properties):
        return HubSpotCompaniesPage(
            results=[
                record
                for record in self.records
                if query.casefold() in (record.properties.get("name") or "").casefold()
            ]
        )

    async def list_companies(self, context, **kwargs):
        return HubSpotCompaniesPage(results=self.records)


class Contacts:
    def __init__(self, records: list[HubSpotContact]) -> None:
        self.records = records

    async def search_contacts(self, context, *, query, limit, properties):
        words = query.casefold().split()
        return HubSpotContactsPage(
            results=[
                record
                for record in self.records
                if all(
                    word
                    in " ".join(value or "" for value in record.properties.values()).casefold()
                    for word in words
                )
            ]
        )


class Deals:
    def __init__(self, records: list[HubSpotDeal]) -> None:
        self.records = records

    async def search_deals(self, context, *, query, limit, properties):
        return HubSpotDealsPage(
            results=[
                record
                for record in self.records
                if query.casefold() in (record.properties.get("dealname") or "").casefold()
            ]
        )


def tools(
    companies: list[HubSpotCompany] | None = None,
    contacts: list[HubSpotContact] | None = None,
    deals: list[HubSpotDeal] | None = None,
) -> HubSpotToolRegistry:
    return HubSpotToolRegistry(
        Companies(companies or []),  # type: ignore[arg-type]
        Contacts(contacts or []),  # type: ignore[arg-type]
        Deals(deals or []),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_company_matching_is_case_and_whitespace_insensitive():
    company = HubSpotCompany(id="company-1", properties={"name": "Demo AI Company 101"})

    result = await tools(companies=[company]).resolve_company(
        "tenant-a", "  demo   ai COMPANY 101  "
    )

    assert result.status == "found"
    assert result.company == company


@pytest.mark.asyncio
async def test_company_matching_accepts_a_minor_typo():
    company = HubSpotCompany(id="company-1", properties={"name": "Demo AI Company 101"})

    result = await tools(companies=[company]).resolve_company(
        "tenant-a", "demo Ai comapny 101"
    )

    assert result.status == "found"
    assert result.company == company


@pytest.mark.asyncio
async def test_contact_matching_is_case_and_whitespace_insensitive():
    contact = HubSpotContact(
        id="contact-1", properties={"firstname": "John", "lastname": "Smith"}
    )

    result = await tools(contacts=[contact]).resolve_contact(
        "tenant-a", first_name="  JOHN ", last_name="  smITH  "
    )

    assert result.status == "found"
    assert result.contact == contact


@pytest.mark.asyncio
async def test_contact_matching_accepts_a_minor_typo():
    contact = HubSpotContact(
        id="contact-1", properties={"firstname": "John", "lastname": "Smith"}
    )

    result = await tools(contacts=[contact]).resolve_contact(
        "tenant-a", first_name="john", last_name="smth"
    )

    assert result.status == "found"
    assert result.contact == contact


@pytest.mark.asyncio
async def test_deal_matching_accepts_a_minor_typo_and_extra_spaces():
    deal = HubSpotDeal(id="deal-1", properties={"dealname": "Renewal Q3 Deal"})

    result = await tools(deals=[deal]).resolve_deal("tenant-a", "  Renewl   Q3  ")

    assert result.status == "found"
    assert result.deal == deal


@pytest.mark.asyncio
async def test_exact_match_takes_priority_over_fuzzy_contact_candidate():
    exact = HubSpotContact(
        id="contact-exact", properties={"firstname": "John", "lastname": "Smith"}
    )
    near = HubSpotContact(
        id="contact-near", properties={"firstname": "John", "lastname": "Smiths"}
    )

    result = await tools(contacts=[near, exact]).resolve_contact(
        "tenant-a", first_name="john", last_name="smith"
    )

    assert result.status == "found"
    assert result.contact == exact


@pytest.mark.asyncio
async def test_similarly_matched_contacts_are_ambiguous():
    first = HubSpotContact(
        id="contact-1", properties={"firstname": "John", "lastname": "Smith"}
    )
    second = HubSpotContact(
        id="contact-2", properties={"firstname": "John", "lastname": "Smith"}
    )

    result = await tools(contacts=[first, second]).resolve_contact(
        "tenant-a", first_name="john", last_name="smth"
    )

    assert result.status == "ambiguous"
    assert result.contact is None


@pytest.mark.asyncio
async def test_unrelated_company_contact_and_deal_names_are_not_matched():
    registry = tools(
        companies=[HubSpotCompany(id="company-1", properties={"name": "Demo AI Company 101"})],
        contacts=[
            HubSpotContact(
                id="contact-1", properties={"firstname": "John", "lastname": "Smith"}
            )
        ],
        deals=[HubSpotDeal(id="deal-1", properties={"dealname": "Renewal Q3 Deal"})],
    )

    company = await registry.resolve_company("tenant-a", "Unrelated Robotics")
    contact = await registry.resolve_contact(
        "tenant-a", first_name="Alice", last_name="Jones"
    )
    deal = await registry.resolve_deal("tenant-a", "Unrelated Robotics")

    assert company.status == "not_found"
    assert contact.status == "not_found"
    assert deal.status == "not_found"
