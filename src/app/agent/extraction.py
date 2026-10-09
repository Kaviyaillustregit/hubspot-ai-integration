"""Deterministic validation of LLM-extracted CRM fields.

The LLM output is untrusted: every value must literally appear in the user's message,
and the value actually used is the user's own text, never the model's rewrite.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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
    "company_employees": "numberofemployees",
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
    "company_employees": "number of employees",
    "deal_name": "deal name",
    "deal_amount": "deal amount",
    "deal_stage": "deal stage",
    "deal_pipeline": "deal pipeline",
    "deal_close_date": "deal close date",
    "deal_type": "deal type",
    "deal_owner": "deal owner",
    "deal_currency": "deal currency",
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
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_DATE = re.compile(
    r"^(?P<month>[A-Za-z]+)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:(?:,\s*|\s+)(?P<year>\d{4}))?$",
    re.IGNORECASE,
)
_DAY_MONTH_DATE = re.compile(
    r"^(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>[A-Za-z]+)\.?"
    r"(?:(?:,\s*|\s+)(?P<year>\d{4}))?$",
    re.IGNORECASE,
)
_WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_MONTH_NAMES = {
    name.casefold(): month
    for month, names in enumerate(
        (
            (),
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        )
    )
    for name in names
}


class ExtractionValidationError(ValueError):
    def __init__(self, field: str, value: str, reason: str) -> None:
        super().__init__(f"{field} failed validation: {reason}")
        self.field = field
        self.value = value
        self.reason = reason

    @property
    def user_message(self) -> str:
        label = _FIELD_LABELS.get(self.field, self.field)
        if self.reason == "ambiguous":
            return (
                f"I couldn't determine the {label} \"{self.value}\" unambiguously. "
                "Please provide an exact date."
            )
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
    deal_close_date: str | None = None
    deal_type: str | None = None
    deal_owner: str | None = None
    deal_currency: str | None = None
    requested_fields: list[str] = field(default_factory=list)


def validate_extraction(
    message: str, extraction: CRMIntentExtraction, *, today: date | None = None
) -> ValidatedExtraction:
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
    _check_phone("company_phone", company_properties.get("phone"), min_digits=4)
    employees = company_properties.get("numberofemployees")
    if employees is not None and (not employees.isdigit() or len(employees) > 9):
        raise ExtractionValidationError("company_employees", employees, "invalid_format")

    amount_text = _grounded(without_emails, "deal_amount", extraction.deal_amount)
    close_date_text = _grounded(
        without_emails, "deal_close_date", extraction.deal_close_date
    )
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
        deal_close_date=(
            normalize_deal_close_date(close_date_text, today=today)
            if close_date_text is not None
            else None
        ),
        deal_type=_grounded(without_emails, "deal_type", extraction.deal_type),
        deal_owner=_grounded(without_emails, "deal_owner", extraction.deal_owner),
        deal_currency=_grounded(without_emails, "deal_currency", extraction.deal_currency),
        requested_fields=_grounded_requested_fields(visible, extraction.requested_fields),
    )


_REQUESTED_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "company name", "contact name", "deal name"),
    "owner": ("owner",),
    "phone": ("phone", "phone number"),
    "email": ("email", "email address"),
    "city": ("city",),
    "state": ("state", "region"),
    "industry": ("industry",),
    "employees": ("employees", "employee count", "number of employees"),
    "lifecycle": ("lifecycle stage", "lifecycle"),
    "lead_status": ("lead status",),
    "last_contacted": ("last contacted", "last contact"),
    "job_title": ("employment role", "job title", "role"),
    "job_sub_role": ("job sub role", "sub role"),
    "seniority": ("job seniority", "seniority"),
    "linkedin": ("linkedin", "linkedin url"),
    "company": ("associated company", "company"),
    "associated_deals": ("associated deals", "deals"),
    "stage": ("stage name", "deal stage", "stage"),
    "amount": ("amount", "deal amount"),
    "probability": ("probability",),
    "close_date": ("close date", "closing date"),
}


def _grounded_requested_fields(message: str, requested_fields: list[str]) -> list[str]:
    normalized = message.casefold()
    mentions: list[tuple[int, str]] = []
    for field_name in dict.fromkeys(requested_fields):
        aliases = _REQUESTED_FIELD_ALIASES.get(field_name)
        if aliases is None:
            continue
        positions = [
            match.start()
            for alias in aliases
            if (match := re.search(rf"\b{re.escape(alias)}\b", normalized)) is not None
        ]
        if positions:
            mentions.append((min(positions), field_name))
    return [field_name for _, field_name in sorted(mentions)]


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


def normalize_deal_close_date(text: str, *, today: date | None = None) -> str:
    """Resolve a grounded date phrase to a UTC date-time accepted by HubSpot."""
    current_date = today or datetime.now(UTC).date()
    value = text.strip().rstrip(".,!?").casefold()
    try:
        if _ISO_DATE.fullmatch(value):
            return _hubspot_date(date.fromisoformat(value))
        if value == "today":
            return _hubspot_date(current_date)
        if value == "tomorrow":
            return _hubspot_date(current_date + timedelta(days=1))
        weekday_match = re.fullmatch(r"next\s+([a-z]+)", value)
        if weekday_match and weekday_match.group(1) in _WEEKDAY_NAMES:
            target = _WEEKDAY_NAMES[weekday_match.group(1)]
            days_ahead = (target - current_date.weekday()) % 7 or 7
            return _hubspot_date(current_date + timedelta(days=days_ahead))

        matched = _MONTH_DATE.fullmatch(value) or _DAY_MONTH_DATE.fullmatch(value)
        if matched:
            groups = matched.groupdict()
            month = _MONTH_NAMES.get(groups["month"].rstrip(".").casefold())
            if month is None:
                raise ValueError
            day = int(groups["day"])
            year = int(groups["year"]) if groups["year"] else current_date.year
            parsed = date(year, month, day)
            if groups["year"] is None and parsed < current_date:
                raise ExtractionValidationError("deal_close_date", text, "ambiguous")
            return _hubspot_date(parsed)
    except ExtractionValidationError:
        raise
    except ValueError as exc:
        raise ExtractionValidationError("deal_close_date", text, "invalid_format") from exc
    raise ExtractionValidationError("deal_close_date", text, "ambiguous")


def _hubspot_date(value: date) -> str:
    return f"{value.isoformat()}T00:00:00Z"


def _check_phone(name: str, phone: str | None, *, min_digits: int = 7) -> None:
    if phone is not None and (
        not _PHONE.fullmatch(phone) or sum(char.isdigit() for char in phone) < min_digits
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
