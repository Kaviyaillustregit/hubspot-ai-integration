"""Deterministic validation of LLM-extracted CRM fields.

The LLM output is untrusted: every value must literally appear in the user's message,
and the value actually used is the user's own text, never the model's rewrite.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.agent.schemas import CRMIntentExtraction, CRMIntentName

# Extraction field -> HubSpot contact property. No other property is ever written.
CONTACT_PROPERTY_FIELDS: dict[str, str] = {
    "first_name": "firstname",
    "last_name": "lastname",
    "email": "email",
    "phone": "phone",
    "job_title": "jobtitle",
}

# Extraction field -> HubSpot company property (besides the name).
COMPANY_PROPERTY_FIELDS: dict[str, str] = {
    "company_domain": "domain",
    "company_website": "website",
    "company_phone": "phone",
    "company_city": "city",
}

_FIELD_LABELS = {
    "first_name": "first name",
    "last_name": "last name",
    "email": "email address",
    "phone": "phone number",
    "job_title": "job title",
    "company_name": "company name",
    "contact_id": "contact ID",
    "company_domain": "company domain",
    "company_website": "company website",
    "company_phone": "company phone number",
    "company_city": "company city",
    "deal_name": "deal name",
    "deal_amount": "deal amount",
    "deal_stage": "deal stage",
    "deal_pipeline": "deal pipeline",
}

_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_PHONE = re.compile(r"^\+?[0-9][0-9 ().-]*[0-9]$")
_CONTACT_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_DOMAIN = re.compile(r"^(?:https?://)?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/\S*)?$")
_AMOUNT = re.compile(
    r"^(?:usd|us\$|\$|€|£|₹|inr|eur|gbp)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?"
    r"\s*(k|m|thousand|million)?\s*(?:usd|dollars?|inr|rupees?|eur|euros?|gbp|pounds?)?$",
    re.IGNORECASE,
)
_AMOUNT_MULTIPLIERS = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000}
_MAX_AMOUNT = Decimal(10) ** 12
# Slack markup: <@U1> mentions, <#C1|name> channels, <!here>, <mailto:a@b.c|a@b.c>, <http://x|x>.
_SLACK_TOKEN = re.compile(r"<([^<>|]*)(?:\|([^<>]*))?>")
_EMAIL_SPAN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class ExtractionValidationError(ValueError):
    def __init__(self, field: str, value: str, reason: str) -> None:
        super().__init__(f"{field} failed validation: {reason}")
        self.field = field
        self.value = value
        self.reason = reason

    @property
    def user_message(self) -> str:
        label = _FIELD_LABELS.get(self.field, self.field)
        if self.reason == "ungrounded":
            return (
                f"I couldn't match the {label} \"{self.value}\" to your message exactly, "
                "so I didn't make any changes. Please check it and try again."
            )
        return f"\"{self.value}\" doesn't look like a valid {label}, so I didn't make any changes."


@dataclass(frozen=True)
class ValidatedExtraction:
    intent: CRMIntentName
    properties: dict[str, str]
    company_name: str | None
    contact_id: str | None
    company_properties: dict[str, str] = field(default_factory=dict)
    deal_name: str | None = None
    # Normalized decimal string, e.g. "50000" or "1250.50".
    deal_amount: str | None = None
    deal_stage: str | None = None
    deal_pipeline: str | None = None


def validate_extraction(message: str, extraction: CRMIntentExtraction) -> ValidatedExtraction:
    visible = visible_text(message)
    # Only the email field may be grounded inside an email address; otherwise "ABC"
    # would be "found" in "victor@abc.com".
    without_emails = _EMAIL_SPAN.sub(lambda matched: " " * len(matched.group(0)), visible)

    properties: dict[str, str] = {}
    for name, property_name in CONTACT_PROPERTY_FIELDS.items():
        source = visible if name == "email" else without_emails
        value = _grounded(source, name, getattr(extraction, name))
        if value is not None:
            properties[property_name] = value

    email = properties.get("email")
    if email is not None and not _EMAIL.fullmatch(email):
        raise ExtractionValidationError("email", email, "invalid_format")
    _check_phone("phone", properties.get("phone"))

    contact_id = _grounded(without_emails, "contact_id", extraction.contact_id)
    if contact_id is not None and not _CONTACT_ID.fullmatch(contact_id):
        raise ExtractionValidationError("contact_id", contact_id, "invalid_format")

    company_properties: dict[str, str] = {}
    for name, property_name in COMPANY_PROPERTY_FIELDS.items():
        value = _grounded(without_emails, name, getattr(extraction, name))
        if value is not None:
            company_properties[property_name] = value
    for name in ("domain", "website"):
        value = company_properties.get(name)
        if value is not None and not _DOMAIN.fullmatch(value):
            raise ExtractionValidationError(f"company_{name}", value, "invalid_format")
    _check_phone("company_phone", company_properties.get("phone"))

    amount_text = _grounded(without_emails, "deal_amount", extraction.deal_amount)

    return ValidatedExtraction(
        intent=extraction.intent,
        properties=properties,
        company_name=_grounded(without_emails, "company_name", extraction.company_name),
        contact_id=contact_id,
        company_properties=company_properties,
        deal_name=_grounded(without_emails, "deal_name", extraction.deal_name),
        deal_amount=normalize_amount(amount_text) if amount_text is not None else None,
        deal_stage=_grounded(without_emails, "deal_stage", extraction.deal_stage),
        deal_pipeline=_grounded(without_emails, "deal_pipeline", extraction.deal_pipeline),
    )


def visible_text(message: str) -> str:
    """The message as the user sees it: links show their label, mentions are removed."""

    def replace(matched: re.Match[str]) -> str:
        target, label = matched.group(1), matched.group(2)
        if target.startswith(("@", "#", "!")):
            return " "
        shown = label if label is not None else target
        return shown.removeprefix("mailto:")

    return _SLACK_TOKEN.sub(replace, message)


def normalize_amount(text: str) -> str:
    """Parse "$50,000", "75000", "50k" or "1.5 million" into a plain decimal string."""
    matched = _AMOUNT.fullmatch(text.strip())
    if matched is None:
        raise ExtractionValidationError("deal_amount", text, "invalid_format")
    whole, fraction, scale = matched.groups()
    try:
        amount = Decimal(whole.replace(",", "") + (f".{fraction}" if fraction else ""))
    except InvalidOperation as exc:
        raise ExtractionValidationError("deal_amount", text, "invalid_format") from exc
    if scale:
        amount *= _AMOUNT_MULTIPLIERS[scale.lower()]
    if amount > _MAX_AMOUNT:
        raise ExtractionValidationError("deal_amount", text, "invalid_format")
    if amount == amount.to_integral_value():
        return str(int(amount))
    return f"{amount.quantize(Decimal('0.01'))}"


def _check_phone(name: str, phone: str | None) -> None:
    if phone is not None and (
        not _PHONE.fullmatch(phone) or sum(char.isdigit() for char in phone) < 7
    ):
        raise ExtractionValidationError(name, phone, "invalid_format")


def _grounded(message: str, field: str, value: str | None) -> str | None:
    """Return the span of ``message`` that ``value`` refers to, or reject the value."""
    if value is None:
        return None
    tokens = value.split()
    if not tokens:
        return None
    # Case and whitespace may differ from the model's copy; characters may not.
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(token) for token in tokens) + r"(?!\w)"
    matched = re.search(pattern, message, re.IGNORECASE)
    if matched is None:
        raise ExtractionValidationError(field, value.strip(), "ungrounded")
    return matched.group(0)
