import pytest

from app.agent.crm_intelligence import CRMQueryPlan
from app.agent.crm_query_intelligence import UnsupportedCRMField
from app.agent.schemas import AgentRequest, CRMIntentExtraction
from app.agent.service import AccountIntelligenceAgent, _format_batch2_slack_table
from app.ai.provider import AIProvider
from app.ai.service import AIService
from app.integrations.errors import IntegrationPermissionError
from app.integrations.hubspot.models import HubSpotCompany, HubSpotContact, HubSpotRecord
from app.integrations.hubspot.properties import HubSpotPropertyDefinition


class QueryProvider(AIProvider):
    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(intent="crm_question", query="contact_list", confidence=1.0)
        if prompt_name == "crm-intelligence/v1":
            return CRMQueryPlan(
                object="contact",
                properties=["First name", "Customer score", "owner"],
                filters=[{"field": "Customer score", "operator": "gte", "value": "80"}],
                sort=[{"field": "Customer score", "direction": "desc"}],
                limit=5,
            )
        raise AssertionError(prompt_name)


class QueryTools:
    property_discovery_available = True

    async def discover_properties(self, tenant_id, object_type):
        assert tenant_id == "tenant-a"
        assert object_type == "contacts"
        return [
            HubSpotPropertyDefinition(name="firstname", label="First name"),
            HubSpotPropertyDefinition(name="custom_score", label="Customer score", type="number"),
            HubSpotPropertyDefinition(
                name="hubspot_owner_id", label="HubSpot owner", type="number"
            ),
        ]

    async def list_all_contacts(self, tenant_id, *, properties):
        assert "custom_score" in properties
        return [
            HubSpotContact(
                id="c1",
                properties={"firstname": "Ava", "custom_score": "95", "hubspot_owner_id": "7"},
            ),
            HubSpotContact(
                id="c2",
                properties={"firstname": "Ben", "custom_score": "45", "hubspot_owner_id": "8"},
            ),
        ]

    async def list_owners(self, tenant_id):
        return [type("Owner", (), {"id": "7", "display_name": "Riley Smith"})()]

    async def hubspot_record_urls(self, tenant_id, records):
        return [f"https://hubspot.test/{record_id}" for _, record_id in records]


@pytest.mark.asyncio
async def test_complete_agent_path_uses_custom_property_catalog_and_returns_linked_slack_table():
    agent = AccountIntelligenceAgent(QueryTools(), AIService(QueryProvider()), object())
    request = AgentRequest(
        tenant_id="tenant-a",
        actor_id="user-a",
        message="Show contacts with Customer score at least 80",
        request_id="batch2-1",
    )

    response = await agent.respond(request)

    assert response.status == "ok"
    assert "rows" in response.result, response.model_dump()
    assert response.result["rows"] == [
        {
            "id": "c1",
            "firstname": "Ava",
            "custom_score": "95",
            "hubspot_owner_id": "Riley Smith",
            "view_url": "https://hubspot.test/c1",
        }
    ]
    assert response.result["table"]["columns"][-1] == {
        "key": "view_url",
        "label": "View in HubSpot",
    }


class CompanyProvider(AIProvider):
    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(intent="crm_question", query="company_list", confidence=1.0)
        if prompt_name == "crm-intelligence/v1":
            return CRMQueryPlan(
                object="company",
                properties=["Company name", "Employees", "owner"],
                filters=[{"field": "Employees", "operator": "gte", "value": "50"}],
                limit=10,
            )
        raise AssertionError(prompt_name)


class CompanyTools(QueryTools):
    async def discover_properties(self, tenant_id, object_type):
        assert tenant_id == "tenant-company"
        assert object_type == "companies"
        return [
            HubSpotPropertyDefinition(name="name", label="Company name"),
            HubSpotPropertyDefinition(name="numberofemployees", label="Employees", type="number"),
            HubSpotPropertyDefinition(
                name="hubspot_owner_id", label="HubSpot owner", type="number"
            ),
        ]

    async def list_all_companies(self, tenant_id, *, properties):
        assert tenant_id == "tenant-company"
        assert "numberofemployees" in properties
        return [
            HubSpotCompany(
                id="co-1",
                properties={
                    "name": "Northwind",
                    "numberofemployees": "120",
                    "hubspot_owner_id": "9",
                },
            )
        ]

    async def list_owners(self, tenant_id):
        assert tenant_id == "tenant-company"
        raise IntegrationPermissionError("owner-read scope is not granted")

    async def hubspot_record_urls(self, tenant_id, records):
        assert tenant_id == "tenant-company"
        return [f"https://hubspot.test/{record_id}" for _, record_id in records]


@pytest.mark.asyncio
async def test_company_route_works_when_owner_lookup_lacks_permission():
    agent = AccountIntelligenceAgent(CompanyTools(), AIService(CompanyProvider()), object())
    request = AgentRequest(
        tenant_id="tenant-company",
        actor_id="user-company",
        message="Show companies with at least 50 employees",
        request_id="batch2-company",
    )

    response = await agent.respond(request)

    assert response.status == "ok"
    assert response.result["rows"] == [
        {
            "id": "co-1",
            "name": "Northwind",
            "numberofemployees": "120",
            "hubspot_owner_id": "9",
            "view_url": "https://hubspot.test/co-1",
        }
    ]


class IndustryProvider(CompanyProvider):
    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(intent="crm_question", query="company_list", confidence=1.0)
        if prompt_name == "crm-intelligence/v1":
            return CRMQueryPlan(object="company", properties=["industry"])
        raise AssertionError(prompt_name)


class IndustryTools(CompanyTools):
    async def discover_properties(self, tenant_id, object_type):
        return [
            HubSpotPropertyDefinition(
                name="industry", label="Industry", type="enumeration",
                options=[{"value": "COMPUTER_GAMES", "label": "Computer Games"}],
            ),
            HubSpotPropertyDefinition(name="name", label="Company name"),
        ]

    async def list_all_companies(self, tenant_id, *, properties):
        return [
            HubSpotCompany(
                id="co-1",
                properties={"name": "Northwind", "industry": "COMPUTER_GAMES"},
            )
        ]

    async def list_owners(self, tenant_id):
        return []

    async def hubspot_record_urls(self, tenant_id, records):
        return ["https://app.hubspot.com/contacts/42/record/0-2/co-1"]


@pytest.mark.asyncio
async def test_company_enum_values_use_metadata_option_labels():
    agent = AccountIntelligenceAgent(IndustryTools(), AIService(IndustryProvider()), object())
    response = await agent.respond(AgentRequest(
        tenant_id="tenant-industry", actor_id="user", message="Show company industry",
        request_id="industry-enum",
    ))

    assert response.result["rows"][0]["industry"] == "Computer Games"
    assert response.result["rows"][0]["industry_raw"] == "COMPUTER_GAMES"


class ContactAssociationProvider(QueryProvider):
    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intent/v2":
            return CRMIntentExtraction(intent="crm_question", query="contact_list", confidence=1.0)
        if prompt_name == "crm-intelligence/v1":
            return CRMQueryPlan(object="contact", properties=["associatedcompanyid"])
        raise AssertionError(prompt_name)


class ContactAssociationTools(QueryTools):
    async def discover_properties(self, tenant_id, object_type):
        return [
            HubSpotPropertyDefinition(name="firstname", label="First name"),
            HubSpotPropertyDefinition(name="lastname", label="Last name"),
        ]

    async def list_all_contacts(self, tenant_id, *, properties):
        assert "associatedcompanyid" not in properties
        return [HubSpotContact(id="c-1", properties={"firstname": "Ava", "lastname": "Chen"})]

    async def associated_records(self, tenant_id, *, from_type, from_id, to_type):
        assert (from_type, from_id, to_type) == ("contacts", "c-1", "companies")
        return [HubSpotRecord(id="co-1", properties={"name": "Northwind"})]

    async def hubspot_record_urls(self, tenant_id, records):
        return [
            f"https://app.hubspot.com/contacts/42/record/0-2/{record_id}"
            for _, record_id in records
        ]

    async def list_owners(self, tenant_id):
        return []


@pytest.mark.asyncio
async def test_contact_associated_company_is_resolved_as_association_not_property():
    agent = AccountIntelligenceAgent(
        ContactAssociationTools(), AIService(ContactAssociationProvider()), object()
    )
    response = await agent.respond(AgentRequest(
        tenant_id="tenant-a", actor_id="user",
        message="Show contacts and their associated company", request_id="contact-assoc",
    ))

    assert response.status == "ok"
    assert response.result["rows"][0]["associated_company"] == "Northwind"
    assert response.result["rows"][0]["associated_company_links"] == [{
        "name": "Northwind", "url": "https://app.hubspot.com/contacts/42/record/0-2/co-1",
    }]


@pytest.mark.parametrize(
    ("association_result", "expected"),
    [("empty", "No associated companies"), ("error", "Could not verify associated companies")],
)
@pytest.mark.asyncio
async def test_contact_association_empty_and_failure_are_distinguished(
    association_result, expected
):
    class AssociationTools(ContactAssociationTools):
        async def associated_records(self, tenant_id, *, from_type, from_id, to_type):
            if association_result == "error":
                raise IntegrationPermissionError("association read unavailable")
            return []

    agent = AccountIntelligenceAgent(
        AssociationTools(), AIService(ContactAssociationProvider()), object()
    )
    response = await agent.respond(AgentRequest(
        tenant_id="tenant-a", actor_id="user",
        message="Show contacts and their associated company", request_id="contact-assoc-state",
    ))
    assert response.result["rows"][0]["associated_company"] == expected


class StandardQueryProvider(AIProvider):
    def __init__(self, object_type, field):
        self.object_type = object_type
        self.field = field

    async def generate_structured(self, *, prompt_name, variables, output_schema):
        if prompt_name == "crm-intelligence/v1":
            return CRMQueryPlan(object=self.object_type, properties=[self.field])
        raise AssertionError(prompt_name)


class MissingMetadataTools:
    property_discovery_available = True

    def __init__(self, object_type, field):
        self.object_type = object_type
        self.field = field

    async def discover_properties(self, tenant_id, object_type):
        assert tenant_id == "tenant-standard"
        assert object_type == ("contacts" if self.object_type == "contact" else "companies")
        scope = f"crm.schemas.{object_type}.read"
        raise IntegrationPermissionError("scope not granted", scope=scope)

    async def list_all_contacts(self, tenant_id, *, properties):
        assert tenant_id == "tenant-standard"
        assert "firstname" in properties
        return [HubSpotContact(id="contact-1", properties={"firstname": "Ava"})]

    async def list_all_companies(self, tenant_id, *, properties):
        assert tenant_id == "tenant-standard"
        assert "name" in properties
        return [HubSpotCompany(id="company-1", properties={"name": "Northwind"})]

    async def list_owners(self, tenant_id):
        return []

    async def hubspot_record_urls(self, tenant_id, records):
        return [f"https://hubspot.test/{record_id}" for _, record_id in records]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("object_type", "field", "expected_property", "record_id"),
    [
        ("contact", "First name", "firstname", "contact-1"),
        ("company", "Company name", "name", "company-1"),
    ],
)
async def test_standard_queries_fall_back_when_property_metadata_scope_is_missing(
    object_type, field, expected_property, record_id
):
    tools = MissingMetadataTools(object_type, field)
    agent = AccountIntelligenceAgent(
        tools, AIService(StandardQueryProvider(object_type, field)), object()
    )
    request = AgentRequest(
        tenant_id="tenant-standard",
        actor_id="user-standard",
        message=f"Show {object_type}s",
        request_id="standard-query",
    )

    response = await agent._run_generic_crm_query(
        request, CRMQueryPlan(object=object_type, properties=[field])
    )

    assert response.status == "ok"
    assert response.result["rows"][0]["id"] == record_id
    assert expected_property in response.result["rows"][0]
    assert response.tools_used[0] == "standard_properties_fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("object_type", "field"),
    [("contact", "Customer score"), ("company", "Custom segment")],
)
async def test_custom_property_query_explains_missing_metadata_scope(object_type, field):
    tools = MissingMetadataTools(object_type, field)
    agent = AccountIntelligenceAgent(
        tools, AIService(StandardQueryProvider(object_type, field)), object()
    )
    request = AgentRequest(
        tenant_id="tenant-standard",
        actor_id="user-standard",
        message=f"Show {object_type}s with {field}",
        request_id="custom-query",
    )

    property_scope_object = "companies" if object_type == "company" else "contacts"
    with pytest.raises(
        UnsupportedCRMField, match=f"crm.schemas.{property_scope_object}.read"
    ):
        await agent._run_generic_crm_query(
            request, CRMQueryPlan(object=object_type, properties=[field])
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("object_type", "message", "properties", "record"),
    [
        (
            "company",
            "Show me 5 companies with company name, industry, city, and number of employees. "
            "Include every requested field even if the value is empty, and display "
            "'Not available' for missing values.",
            ["name", "industry", "city", "numberofemployees"],
            HubSpotCompany(
                id="co-final-1",
                properties={
                    "name": "Northwind",
                    "industry": "Software",
                    "city": "",
                    "numberofemployees": "120",
                },
            ),
        ),
        (
            "contact",
            "Show 5 contacts with first name, email, and city. Include every requested field "
            "even if empty, and display 'Not available' for missing values.",
            ["firstname", "email", "city"],
            HubSpotContact(
                id="contact-final-1",
                properties={"firstname": "Ava", "email": None, "city": "Pune"},
            ),
        ),
    ],
)
async def test_final_batch2_slack_text_renders_all_explicit_fields_and_links(
    object_type, message, properties, record
):
    class Provider(AIProvider):
        async def generate_structured(self, *, prompt_name, variables, output_schema):
            if prompt_name == "crm-intelligence/v1":
                # Deliberately omit requested columns to prove deterministic field grounding.
                return CRMQueryPlan(
                    object=object_type,
                    properties=["name" if object_type == "company" else "firstname"],
                    limit=5,
                )
            raise AssertionError(prompt_name)

    class Tools:
        property_discovery_available = True

        async def discover_properties(self, tenant_id, object_name):
            assert tenant_id == "tenant-final"
            assert object_name == ("companies" if object_type == "company" else "contacts")
            definitions = {
                "name": ("Company name", "string"),
                "industry": ("Industry", "string"),
                "city": ("City", "string"),
                "numberofemployees": ("Number of employees", "number"),
                "firstname": ("First name", "string"),
                "email": ("Email", "string"),
            }
            return [
                HubSpotPropertyDefinition(name=name, label=label, type=kind)
                for name, (label, kind) in definitions.items()
            ]

        async def list_all_companies(self, tenant_id, *, properties):
            assert tenant_id == "tenant-final"
            assert set(properties).issuperset(properties_expected)
            return [record]

        async def list_all_contacts(self, tenant_id, *, properties):
            assert tenant_id == "tenant-final"
            assert set(properties).issuperset(properties_expected)
            return [record]

        async def list_owners(self, tenant_id):
            return []

        async def hubspot_record_urls(self, tenant_id, records):
            return [f"https://app.hubspot.com/records/{record_id}" for _, record_id in records]

    properties_expected = set(properties)
    agent = AccountIntelligenceAgent(Tools(), AIService(Provider()), object())
    request = AgentRequest(
        tenant_id="tenant-final",
        actor_id="user-final",
        message=message,
        request_id="batch2-final-response",
    )

    response = await agent._run_generic_crm_query(
        request,
        CRMQueryPlan(
            object=object_type,
            properties=["name" if object_type == "company" else "firstname"],
        ),
    )

    assert response.status == "ok"
    assert response.result["plan"]["properties"] == properties
    expected_headers = (
        ["Company name", "Industry", "City", "Number of employees"]
        if object_type == "company"
        else ["First name", "Email", "City"]
    )
    for header in expected_headers:
        assert f"*{header}:*" in response.text
    assert "Not available" in response.text
    assert "<https://app.hubspot.com/records/" in response.text
    if object_type == "company":
        assert "Northwind" in response.text
        assert "Software" in response.text
        assert "120" in response.text
        assert "Pune" not in response.text
    else:
        assert "Ava" in response.text
        assert "Pune" in response.text


def test_batch2_slack_table_formatter_respects_message_limit():
    text = _format_batch2_slack_table(
        "Found 3 companies. Showing 3 records.",
        [
            {"key": "name", "label": "Company name"},
            {"key": "view_url", "label": "View in HubSpot"},
        ],
        [
            {"name": "A" * 80, "view_url": "https://hubspot.test/1"},
            {"name": "B" * 80, "view_url": "https://hubspot.test/2"},
            {"name": "C" * 80, "view_url": "https://hubspot.test/3"},
        ],
        max_length=250,
    )

    assert len(text) <= 250
    assert "Company name" in text
    assert "Open in HubSpot" in text
    assert "Additional rows omitted" in text
