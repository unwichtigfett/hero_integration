"""Deterministic, validated ERPNext project titles for HERO ProjectMatch records.

This module deliberately has no Frappe, GraphQL, or mapping-document dependency.
The importer supplies a ProjectMatch payload which has already been selected by the
approved read-only operation.  It never falls back to a Customer address or an
unreviewed field mapping.
"""
from __future__ import annotations

from collections.abc import Mapping


PROJECT_NAME_MAX_LENGTH = 140
# Confirmed HERO project types (user decision 2026-10-30).
TRADE_CATALOG = {
    "PV": "Photovoltaik",
    "KLIM": "Klimatechnik",
    "ELEK": "Elektrohandwerk",
    "SONS": "Sonstiges",
    "ABS": "Absturzsicherung",
    "DS": "Dachsanierung",
}
_TRADE_BY_NAME = {name.casefold(): code for code, name in TRADE_CATALOG.items()}


class ProjectTitleValidationError(ValueError):
    """Raised before a Project document is created when title data is incomplete."""


def project_title_missing(record: Mapping[str, object]) -> bool:
    """Identify omitted title inputs without treating explicit null as absence."""
    if "project_nr" not in record or ("project_type" not in record and "confirmed_project_type" not in record):
        return True
    if "customer" not in record or "address" not in record:
        return True
    customer, address = record["customer"], record["address"]
    if isinstance(customer, Mapping) and any(key not in customer for key in ("title", "full_name")):
        return True
    if isinstance(address, Mapping) and not any(key in address for key in ("full_address", "formatted_address")):
        return any(key not in address for key in ("street", "house_number", "postal_code", "city"))
    return False


def build_project_title(record: Mapping[str, object], *, max_length: int = PROJECT_NAME_MAX_LENGTH) -> str:
    """Build the native ``Project.project_name`` without guessing domain values."""
    if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length < 1:
        raise ProjectTitleValidationError("ERPNext Project.project_name has no valid maximum length.")

    trade_code, trade_name = project_trade(record)
    project_number = _project_number(record, trade_code)
    customer = _mapping(record.get("customer"), "ProjectMatch.customer")
    salutation = _required_text(customer.get("title"), "ProjectMatch.customer.title")
    customer_name = _required_text(customer.get("full_name"), "ProjectMatch.customer.full_name")
    address = _project_address(record.get("address"))

    title = f"{trade_code}-{project_number} | {trade_name}, {salutation} {customer_name}, {address}"
    title = " ".join(title.split())
    if len(title) > max_length:
        raise ProjectTitleValidationError(
            f"Generated Project.project_name exceeds its {max_length}-character field limit."
        )
    return title


def project_trade(record: Mapping[str, object]) -> tuple[str, str]:
    """The HERO trade (measure) by code or name; project_type is only a fallback."""
    candidates = []
    measure = record.get("measure")
    if isinstance(measure, Mapping):
        candidates += [measure.get("short"), measure.get("name")]
    for field in ("project_type", "confirmed_project_type"):
        value = record.get(field)
        candidates.append(value.get("code") if isinstance(value, Mapping) else value)
    values = [value.strip() for value in candidates if isinstance(value, str) and value.strip()]
    if not values:
        raise ProjectTitleValidationError("ProjectMatch.measure, project_type or a confirmed ProjectType is required.")
    for value in values:
        code = value.upper() if value.upper() in TRADE_CATALOG else _TRADE_BY_NAME.get(value.casefold())
        if code:
            return code, TRADE_CATALOG[code]
    raise ProjectTitleValidationError(f"Project trade {values[0][:40]!r} has no confirmed HERO-to-ERPNext title mapping.")


def _project_number(record: Mapping[str, object], trade_code: str) -> str:
    number = _required_text(record.get("project_nr"), "ProjectMatch.project_nr")
    prefix = f"{trade_code}-"
    while number.upper().startswith(prefix):
        number = number[len(prefix):].strip()
    if not number:
        raise ProjectTitleValidationError("ProjectMatch.project_nr is empty after its trade prefix.")
    return number


def _project_address(value: object) -> str:
    address = _mapping(value, "ProjectMatch.address")
    formatted = address.get("full_address") or address.get("formatted_address")
    if isinstance(formatted, str) and formatted.strip():
        return _normalise_address_text(formatted, "ProjectMatch.address.full_address")

    street = _required_text(address.get("street"), "ProjectMatch.address.street")
    house_number = _required_text(address.get("house_number"), "ProjectMatch.address.house_number")
    postal_code = _required_text(address.get("postal_code"), "ProjectMatch.address.postal_code")
    city = _required_text(address.get("city"), "ProjectMatch.address.city")
    return f"{street} {house_number}, {postal_code} {city}"


def _normalise_address_text(value: str, field: str) -> str:
    parts = [" ".join(part.split()) for part in value.split(",") if part.strip()]
    if len(parts) < 2:
        raise ProjectTitleValidationError(f"{field} is not a complete formatted project address.")
    return ", ".join(parts)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProjectTitleValidationError(f"{field} is required and must be an object.")
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectTitleValidationError(f"{field} is required for the ERPNext project title.")
    return " ".join(value.split())
