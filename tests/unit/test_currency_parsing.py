"""Unit tests for generalized currency parsing and normalization."""

from decimal import Decimal
import pytest

from app.agent.currency import (
    CurrencyParsingError,
    ParsedCurrencyAmount,
    normalize_currency_code,
    parse_currency_amount,
)
from app.agent.extraction import ExtractionValidationError, normalize_amount


def test_indian_rupee_symbols_and_aliases():
    """Verify various Indian Rupee symbols and abbreviations resolve correctly."""
    cases = [
        ("₹250000", Decimal("250000"), "250000", "INR"),
        ("₹ 250000", Decimal("250000"), "250000", "INR"),
        ("Rs. 250000", Decimal("250000"), "250000", "INR"),
        ("Rs 250000", Decimal("250000"), "250000", "INR"),
        ("rs 250000", Decimal("250000"), "250000", "INR"),
        ("INR 250000", Decimal("250000"), "250000", "INR"),
        ("inr 250000", Decimal("250000"), "250000", "INR"),
        ("250000 INR", Decimal("250000"), "250000", "INR"),
        ("250000 rupees", Decimal("250000"), "250000", "INR"),
        ("250000 rupee", Decimal("250000"), "250000", "INR"),
        ("250000 ₹", Decimal("250000"), "250000", "INR"),
        ("Rs. 250000/-", Decimal("250000"), "250000", "INR"),
    ]
    for text, expected_amt, expected_str, expected_cur in cases:
        parsed = parse_currency_amount(text)
        assert parsed.amount == expected_amt, f"Failed amount for {text}"
        assert parsed.amount_str == expected_str, f"Failed amount_str for {text}"
        assert parsed.currency == expected_cur, f"Failed currency for {text}"


def test_indian_comma_grouping():
    """Verify Indian numbering comma grouping conventions are parsed accurately."""
    cases = [
        ("₹2,50,000", Decimal("250000"), "250000", "INR"),
        ("1,00,000", Decimal("100000"), "100000", None),
        ("10,00,000", Decimal("1000000"), "1000000", None),
        ("1,25,50,000", Decimal("12550000"), "12550000", None),
        ("₹ 1,25,50,000", Decimal("12550000"), "12550000", "INR"),
        ("Rs. 50,000", Decimal("50000"), "50000", "INR"),
    ]
    for text, expected_amt, expected_str, expected_cur in cases:
        parsed = parse_currency_amount(text)
        assert parsed.amount == expected_amt
        assert parsed.amount_str == expected_str
        assert parsed.currency == expected_cur


def test_lakh_and_crore_expressions():
    """Verify lakh, crore and other Indian scale multipliers."""
    cases = [
        ("2.5 lakh", Decimal("250000"), "250000", None),
        ("2.5 lakhs", Decimal("250000"), "250000", None),
        ("₹ 2.5 lakh", Decimal("250000"), "250000", "INR"),
        ("2.5 lakh rupees", Decimal("250000"), "250000", "INR"),
        ("Rs. 2.5 lakhs", Decimal("250000"), "250000", "INR"),
        ("1 crore", Decimal("10000000"), "10000000", None),
        ("1.5 crores", Decimal("15000000"), "15000000", None),
        ("₹ 1.5 crore", Decimal("15000000"), "15000000", "INR"),
        ("1.5 cr", Decimal("15000000"), "15000000", None),
        ("1.5 cr INR", Decimal("15000000"), "15000000", "INR"),
        ("10 crores", Decimal("100000000"), "100000000", None),
    ]
    for text, expected_amt, expected_str, expected_cur in cases:
        parsed = parse_currency_amount(text)
        assert parsed.amount == expected_amt
        assert parsed.amount_str == expected_str
        assert parsed.currency == expected_cur


def test_decimal_amounts():
    """Verify decimal numbers are quantized accurately with Decimal."""
    cases = [
        ("₹ 2,50,000.50", Decimal("250000.50"), "250000.50", "INR"),
        ("Rs. 1,00,000.75", Decimal("100000.75"), "100000.75", "INR"),
        ("50000.00", Decimal("50000.00"), "50000", None),
        ("1.25 lakh", Decimal("125000"), "125000", None),
        ("0.75 crore", Decimal("7500000"), "7500000", None),
        ("$1250.99", Decimal("1250.99"), "1250.99", "USD"),
    ]
    for text, expected_amt, expected_str, expected_cur in cases:
        parsed = parse_currency_amount(text)
        assert parsed.amount == expected_amt
        assert parsed.amount_str == expected_str
        assert parsed.currency == expected_cur


def test_international_currencies_and_numeric_inputs():
    """Verify international currencies and plain numeric inputs remain supported."""
    cases = [
        ("$50,000", Decimal("50000"), "50000", "USD"),
        ("US$ 50,000", Decimal("50000"), "50000", "USD"),
        ("50,000 USD", Decimal("50000"), "50000", "USD"),
        ("50000", Decimal("50000"), "50000", None),
        ("50k", Decimal("50000"), "50000", None),
        ("1.5 million", Decimal("1500000"), "1500000", None),
        ("1.5m", Decimal("1500000"), "1500000", None),
        ("$1.5 million", Decimal("1500000"), "1500000", "USD"),
        ("€100", Decimal("100"), "100", "EUR"),
        ("100 EUR", Decimal("100"), "100", "EUR"),
        ("£500", Decimal("500"), "500", "GBP"),
        ("500 GBP", Decimal("500"), "500", "GBP"),
    ]
    for text, expected_amt, expected_str, expected_cur in cases:
        parsed = parse_currency_amount(text)
        assert parsed.amount == expected_amt
        assert parsed.amount_str == expected_str
        assert parsed.currency == expected_cur


def test_malformed_and_ambiguous_inputs():
    """Verify invalid comma placement, conflicting currencies and malformed inputs are rejected."""
    invalid_cases = [
        "1,0000",        # Bad comma grouping
        "1,00",          # Incomplete comma grouping
        "1,,000",        # Double comma
        ",500",          # Leading comma
        "500,",          # Trailing comma
        "$500 INR",      # Conflicting currencies
        "₹ $ 500",       # Double prefix symbols
        "1.2.3",         # Multiple decimal points
        "-500",          # Negative amount
        "10000000000000",# Exceeds max amount
        "",              # Empty string
        "   ",           # Whitespace
        "abc",           # Not a number
    ]
    for text in invalid_cases:
        with pytest.raises(CurrencyParsingError):
            parse_currency_amount(text)


def test_normalize_amount_backward_compatibility():
    """Verify normalize_amount in extraction module uses the new parsing layer."""
    assert normalize_amount("₹2,50,000") == "250000"
    assert normalize_amount("Rs. 2,50,000") == "250000"
    assert normalize_amount("2.5 lakh") == "250000"
    assert normalize_amount("1 crore") == "10000000"
    assert normalize_amount("$50,000") == "50000"
    assert normalize_amount("50k") == "50000"
    assert normalize_amount("1.5 million") == "1500000"

    with pytest.raises(ExtractionValidationError):
        normalize_amount("1,0000")
