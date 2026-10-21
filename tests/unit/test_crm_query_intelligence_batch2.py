from datetime import date
from types import SimpleNamespace

import pytest

from app.agent import crm_query_intelligence as query_intelligence
from app.agent.crm_intelligence import (
    CRMIntelligenceAggregation,
    CRMIntelligenceFilter,
    CRMIntelligenceSort,
    CRMQueryPlan,
)
from app.agent.crm_query_intelligence import (
    UnsupportedCRMField,
    execute_record_query,
    resolve_catalog_fields,
    resolve_date_bounds,
)
from app.integrations.hubspot.properties import HubSpotPropertyDefinition

CATALOG = [
    HubSpotPropertyDefinition(name="firstname", label="First name"),
    HubSpotPropertyDefinition(name="lastname", label="Last name"),
    HubSpotPropertyDefinition(name="email", label="Email"),
    HubSpotPropertyDefinition(name="hubspot_owner_id", label="HubSpot owner", type="number"),
    HubSpotPropertyDefinition(name="lifecyclestage", label="Lifecycle stage", type="enumeration"),
    HubSpotPropertyDefinition(name="custom_score", label="Customer score", type="number"),
    HubSpotPropertyDefinition(name="createdate", label="Create date", type="date"),
]
COMPANY_CATALOG = [
    HubSpotPropertyDefinition(name="name", label="Company name"),
    HubSpotPropertyDefinition(name="industry", label="Industry"),
    HubSpotPropertyDefinition(name="numberofemployees", label="Employees", type="number"),
    HubSpotPropertyDefinition(name="createdate", label="Create date", type="date"),
    HubSpotPropertyDefinition(name="custom_segment", label="Customer segment"),
    HubSpotPropertyDefinition(name="hubspot_owner_id", label="HubSpot owner", type="number"),
]


@pytest.fixture
def records():
    return [
        SimpleNamespace(
            id="1",
            properties={
                "firstname": "Ava",
                "lastname": "Jones",
                "email": "ava@example.test",
                "hubspot_owner_id": "10",
                "lifecyclestage": "customer",
                "custom_score": "90",
                "createdate": "2026-02-01",
            },
        ),
        SimpleNamespace(
            id="2",
            properties={
                "firstname": "Ben",
                "lastname": "Lee",
                "email": "ben@example.test",
                "hubspot_owner_id": "20",
                "lifecyclestage": "lead",
                "custom_score": "50",
                "createdate": "2026-03-01",
            },
        ),
        SimpleNamespace(
            id="3",
            properties={
                "firstname": "Cal",
                "lastname": "Diaz",
                "email": "cal@example.test",
                "hubspot_owner_id": "10",
                "lifecyclestage": "customer",
                "custom_score": "70",
                "createdate": "2026-04-01",
            },
        ),
    ]


@pytest.fixture
def company_records():
    return [
        SimpleNamespace(
            id="co-1",
            properties={
                "name": "Apex",
                "industry": "Software",
                "numberofemployees": "90",
                "createdate": "2026-02-01",
                "custom_segment": "enterprise",
                "hubspot_owner_id": "10",
            },
        ),
        SimpleNamespace(
            id="co-2",
            properties={
                "name": "Beacon",
                "industry": "Finance",
                "numberofemployees": "50",
                "createdate": "2026-03-01",
                "custom_segment": "growth",
                "hubspot_owner_id": "20",
            },
        ),
        SimpleNamespace(
            id="co-3",
            properties={
                "name": "Cedar",
                "industry": "Software",
                "numberofemployees": "70",
                "createdate": "2026-04-01",
                "custom_segment": "growth",
                "hubspot_owner_id": "10",
            },
        ),
    ]


def test_property_catalog_resolves_standard_custom_names_and_labels():
    plan = CRMQueryPlan(
        object="contact",
        properties=["First name", "Customer score"],
        filters=[CRMIntelligenceFilter(field="Lifecycle stage", value="customer")],
    )

    resolved = resolve_catalog_fields(plan, CATALOG)

    assert resolved.properties == ["firstname", "custom_score"]
    assert resolved.filters[0].field == "lifecyclestage"


def test_contact_all_catalog_properties_are_returned_when_requested(records):
    plan = CRMQueryPlan(
        object="contact",
        properties=[item.name for item in CATALOG],
    )

    result = execute_record_query(plan, records, CATALOG)

    assert result["matched_records"] == 3
    assert set(item["key"] for item in result["columns"]) == {
        item.name for item in CATALOG
    }
    assert "custom_score" in result["rows"][0]


@pytest.mark.parametrize(
    ("filters", "expected_id"),
    [
        (
            [
                {"field": "firstname", "value": "Ava"},
                {"field": "lastname", "value": "Jones"},
            ],
            "1",
        ),
        ([{"field": "record ID", "value": "2"}], "2"),
    ],
)
def test_contact_retrieve_by_full_name_or_record_id(records, filters, expected_id):
    result = execute_record_query(
        CRMQueryPlan(object="contact", properties=["firstname"], filters=filters),
        records,
        CATALOG,
    )

    assert [row["id"] for row in result["rows"]] == [expected_id]


def test_unknown_field_returns_actionable_suggestion():
    plan = CRMQueryPlan(object="contact", properties=["fristname"])

    with pytest.raises(UnsupportedCRMField, match="firstname"):
        resolve_catalog_fields(plan, CATALOG)


def test_custom_numeric_filter_sort_and_page_are_applied(records):
    plan = CRMQueryPlan(
        object="contact",
        properties=["firstname", "custom_score"],
        filters=[CRMIntelligenceFilter(field="custom_score", operator="gte", value="70")],
        sort=[CRMIntelligenceSort(field="custom_score", direction="desc")],
        limit=1,
        offset=1,
    )

    result = execute_record_query(plan, records, CATALOG)

    assert result["matched_records"] == 2
    assert result["rows"] == [{"id": "3", "firstname": "Cal", "custom_score": "70"}]
    assert result["has_more"] is False


def test_date_range_grouping_and_aggregation_use_real_matching_records(records):
    plan = CRMQueryPlan(
        object="contact",
        date_range={"start": "2026-03-01", "end": "2026-05-01", "field": "createdate"},
        group_by="lifecyclestage",
        aggregations=[CRMIntelligenceAggregation(operation="avg", field="custom_score")],
    )

    result = execute_record_query(plan, records, CATALOG)

    assert result["matched_records"] == 2
    assert {row["group"]: row["count"] for row in result["rows"]} == {"customer": 1, "lead": 1}
    assert result["columns"]


def test_aliases_and_owner_fields_validate_against_live_catalog():
    plan = CRMQueryPlan(object="contact", properties=["owner", "email"])

    assert resolve_catalog_fields(plan, CATALOG).properties == ["hubspot_owner_id", "email"]


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("today", ("2026-10-19", "2026-10-20")),
        ("yesterday", ("2026-10-18", "2026-10-19")),
        ("this week", ("2026-10-19", "2026-10-26")),
        ("last week", ("2026-10-12", "2026-10-19")),
        ("this month", ("2026-10-01", "2026-11-01")),
        ("last month", ("2026-09-01", "2026-10-01")),
        ("this quarter", ("2026-10-01", "2027-01-01")),
        ("last quarter", ("2026-07-01", "2026-10-01")),
        ("this year", ("2026-01-01", "2027-01-01")),
        ("last year", ("2025-01-01", "2026-01-01")),
    ],
)
def test_relative_date_bounds_cover_supported_calendar_periods(relative, expected):
    from datetime import date

    plan = CRMQueryPlan(
        object="contact",
        date_range={"relative": relative, "field": "createdate"},
    )

    assert (
        tuple(
            item.isoformat()
            for item in resolve_date_bounds(plan.date_range, today=date(2026, 10, 19))
        )
        == expected
    )


def test_relative_date_range_uses_plan_timezone_for_datetime_properties(records, monkeypatch):
    monkeypatch.setattr(
        query_intelligence,
        "_relative_date_range",
        lambda relative, *, today, timezone: ("2026-10-19", "2026-10-20"),
    )

    records[0].properties["createdate"] = "2026-10-19T01:00:00Z"
    records[1].properties["createdate"] = "2026-10-20T01:00:00Z"
    records[2].properties["createdate"] = "not-a-date"
    plan = CRMQueryPlan(
        object="contact",
        properties=["firstname"],
        date_range={
            "relative": "today",
            "field": "createdate",
            "timezone": "America/Los_Angeles",
        },
    )

    result = execute_record_query(plan, records, CATALOG)

    assert [row["firstname"] for row in result["rows"]] == ["Ben"]
    assert resolve_date_bounds(plan.date_range, today=date(2026, 10, 19)) == (
        date(2026, 10, 19),
        date(2026, 10, 20),
    )

    records[0].properties["createdate"] = "2026-10-19"
    records[1].properties["createdate"] = "2026-10-18"
    assert execute_record_query(plan, records, CATALOG)["rows"] == [{"id": "1", "firstname": "Ava"}]


@pytest.mark.parametrize(
    ("operator", "expected"),
    [
        ("eq", ["Cal"]),
        ("neq", ["Ava"]),
        ("contains", ["Cal"]),
        ("in", ["Ava", "Cal"]),
        ("gt", ["Ava"]),
        ("gte", ["Ava", "Cal"]),
        ("lt", []),
        ("lte", ["Cal"]),
    ],
)
def test_filter_operators_are_typed_and_missing_values_are_safe(records, operator, expected):
    plan = CRMQueryPlan(
        object="contact",
        properties=["firstname"],
        filters=[
            CRMIntelligenceFilter(
                field="custom_score",
                operator=operator,
                value="70,90" if operator == "in" else "70",
            )
        ],
    )
    records[1].properties["custom_score"] = "malformed"
    records[2].properties["custom_score"] = "70"

    result = execute_record_query(plan, records, CATALOG)

    assert [row["firstname"] for row in result["rows"]] == expected


@pytest.mark.parametrize(
    "direction,expected", [("asc", ["Cal", "Ava", "Ben"]), ("desc", ["Ava", "Cal", "Ben"])]
)
def test_sort_directions_and_missing_values(records, direction, expected):
    records[1].properties["custom_score"] = None
    plan = CRMQueryPlan(
        object="contact",
        properties=["firstname"],
        sort=[{"field": "custom_score", "direction": direction}],
    )

    result = execute_record_query(plan, records, CATALOG)

    assert [row["firstname"] for row in result["rows"]] == expected


def test_multiple_sort_keys_are_stable_and_pagination_reports_more(records):
    records[0].properties["lifecyclestage"] = "lead"
    records[1].properties["lifecyclestage"] = "lead"
    records[2].properties["lifecyclestage"] = "customer"
    plan = CRMQueryPlan(
        object="contact",
        properties=["firstname"],
        sort=[
            {"field": "lifecyclestage", "direction": "asc"},
            {"field": "custom_score", "direction": "desc"},
        ],
        limit=2,
        offset=0,
    )

    result = execute_record_query(plan, records, CATALOG)

    assert result["rows"] == [
        {"id": "3", "firstname": "Cal"},
        {"id": "1", "firstname": "Ava"},
    ]
    assert result["has_more"] is True
    assert result["offset"] == 0


@pytest.mark.parametrize(
    ("operation", "expected"),
    [("count", 3), ("sum", "210"), ("avg", "70"), ("min", "50"), ("max", "90")],
)
def test_aggregations_cover_all_supported_operations(records, operation, expected):
    plan = CRMQueryPlan(
        object="contact",
        aggregations=[{"operation": operation, "field": "custom_score"}],
    )

    result = execute_record_query(plan, records, CATALOG)

    assert result["rows"][0][f"{operation}_custom_score"] == expected


def test_unknown_field_fails_with_suggestion_instead_of_returning_unfiltered_data():
    with pytest.raises(UnsupportedCRMField, match="Did you mean"):
        execute_record_query(
            CRMQueryPlan(object="contact", filters=[{"field": "fristname", "value": "Ava"}]),
            [],
            CATALOG,
        )


def test_empty_contains_and_non_numeric_aggregation_do_not_match_or_crash(records):
    records[0].properties["industry"] = "Software"
    records[1].properties["industry"] = None
    catalog = [*CATALOG, HubSpotPropertyDefinition(name="industry", label="Industry")]
    contains_result = execute_record_query(
        CRMQueryPlan(
            object="contact",
            properties=["firstname"],
            filters=[{"field": "industry", "operator": "contains", "value": ""}],
        ),
        records,
        catalog,
    )
    aggregation_result = execute_record_query(
        CRMQueryPlan(
            object="contact",
            aggregations=[{"operation": "sum", "field": "industry"}],
        ),
        records,
        catalog,
    )

    assert contains_result["rows"] == [{"id": "1", "firstname": "Ava"}]
    assert aggregation_result["rows"][0]["sum_industry"] is None


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("Company name", "name"),
        ("Employees", "numberofemployees"),
        ("Customer segment", "custom_segment"),
    ],
)
def test_company_fields_resolve_standard_and_custom_catalog_labels(field, expected):
    plan = CRMQueryPlan(object="company", properties=[field])

    assert resolve_catalog_fields(plan, COMPANY_CATALOG).properties == [expected]


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("name", "Beacon", ["co-2"]),
        ("id", "co-2", ["co-2"]),
        ("record ID", "co-1", ["co-1"]),
    ],
)
def test_company_retrieve_by_name_or_id(company_records, field, value, expected):
    result = execute_record_query(
        CRMQueryPlan(
            object="company",
            properties=["Company name"],
            filters=[{"field": field, "operator": "eq", "value": value}],
        ),
        company_records,
        COMPANY_CATALOG,
    )

    assert [row["id"] for row in result["rows"]] == expected


@pytest.mark.parametrize(
    ("operator", "expected"),
    [
        ("eq", ["Cedar"]),
        ("neq", ["Apex"]),
        ("contains", ["Cedar"]),
        ("in", ["Apex", "Cedar"]),
        ("gt", ["Apex"]),
        ("gte", ["Apex", "Cedar"]),
        ("lt", []),
        ("lte", ["Cedar"]),
    ],
)
def test_company_filter_operators_handle_values_and_malformed_data(
    company_records, operator, expected
):
    company_records[1].properties["numberofemployees"] = "malformed"
    result = execute_record_query(
        CRMQueryPlan(
            object="company",
            properties=["name"],
            filters=[
                {
                    "field": "numberofemployees",
                    "operator": operator,
                    "value": "70,90" if operator == "in" else "70",
                }
            ],
        ),
        company_records,
        COMPANY_CATALOG,
    )

    assert [row["name"] for row in result["rows"]] == expected


@pytest.mark.parametrize(
    ("direction", "expected"),
    [("asc", ["Cedar", "Apex", "Beacon"]), ("desc", ["Apex", "Cedar", "Beacon"])],
)
def test_company_sort_direction_and_offset_pagination(company_records, direction, expected):
    company_records[1].properties["numberofemployees"] = None
    result = execute_record_query(
        CRMQueryPlan(
            object="company",
            properties=["name"],
            sort=[{"field": "numberofemployees", "direction": direction}],
            limit=1,
            offset=1,
        ),
        company_records,
        COMPANY_CATALOG,
    )

    assert result["matched_records"] == 3
    assert [row["name"] for row in result["rows"]] == [expected[1]]
    assert result["has_more"] is True
    assert result["offset"] == 1


def test_company_multiple_sort_keys_and_all_catalog_fields(company_records):
    plan = CRMQueryPlan(
        object="company",
        properties=[item.name for item in COMPANY_CATALOG],
        sort=[
            {"field": "industry", "direction": "asc"},
            {"field": "numberofemployees", "direction": "desc"},
        ],
    )

    result = execute_record_query(plan, company_records, COMPANY_CATALOG)

    assert [row["id"] for row in result["rows"]] == ["co-2", "co-1", "co-3"]
    assert "custom_segment" in result["rows"][0]
    assert len(result["columns"]) == len(COMPANY_CATALOG)


@pytest.mark.parametrize(
    ("operation", "expected"),
    [("count", 3), ("sum", "210"), ("avg", "70"), ("min", "50"), ("max", "90")],
)
def test_company_aggregations_cover_all_supported_operations(
    company_records, operation, expected
):
    result = execute_record_query(
        CRMQueryPlan(
            object="company",
            aggregations=[{"operation": operation, "field": "numberofemployees"}],
        ),
        company_records,
        COMPANY_CATALOG,
    )

    assert result["rows"][0][f"{operation}_numberofemployees"] == expected


def test_company_grouped_aggregation_and_relative_date_filter(company_records, monkeypatch):
    monkeypatch.setattr(
        query_intelligence,
        "_relative_date_range",
        lambda relative, *, today, timezone: ("2026-03-01", "2026-05-01"),
    )
    result = execute_record_query(
        CRMQueryPlan(
            object="company",
            date_range={"relative": "this month", "field": "createdate"},
            group_by="industry",
            aggregations=[{"operation": "avg", "field": "numberofemployees"}],
        ),
        company_records,
        COMPANY_CATALOG,
    )

    assert result["matched_records"] == 2
    assert {row["group"]: row["count"] for row in result["rows"]} == {
        "Finance": 1,
        "Software": 1,
    }
    assert {row["group"]: row["avg_numberofemployees"] for row in result["rows"]} == {
        "Finance": "50",
        "Software": "70",
    }


def test_company_invalid_field_and_empty_results_are_safe(company_records):
    with pytest.raises(UnsupportedCRMField):
        execute_record_query(
            CRMQueryPlan(object="company", filters=[{"field": "custmer segment", "value": "x"}]),
            company_records,
            COMPANY_CATALOG,
        )
    result = execute_record_query(
        CRMQueryPlan(
            object="company",
            properties=["name"],
            filters=[{"field": "industry", "value": "Healthcare"}],
        ),
        company_records,
        COMPANY_CATALOG,
    )

    assert result["matched_records"] == 0
    assert result["rows"] == []
