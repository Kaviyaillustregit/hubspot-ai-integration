"""Generalized currency parsing and normalization for HubSpot CRM.

Supports international currencies and Indian numbering conventions (Lakh, Crore,
Indian comma grouping) with Decimal precision.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re


class CurrencyParsingError(ValueError):
    """Raised when an amount or currency cannot be parsed."""

    def __init__(self, field: str, value: str, code: str = "invalid_format") -> None:
        super().__init__(f"Invalid currency amount: {value}")
        self.field = field
        self.value = value
        self.code = code


@dataclass(frozen=True, slots=True)
class ParsedCurrencyAmount:
    """Structured representation of a parsed monetary amount."""

    amount: Decimal
    amount_str: str
    currency: str | None
    raw_input: str


# Canonical ISO currency codes
_CURRENCY_MAP: dict[str, str] = {
    # Indian Rupee
    "₹": "INR",
    "rs.": "INR",
    "rs": "INR",
    "inr": "INR",
    "rupee": "INR",
    "rupees": "INR",
    # US Dollar
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "dollar": "USD",
    "dollars": "USD",
    # Euro
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    # British Pound
    "£": "GBP",
    "gbp": "GBP",
    "pound": "GBP",
    "pounds": "GBP",
}

# Number scale multipliers
_MULTIPLIER_MAP: dict[str, Decimal] = {
    "k": Decimal("1000"),
    "thousand": Decimal("1000"),
    "thousands": Decimal("1000"),
    "lakh": Decimal("100000"),
    "lakhs": Decimal("100000"),
    "lac": Decimal("100000"),
    "lacs": Decimal("100000"),
    "m": Decimal("1000000"),
    "million": Decimal("1000000"),
    "millions": Decimal("1000000"),
    "crore": Decimal("10000000"),
    "crores": Decimal("10000000"),
    "cr": Decimal("10000000"),
    "b": Decimal("1000000000"),
    "billion": Decimal("1000000000"),
    "billions": Decimal("1000000000"),
}

_MAX_AMOUNT = Decimal(10) ** 12

_INDIAN_GROUPING = re.compile(r"^\d{1,2}(?:,\d{2})*,\d{3}$")
_STANDARD_GROUPING = re.compile(r"^\d{1,3}(?:,\d{3})+$")


def normalize_currency_code(raw: str | None) -> str | None:
    """Normalize a raw currency code or symbol to its 3-letter ISO code."""
    if not raw or not raw.strip():
        return None
    cleaned = raw.strip().casefold()
    if cleaned in _CURRENCY_MAP:
        return _CURRENCY_MAP[cleaned]
    # If 3 uppercase/lowercase letters e.g. CAD, AUD, JPY
    if re.fullmatch(r"[A-Za-z]{3}", cleaned):
        return cleaned.upper()
    return None


def parse_currency_amount(text: str, *, field_name: str = "deal_amount") -> ParsedCurrencyAmount:
    """Parse a monetary amount string into a structured ParsedCurrencyAmount.

    Supports:
    - Symbols: ₹, $, €, £, US$
    - Text codes: INR, USD, EUR, GBP, Rs., Rs, rupees, dollars, euros, pounds
    - Indian grouping: 1,00,000; 10,00,000; 1,25,50,000; 2,50,000
    - Standard grouping: 50,000; 1,000,000
    - Words: lakh/lakhs, crore/crores, thousand, million, billion, k, m, cr
    - Prefixed, suffixed, or standalone currency
    - Plain numeric inputs
    - Decimal precision via Decimal
    """
    raw_input = text
    s = text.strip()
    if not s:
        raise CurrencyParsingError(field_name, text, "invalid_format")

    # Strip common Indian accounting suffix '/-' or trailing punctuation
    if s.endswith("/-"):
        s = s[:-2].strip()
    elif s.endswith("/"):
        s = s[:-1].strip()

    currencies_found: list[str] = []

    # 1. Match prefix currency symbol or code
    prefix_sym = re.match(r"^([₹$€£])\s*", s)
    if prefix_sym:
        sym = prefix_sym.group(1)
        currencies_found.append(_CURRENCY_MAP[sym])
        s = s[prefix_sym.end():].strip()
    else:
        prefix_word = re.match(r"^(rs\.|rs\b|inr\b|us\$|\$|usd\b|eur\b|gbp\b)\s*", s, re.IGNORECASE)
        if prefix_word:
            word = prefix_word.group(1).casefold().rstrip()
            currencies_found.append(_CURRENCY_MAP[word])
            s = s[prefix_word.end():].strip()

    # 2. Match suffix currency symbol
    suffix_sym = re.search(r"\s*([₹$€£])$", s)
    if suffix_sym:
        sym = suffix_sym.group(1)
        currencies_found.append(_CURRENCY_MAP[sym])
        s = s[:suffix_sym.start()].strip()

    # 3. Match suffix tokens (multipliers and currency words)
    multiplier: Decimal | None = None

    while True:
        trailing = re.search(r"\s+([a-zA-Z.]+)\s*$", s)
        if not trailing:
            # Check attached suffix like 50k, 2.5cr, 100USD
            joined = re.search(r"([0-9])([a-zA-Z]+)$", s)
            if joined:
                token = joined.group(2).casefold()
                if token in _MULTIPLIER_MAP and multiplier is None:
                    multiplier = _MULTIPLIER_MAP[token]
                    s = s[:-len(token)].strip()
                    continue
                if token in _CURRENCY_MAP:
                    currencies_found.append(_CURRENCY_MAP[token])
                    s = s[:-len(token)].strip()
                    continue
            break

        token = trailing.group(1).casefold()
        if token in _MULTIPLIER_MAP and multiplier is None:
            multiplier = _MULTIPLIER_MAP[token]
            s = s[:trailing.start()].strip()
        elif token in _CURRENCY_MAP:
            currencies_found.append(_CURRENCY_MAP[token])
            s = s[:trailing.start()].strip()
        else:
            break

    # Validate that we didn't get conflicting currencies (e.g. "$500 INR")
    unique_currencies = set(currencies_found)
    if len(unique_currencies) > 1:
        raise CurrencyParsingError(field_name, text, "ambiguous")

    final_currency = currencies_found[0] if currencies_found else None

    if not s:
        raise CurrencyParsingError(field_name, text, "invalid_format")

    # Split integer and fraction parts
    if "." in s:
        parts = s.split(".")
        if len(parts) != 2:
            raise CurrencyParsingError(field_name, text, "invalid_format")
        int_part, frac_part = parts
        if not frac_part.isdigit() or len(frac_part) == 0:
            raise CurrencyParsingError(field_name, text, "invalid_format")
    else:
        int_part = s
        frac_part = None

    # Validate integer grouping
    if int_part.isdigit():
        pass
    elif _STANDARD_GROUPING.fullmatch(int_part):
        pass
    elif _INDIAN_GROUPING.fullmatch(int_part):
        pass
    else:
        raise CurrencyParsingError(field_name, text, "invalid_format")

    clean_digits = int_part.replace(",", "")
    if frac_part:
        clean_digits = f"{clean_digits}.{frac_part}"

    try:
        amount = Decimal(clean_digits)
    except InvalidOperation as exc:
        raise CurrencyParsingError(field_name, text, "invalid_format") from exc

    if multiplier:
        amount *= multiplier

    if amount < 0 or amount > _MAX_AMOUNT:
        raise CurrencyParsingError(field_name, text, "invalid_format")

    if amount == amount.to_integral_value():
        amount_str = str(int(amount))
    else:
        amount_str = f"{amount.quantize(Decimal('0.01'))}"

    return ParsedCurrencyAmount(
        amount=amount,
        amount_str=amount_str,
        currency=final_currency,
        raw_input=raw_input,
    )
