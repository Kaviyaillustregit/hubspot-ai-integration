from datetime import date

import pytest

from app.agent.extraction import (
    ExtractionValidationError,
    validate_extraction,
    visible_text,
)
from app.agent.schemas import CRMIntentExtraction, GroundedSummary
from app.agent.tools import normalize_company_name
from app.ai.prompts import build_prompt, strip_json_fences
from app.integrations.errors import IntegrationError


def extraction(**fields) -> CRMIntentExtraction:
    return CRMIntentExtraction(
        intent=fields.pop("intent", "create_contact"),
        confidence=0.9,
        **fields,
    )


def test_grounded_values_map_to_allowed_hubspot_properties_only():
    validated = validate_extraction(
        "Add Victor Hall (VP Sales), victor@abc.com, +1 (555) 010-0199 under ABC company",
        extraction(
            first_name="Victor",
            last_name="Hall",
            job_title="VP Sales",
            email="victor@abc.com",
            phone="+1 (555) 010-0199",
            company_name="ABC company",
        ),
    )

    assert validated.properties == {
        "firstname": "Victor",
        "lastname": "Hall",
        "email": "victor@abc.com",
        "phone": "+1 (555) 010-0199",
        "jobtitle": "VP Sales",
    }
    assert validated.company_name == "ABC company"
    assert validated.contact_id is None


def test_grounding_uses_the_users_casing_and_spacing():
    validated = validate_extraction(
        "creat contact angel   john under abc",
        extraction(first_name="Angel John", company_name="ABC"),
    )

    assert validated.properties == {"firstname": "angel   john"}
    assert validated.company_name == "abc"


def test_requested_crm_fields_are_grounded_and_follow_the_users_order():
    validated = validate_extraction(
        "Show companies with phone, city, and number of employees",
        extraction(
            intent="crm_question",
            query="company_list",
            requested_fields=["employees", "city", "phone"],
        ),
    )

    assert validated.requested_fields == ["phone", "city", "employees"]


@pytest.mark.parametrize(
    ("message", "fields", "field"),
    [
        ("Create Victer Hall", {"first_name": "Victor"}, "first_name"),
        ("Create Victor, victor@abcc.com", {"email": "victor@abc.com"}, "email"),
        ("Create Victor", {"company_name": "ABC"}, "company_name"),
        ("Create Victor Hall", {"last_name": "Hal"}, "last_name"),
        ("Update contact 1234", {"intent": "update_contact", "contact_id": "123"}, "contact_id"),
        ("Create Victor victor@abc.com", {"company_name": "abc"}, "company_name"),
    ],
)
def test_ungrounded_values_are_rejected(message, fields, field):
    with pytest.raises(ExtractionValidationError) as raised:
        validate_extraction(message, extraction(**fields))

    assert raised.value.field == field
    assert raised.value.reason == "ungrounded"


@pytest.mark.parametrize(
    ("message", "fields", "field"),
    [
        ("Create Victor victor@abc", {"email": "victor@abc"}, "email"),
        ("Create Victor phone 12-34", {"phone": "12-34"}, "phone"),
    ],
)
def test_badly_formatted_values_are_rejected(message, fields, field):
    with pytest.raises(ExtractionValidationError) as raised:
        validate_extraction(message, extraction(**fields))

    assert raised.value.field == field
    assert raised.value.reason == "invalid_format"


def test_email_inside_slack_mailto_link_is_grounded():
    validated = validate_extraction(
        "Create Victor <mailto:victor@abc.com|victor@abc.com>",
        extraction(first_name="Victor", email="victor@abc.com"),
    )

    assert validated.properties["email"] == "victor@abc.com"


def test_extraction_schema_rejects_unknown_fields_and_intents():
    with pytest.raises(ValueError):
        CRMIntentExtraction.model_validate(
            {"intent": "delete_company", "confidence": 0.9}
        )
    with pytest.raises(ValueError):
        CRMIntentExtraction.model_validate(
            {"intent": "create_contact", "confidence": 0.9, "lifecyclestage": "lead"}
        )


@pytest.mark.parametrize(
    ("name", "normalized"),
    [
        ("ABC", "ABC"),
        ("ABC company", "ABC"),
        ("ABC Company, Inc.", "ABC"),
        ("  ABC   Corp ", "ABC"),
        ("Acme Holdings", "Acme Holdings"),
        ("Company", "Company"),
    ],
)
def test_company_name_normalization(name, normalized):
    assert normalize_company_name(name) == normalized


def test_prompt_builder_includes_instructions_schema_and_input():
    prompt = build_prompt(
        "account-intelligence/v1",
        {"crm": {"company": {"id": "1"}}},
        GroundedSummary,
    )

    assert "Use only these CRM facts" in prompt
    assert '"crm_facts"' in prompt
    assert '{"crm": {"company": {"id": "1"}}}' in prompt


def test_intent_prompt_forbids_inventing_or_correcting_values():
    prompt = build_prompt("crm-intent/v2", {"message": "hi"}, CRMIntentExtraction)

    assert "Never correct the spelling" in prompt
    assert "Never invent or infer" in prompt
    assert "never decide whether an action is allowed" in prompt


def test_crm_prompt_maps_deal_query_synonyms_and_account_to_company():
    prompt = build_prompt(
        "crm-intent/v2", {"message": "Show open opportunities"}, CRMIntentExtraction
    )

    assert "open_deals" in prompt
    assert "closed_won_deals" in prompt
    assert "closed_lost_deals" in prompt
    assert "best_chance_deals" in prompt
    assert "Account means HubSpot company" in prompt


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("2026-10-10", "2026-10-10T00:00:00Z"),
        ("Oct 10", "2026-10-10T00:00:00Z"),
        ("October 10th", "2026-10-10T00:00:00Z"),
        ("10 October 2026", "2026-10-10T00:00:00Z"),
        ("December 10,2026", "2026-12-10T00:00:00Z"),
        ("tomorrow", "2026-10-10T00:00:00Z"),
        ("next Friday", "2026-10-16T00:00:00Z"),
    ],
)
def test_deal_close_dates_are_normalized_from_grounded_phrases(phrase, expected):
    message = f"Create a deal called Renewal with close date {phrase}"
    validated = validate_extraction(
        message,
        extraction(
            intent="create_deal",
            deal_name="Renewal",
            deal_close_date=phrase,
        ),
        today=date(2026, 10, 9),
    )

    assert validated.deal_close_date == expected


@pytest.mark.parametrize(
    ("phrase", "reason"),
    [
        ("2026-02-30", "invalid_format"),
        ("next month", "ambiguous"),
        ("Oct 8", "ambiguous"),
    ],
)
def test_ambiguous_or_invalid_deal_close_dates_are_rejected(phrase, reason):
    with pytest.raises(ExtractionValidationError) as raised:
        validate_extraction(
            f"Create a deal called Renewal closing {phrase}",
            extraction(
                intent="create_deal",
                deal_name="Renewal",
                deal_close_date=phrase,
            ),
            today=date(2026, 10, 9),
        )

    assert raised.value.field == "deal_close_date"
    assert raised.value.reason == reason


def test_unknown_prompt_is_an_integration_error():
    with pytest.raises(IntegrationError):
        build_prompt("missing/v1", {}, GroundedSummary)


def test_json_code_fences_are_stripped():
    assert strip_json_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_json_fences('{"a": 1}') == '{"a": 1}'


@pytest.mark.parametrize(
    ("written", "normalized"),
    [
        ("$50,000", "50000"),
        ("50000", "50000"),
        ("75,000 USD", "75000"),
        ("50k", "50000"),
        ("1.5 million", "1500000"),
        ("$1,250.50", "1250.50"),
    ],
)
def test_deal_amounts_are_parsed_deterministically(written, normalized):
    validated = validate_extraction(
        f"Create a deal called Renewal worth {written}",
        extraction(intent="create_deal", deal_name="Renewal", deal_amount=written),
    )

    assert validated.deal_amount == normalized
    assert validated.deal_name == "Renewal"


@pytest.mark.parametrize("written", ["lots", "50,00,0", "-500", "1e9", "999999999999999"])
def test_invalid_deal_amounts_are_rejected(written):
    with pytest.raises(ExtractionValidationError) as raised:
        validate_extraction(
            f"Create a deal called Renewal worth {written}",
            extraction(intent="create_deal", deal_name="Renewal", deal_amount=written),
        )

    assert raised.value.field == "deal_amount"


def test_company_fields_are_grounded_and_mapped_to_hubspot_properties():
    validated = validate_extraction(
        "Update ABC Technologies: website <http://abctech.io|abctech.io>, phone +1 415 555 0100, "
        "city Austin",
        extraction(
            intent="update_company",
            company_name="ABC Technologies",
            company_website="abctech.io",
            company_phone="+1 415 555 0100",
            company_city="Austin",
        ),
    )

    assert validated.company_properties == {
        "website": "abctech.io",
        "phone": "+1 415 555 0100",
        "city": "Austin",
    }


def test_account_phone_and_employee_count_validate_for_hubspot_company_mapping():
    validated = validate_extraction(
        "Create an account named Testing KAVIYA with phone number 2385 and employees 1000.",
        extraction(
            intent="create_company",
            company_name="Testing KAVIYA",
            company_phone="2385",
            company_employees="1000",
        ),
    )

    assert validated.company_properties == {"phone": "2385", "numberofemployees": "1000"}


def test_company_domain_taken_from_an_email_is_rejected():
    with pytest.raises(ExtractionValidationError) as raised:
        validate_extraction(
            "Create a company for victor@abctech.io",
            extraction(intent="create_company", company_domain="abctech.io"),
        )

    assert raised.value.field == "company_domain"
    assert raised.value.reason == "ungrounded"


def test_deal_stage_must_be_written_by_the_user():
    with pytest.raises(ExtractionValidationError) as raised:
        validate_extraction(
            "Move the Renewal deal forward",
            extraction(intent="update_deal", deal_name="Renewal", deal_stage="Closed Won"),
        )

    assert raised.value.field == "deal_stage"


def test_slack_markup_is_reduced_to_visible_text():
    assert visible_text("<@U1> add <mailto:a@b.co|a@b.co> to <http://abc.io|abc.io> <!here>") == (
        "  add a@b.co to abc.io  "
    )
