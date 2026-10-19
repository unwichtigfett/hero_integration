"""Deterministic, validated ERPNext project titles for HERO ProjectMatch records.

This module deliberately has no Frappe, GraphQL, or mapping-document dependency.
The importer supplies a ProjectMatch payload which has already been selected by the
approved read-only operation.  It never falls back to a Customer address or an
unreviewed field mapping.
"""
from __future__ import annotations

from collections.abc import Mapping


PROJECT_NAME_MAX_LENGTH = 140
TRADE_CATALOG = {"PV": "Photovoltaik"}


class ProjectTitleValidationError(ValueError):
    """Raised before a Project document is created when title data is incomplete."""


def build_project_title(record: Mapping[str, object], *, max_length: int = PROJECT_NAME_MAX_LENGTH) -> str:
    """Build the native ``Project.project_name`` without guessing domain values."""
    if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length < 1:
        raise ProjectTitleValidationError("ERPNext Project.project_name has no valid maximum length.")

    trade_code = _trade_code(record)
    trade_name = TRADE_CATALOG.get(trade_code)
    if not trade_name:
        raise ProjectTitleValidationError("Project trade has no confirmed HERO-to-ERPNext title mapping.")
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


def _trade_code(record: Mapping[str, object]) -> str:
    value = record.get("project_type")
    if isinstance(value, Mapping):
        value = value.get("code")
    if value is None:
        value = record.get("confirmed_project_type")
        if isinstance(value, Mapping):
            value = value.get("code")
    if not isinstance(value, str) or not value.strip():
        raise ProjectTitleValidationError("ProjectMatch.project_type or a confirmed ProjectType is required.")
    return value.strip().upper()


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
