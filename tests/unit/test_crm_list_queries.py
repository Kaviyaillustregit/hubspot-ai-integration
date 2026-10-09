import asyncio

from app.agent.extraction import validate_extraction
from app.agent.operations import CRMOperations
from app.agent.schemas import AgentRequest, CRMIntentExtraction
from app.agent.service import AccountIntelligenceAgent
from app.agent.tools import HubSpotToolRegistry
from app.ai.service import AIService
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotCompany,
    HubSpotContact,
    HubSpotContactsPage,
    HubSpotDeal,
    HubSpotPipeline,
    HubSpotPipelineStage,
    HubSpotRecord,
)
from app.services.action_safety import ConfirmedAction


class PagedCompanies:
    def __init__(self, records: list[HubSpotCompany]) -> None:
        self.records = records
        self.cursors: list[str | None] = []

    async def list_companies(self, context, *, limit, after, properties):
        self.cursors.append(after)
        offset = int(after or "0")
        next_offset = offset + limit
        return HubSpotCompaniesPage(
            results=self.records[offset:next_offset],
            next_after=str(next_offset) if next_offset < len(self.records) else None,
        )


class PagedContacts:
    def __init__(self, records: list[HubSpotContact]) -> None:
        self.records = records
        self.cursors: list[str | None] = []

    async def list_contacts(self, context, *, limit, after, properties):
        self.cursors.append(after)
        offset = int(after or "0")
        next_offset = offset + limit
        return HubSpotContactsPage(
            results=self.records[offset:next_offset],
            next_after=str(next_offset) if next_offset < len(self.records) else None,
        )


async def test_company_and_contact_registry_listing_follows_every_cursor():
    companies = [HubSpotCompany(id=str(i), properties={"name": f"Company {i}"}) for i in range(205)]
    contacts = [
        HubSpotContact(id=str(i), properties={"firstname": f"Contact {i}"})
        for i in range(205)
    ]
    company_source = PagedCompanies(companies)
    contact_source = PagedContacts(contacts)
    registry = HubSpotToolRegistry(
        company_source, contact_source  # type: ignore[arg-type]
    )

    listed_companies = await registry.list_all_companies("tenant-a")
    listed_contacts = await registry.list_all_contacts("tenant-a")

    assert len(listed_companies) == len(listed_contacts) == 205
    assert company_source.cursors == [None, "100", "200"]
    assert contact_source.cursors == [None, "100", "200"]


class ListingTools:
    def __init__(self) -> None:
        self.company = HubSpotCompany(
            id="company-1",
            properties={
                "name": "Testing Corp",
                "hubspot_owner_id": "owner-1",
                "phone": "1234567890",
                "city": "Austin",
                "industry": "Technology",
                "numberofemployees": "1000",
                "lifecyclestage": "customer",
                "hs_lead_status": "IN_PROGRESS",
                "notes_last_contacted": "2026-09-30T10:00:00Z",
            },
        )
        self.contact = HubSpotContact(
            id="contact-1",
            properties={
                "firstname": "Kaviya",
                "lastname": "Smith",
                "hubspot_owner_id": "owner-2",
                "email": "kaviya@example.com",
                "phone": "1234567890",
                "associatedcompanyid": "company-1",
                "city": "Austin",
                "state": "Texas",
                "industry": "Technology",
                "lifecyclestage": "lead",
                "hs_lead_status": "NEW",
                "notes_last_contacted": "2026-10-01T12:00:00Z",
                "jobtitle": "Director",
                "hs_sub_role": "Engineering",
                "hs_seniority": "Director",
                "hs_linkedin_url": "https://linkedin.example/kaviya",
            },
        )

    async def list_all_companies(self, tenant_id):
        return [self.company]

    async def list_all_contacts(self, tenant_id):
        return [self.contact]

    async def search_companies(self, tenant_id, query):
        return [self.company]

    async def search_contacts(self, tenant_id, query):
        return [self.contact]

    async def get_company(self, tenant_id, company_id):
        assert company_id == self.company.id
        return self.company

    async def get_contact(self, tenant_id, contact_id):
        assert contact_id == self.contact.id
        return self.contact

    async def resolve_company(self, tenant_id, name):
        from app.agent.tools import CompanyResolution

        return CompanyResolution("found", self.company)

    async def resolve_contact(self, tenant_id, **kwargs):
        from app.agent.tools import ContactResolution

        return ContactResolution("found", self.contact)

    async def resolve_deal(self, tenant_id, name):
        from app.agent.tools import DealResolution
        from app.integrations.hubspot.models import HubSpotDeal

        return DealResolution(
            "found",
            HubSpotDeal(
                id="deal-1",
                properties={
                    "dealname": "Renewal",
                    "dealstage": "open",
                    "hubspot_owner_id": "owner-3",
                    "amount": "50000",
                    "hs_deal_stage_probability": "0.8",
                    "closedate": "2026-12-31",
                },
            ),
        )

    async def get_deal(self, tenant_id, deal_id):
        resolution = await self.resolve_deal(tenant_id, "")
        assert resolution.deal is not None
        return resolution.deal

    async def deal_pipelines(self, tenant_id):
        return [
            HubSpotPipeline(
                id="sales",
                label="Sales",
                stages=[HubSpotPipelineStage(id="open", label="Qualification")],
            )
        ]

    async def associated_records(self, tenant_id, *, from_type, from_id, to_type):
        if from_type == "companies" and to_type == "deals":
            return [HubSpotRecord(id="deal-1", properties={"dealname": "Renewal"})]
        if from_type == "contacts" and to_type == "companies":
            return [HubSpotRecord(id="company-1", properties={"name": "Testing Corp"})]
        if from_type == "contacts" and to_type == "deals":
            return [HubSpotRecord(id="deal-1", properties={"dealname": "Renewal"})]
        if from_type == "deals" and to_type == "companies":
            return [HubSpotRecord(id="company-1", properties={"name": "Testing Corp"})]
        return []

    async def hubspot_record_urls(self, tenant_id, records):
        object_type_ids = {"contacts": "0-1", "companies": "0-2", "deals": "0-3"}
        return [
            f"https://app.hubspot.com/contacts/42/record/{object_type_ids[object_type]}/{record_id}"
            for object_type, record_id in records
        ]


def request(message: str) -> AgentRequest:
    return AgentRequest(
        tenant_id="tenant-a",
        actor_id="user-1",
        message=message,
        request_id="request-1",
    )


async def query(query_name: str, message: str, *, tools=None, **fields):
    extraction = CRMIntentExtraction(
        intent="crm_question",
        query=query_name,
        confidence=0.95,
        **fields,
    )
    validated = validate_extraction(message, extraction)
    operations = CRMOperations(tools or ListingTools(), action_safety=None)  # type: ignore[arg-type]
    return await operations._answer_query(request(message), query_name, validated)  # noqa: SLF001


async def test_company_list_uses_table_fields_and_associated_deals():
    response = await query("company_list", "List all companies")

    assert response.result is not None
    table = response.result["table"]
    assert table["rows"] == [  # type: ignore[index]
        {
            "name": "Testing Corp",
            "owner": "owner-1",
            "phone": "1234567890",
            "city": "Austin",
            "industry": "Technology",
            "employees": "1000",
            "lifecycle": "customer",
            "lead_status": "IN_PROGRESS",
            "last_contacted": "2026-09-30",
            "associated_deals": "Renewal",
            "view_url": "https://app.hubspot.com/contacts/42/record/0-2/company-1",
        }
    ]


async def test_company_search_and_contact_list_return_structured_tables():
    company = await query(
        "company_search",
        "Find Testing Corp",
        company_name="Testing Corp",
    )
    contact = await query("contact_list", "List all contacts")

    assert company.result is not None and len(company.result["table"]["rows"]) == 1  # type: ignore[index]
    assert contact.result is not None
    row = contact.result["table"]["rows"][0]  # type: ignore[index]
    assert row["name"] == "Kaviya Smith"
    assert row["company"] == "Testing Corp"
    assert row["state"] == "Texas"
    assert row["job_title"] == "Director"
    assert row["job_sub_role"] == "Engineering"
    assert row["seniority"] == "Director"
    assert row["linkedin"] == "https://linkedin.example/kaviya"
    assert row["view_url"].endswith("/record/0-1/contact-1")


async def test_contact_enrichment_is_bounded_concurrent_and_preserves_row_order():
    class ConcurrentListingTools(ListingTools):
        def __init__(self):
            super().__init__()
            self.contacts = [
                HubSpotContact(
                    id=f"contact-{index}",
                    properties={"firstname": f"Contact {index}", "lastname": "Name"},
                )
                for index in range(12)
            ]
            self.active_associations = 0
            self.max_active_associations = 0

        async def list_all_contacts(self, tenant_id):
            return self.contacts

        async def associated_records(self, tenant_id, *, from_type, from_id, to_type):
            self.active_associations += 1
            self.max_active_associations = max(
                self.max_active_associations, self.active_associations
            )
            try:
                await asyncio.sleep(0.005)
            finally:
                self.active_associations -= 1
            if to_type == "companies":
                return [HubSpotRecord(id="company-1", properties={"name": "Testing Corp"})]
            return []

    tools = ConcurrentListingTools()
    response = await query("contact_list", "List all contacts", tools=tools)

    assert response.result is not None
    rows = response.result["table"]["rows"]  # type: ignore[index]
    assert [row["name"] for row in rows] == [
        f"Contact {index} Name" for index in range(12)
    ]
    assert 1 < tools.max_active_associations <= 16


async def test_deal_list_loads_pipeline_and_deals_concurrently():
    class ParallelDealTools(ListingTools):
        def __init__(self):
            super().__init__()
            self.active_loads = 0
            self.max_active_loads = 0

        async def _track_load(self):
            self.active_loads += 1
            self.max_active_loads = max(self.max_active_loads, self.active_loads)
            try:
                await asyncio.sleep(0.005)
            finally:
                self.active_loads -= 1

        async def deal_pipelines(self, tenant_id):
            await self._track_load()
            return await super().deal_pipelines(tenant_id)

        async def list_all_deals(self, tenant_id):
            await self._track_load()
            return [HubSpotDeal(id="deal-1", properties={"dealname": "Renewal"})]

    tools = ParallelDealTools()
    response = await query("all_deals", "Show all deals", tools=tools)

    assert response.status == "ok"
    assert tools.max_active_loads == 2


async def test_company_list_returns_requested_columns_in_order_and_marks_empty_values():
    tools = ListingTools()
    tools.company.properties["phone"] = None
    response = await query(
        "company_list",
        "Show all companies with phone, city, and number of employees",
        requested_fields=["phone", "city", "employees"],
        tools=tools,
    )

    assert response.result is not None
    table = response.result["table"]  # type: ignore[index]
    assert [column["key"] for column in table["columns"]] == [
        "phone",
        "city",
        "employees",
        "view_url",
    ]
    assert table["rows"][0]["phone"] == "—"


async def test_contact_list_returns_only_explicitly_requested_columns():
    response = await query(
        "contact_list",
        "List all contacts with email and job seniority",
        requested_fields=["email", "seniority"],
    )

    assert response.result is not None
    assert [column["key"] for column in response.result["table"]["columns"]] == [  # type: ignore[index]
        "email",
        "seniority",
        "view_url",
    ]


async def test_registry_builds_links_with_portal_and_hubspot_object_type_ids():
    class ContactService:
        async def get_hubspot_account_id(self, context):
            return "portal 42"

    registry = HubSpotToolRegistry(None, ContactService())  # type: ignore[arg-type]

    urls = await registry.hubspot_record_urls(
        "tenant-a",
        [("companies", "company-1"), ("contacts", "contact-1"), ("deals", "deal-1")],
    )

    assert urls == [
        "https://app.hubspot.com/contacts/portal%2042/record/0-2/company-1",
        "https://app.hubspot.com/contacts/portal%2042/record/0-1/contact-1",
        "https://app.hubspot.com/contacts/portal%2042/record/0-3/deal-1",
    ]


async def test_all_contacts_request_routes_to_existing_contact_list_query():
    class ContactListIntentProvider:
        async def generate_structured(self, **kwargs):
            return CRMIntentExtraction(
                intent="crm_question",
                query=None,
                confidence=0.95,
            )

    agent = AccountIntelligenceAgent(
        ListingTools(),  # type: ignore[arg-type]
        AIService(ContactListIntentProvider()),  # type: ignore[arg-type]
        action_safety=None,  # type: ignore[arg-type]
    )

    response = await agent.respond(request("Show me all contacts"))

    assert response.status == "ok"
    assert response.result is not None
    assert response.result["title"] == "Contacts"
    assert response.result["table"]["rows"][0]["name"] == "Kaviya Smith"  # type: ignore[index]


async def test_delete_company_by_name_resolves_and_requests_confirmation():
    class CompanyDeleteIntentProvider:
        async def generate_structured(self, **kwargs):
            return CRMIntentExtraction(
                intent="delete_company",
                confidence=0.95,
                company_name="Test Revenue Company",
            )

    class CompanyDeleteTools(ListingTools):
        def __init__(self):
            super().__init__()
            self.company = HubSpotCompany(
                id="company-revenue-1",
                properties={"name": "Test Revenue Company"},
            )
            self.resolved_names = []
            self.deleted_ids = []

        async def resolve_company(self, tenant_id, name):
            self.resolved_names.append(name)
            from app.agent.tools import CompanyResolution

            return CompanyResolution("found", self.company)

        async def delete_company(self, tenant_id, company_id):
            self.deleted_ids.append(company_id)

    class PendingActionSafety:
        async def create_pending_action(self, **kwargs):
            self.pending_action = kwargs
            return "action-company-delete"

        async def complete_action(self, **kwargs):
            self.completed_action = kwargs

    tools = CompanyDeleteTools()
    safety = PendingActionSafety()
    agent = AccountIntelligenceAgent(
        tools,  # type: ignore[arg-type]
        AIService(CompanyDeleteIntentProvider()),  # type: ignore[arg-type]
        action_safety=safety,  # type: ignore[arg-type]
    )

    response = await agent.respond(request("Delete the company named Test Revenue Company"))

    assert response.status == "pending_confirmation"
    assert tools.resolved_names == ["Test Revenue Company"]
    assert safety.pending_action["action_type"] == "delete_company"
    assert safety.pending_action["payload"]["company_id"] == "company-revenue-1"

    confirmed = ConfirmedAction(
        id="action-company-delete",
        tenant_id="tenant-a",
        actor_id="user-1",
        action_type="delete_company",
        resource_type="company",
        payload=safety.pending_action["payload"],
    )
    archived = await agent._operations.execute_confirmed_update(  # noqa: SLF001
        request("confirm action-company-delete"), confirmed
    )

    assert archived.status == "ok"
    assert tools.deleted_ids == ["company-revenue-1"]


async def test_contact_search_returns_matching_contact_table():
    response = await query(
        "contact_search",
        "Find contact Kaviya Smith",
        first_name="Kaviya",
        last_name="Smith",
    )

    assert response.result is not None
    assert response.result["table"]["rows"][0]["email"] == "kaviya@example.com"  # type: ignore[index]


async def test_company_details_returns_fields_and_associated_deals():
    response = await query(
        "company_details",
        "Show Testing Corp",
        company_name="Testing Corp",
    )

    assert response.result is not None
    row = response.result["table"]["rows"][0]  # type: ignore[index]
    assert row["industry"] == "Technology"
    assert row["associated_deals"] == "Renewal"


async def test_contact_details_includes_associated_company_and_deals():
    response = await query(
        "contact_details",
        "Show contact Kaviya Smith",
        first_name="Kaviya",
        last_name="Smith",
    )

    assert response.result is not None
    row = response.result["table"]["rows"][0]  # type: ignore[index]
    assert row["company"] == "Testing Corp"
    assert row["associated_deals"] == "Renewal"


async def test_deal_details_returns_requested_fields_in_the_table():
    response = await query(
        "deal_details",
        "Show Renewal deal",
        deal_name="Renewal",
    )

    assert response.result is not None
    row = response.result["table"]["rows"][0]  # type: ignore[index]
    assert row == {
        "name": "Renewal",
        "company": "Testing Corp",
        "owner": "owner-3",
        "stage": "Qualification",
        "amount": "50,000",
        "probability": "80%",
        "close_date": "2026-12-31",
        "view_url": "https://app.hubspot.com/contacts/42/record/0-3/deal-1",
    }
