"""Validated, explicit HERO-to-Frappe mapping definitions."""
from __future__ import annotations

from dataclasses import dataclass


SOURCE_CONTRACTS = {
    "HeroCustomers": {
        "target_doctype": "Customer",
        "paths": {
            "id": "int", "nr": "string|null", "first_name": "string|null",
            "last_name": "string|null", "full_name": "string|null", "email": "string|null",
            "phone_mobile": "string|null", "phone_home": "string|null",
            "company_name": "string|null", "category": "string|null", "type": "string|null",
            "is_contact_person": "boolean|null", "is_deleted": "boolean|null",
            "address.id": "int|null", "address.street": "string|null", "address.zipcode": "string|null",
            "address.city": "string|null",
        },
    },
    "HeroProjectMatches": {
        "target_doctype": "Project",
        "paths": {
            "id": "int", "customer_id": "int|null", "project_id": "int|null",
            "project_title": "string", "project_nr": "string", "project_type": "string|null",
            "partner_notes": "string|null", "customer.title": "string", "customer.full_name": "string",
            "address.full_address": "string|null", "display_id": "string|null", "created": "string|null",
            "modified": "string|null",
        },
    },
}

PURPOSE_FIELD = "Field"
PURPOSE_SOURCE_IDENTITY = "Source Identity"
PURPOSE_RECORD_NAME = "Record Name"
PURPOSE_CUSTOMER_TYPE = "Customer Type"
PURPOSE_CUSTOMER_CONTACT = "Customer Primary Contact"
PURPOSE_CUSTOMER_HOME_PHONE = "Customer Home Phone"
PURPOSE_LIFECYCLE_FLAG = "Lifecycle Flag"
PURPOSE_CUSTOMER_RELATION = "Customer Relation"
PURPOSE_CONFIRMED_COMPANY = "Confirmed Company"
PURPOSES = {
    PURPOSE_FIELD, PURPOSE_SOURCE_IDENTITY, PURPOSE_RECORD_NAME, PURPOSE_CUSTOMER_TYPE,
    PURPOSE_CUSTOMER_CONTACT, PURPOSE_CUSTOMER_HOME_PHONE, PURPOSE_LIFECYCLE_FLAG,
    PURPOSE_CUSTOMER_RELATION, PURPOSE_CONFIRMED_COMPANY,
}

TRANSFORM_IDENTITY = "Identity"
TRANSFORM_TRIM = "Trim"
TRANSFORM_POSITIVE_INT_TO_TEXT = "Positive Int To Text"
TRANSFORM_CUSTOMER_TYPE = "Customer Type From Company Name"
TRANSFORM_HERO_CUSTOMER_LINK = "HERO Customer Link"
TRANSFORM_CONFIRMED_COMPANY = "Confirmed ERPNext Company"
TRANSFORMATIONS = {
    TRANSFORM_IDENTITY, TRANSFORM_TRIM, TRANSFORM_POSITIVE_INT_TO_TEXT,
    TRANSFORM_CUSTOMER_TYPE, TRANSFORM_HERO_CUSTOMER_LINK, TRANSFORM_CONFIRMED_COMPANY,
}

ALLOWED_TARGET_TYPES = {"Data", "Small Text", "Int", "Float", "Check", "Date", "Datetime", "Link"}
SYSTEM_FIELDS = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"}
PROTECTED_FIELDS = {
    "Customer": {"name", "customer_name", "customer_type", "customer_primary_contact", "disabled", "status"},
    "Project": {"name", "project_name", "customer", "company", "status", "docstatus", "notes"},
}
RESERVED_TARGET_FIELDS = {
    "Customer": {"custom_hero_source_id", "first_name", "last_name", "email_id", "mobile_no"},
    "Project": {"custom_hero_source_id"},
}


class DirectMappingError(ValueError):
    pass


@dataclass(frozen=True)
class MappingSpec:
    operation: str
    source_path: str
    target_doctype: str
    target_field: str
    source_type: str
    purpose: str = PURPOSE_FIELD
    transformation: str = TRANSFORM_IDENTITY
    relation: str = ""
    priority: int = 100
    definition_id: str = ""


def validate_spec(spec: MappingSpec, metadata) -> None:
    contract = SOURCE_CONTRACTS.get(spec.operation)
    if not contract or spec.source_path not in contract["paths"]:
        raise DirectMappingError("Source path is not selected by the approved HERO operation.")
    if spec.source_type != contract["paths"][spec.source_path]:
        raise DirectMappingError("Source type does not match the approved HERO contract.")
    if spec.purpose not in PURPOSES or spec.transformation not in TRANSFORMATIONS:
        raise DirectMappingError("Mapping purpose or transformation is not supported.")
    ensure_write = getattr(metadata, "ensure_write", None)
    if ensure_write:
        ensure_write(spec.target_doctype)
    _validate_binding(spec, metadata)


def transformed_value(spec: MappingSpec, value: object) -> object | None:
    if spec.transformation == TRANSFORM_IDENTITY:
        return value
    if spec.transformation == TRANSFORM_TRIM:
        return value.strip() if isinstance(value, str) and value.strip() else None
    if spec.transformation == TRANSFORM_POSITIVE_INT_TO_TEXT:
        return str(value) if isinstance(value, int) and value > 0 else None
    if spec.transformation == TRANSFORM_CUSTOMER_TYPE:
        return "Company" if isinstance(value, str) and value.strip() else "Individual"
    return value


def _validate_binding(spec: MappingSpec, metadata) -> None:
    if spec.purpose == PURPOSE_FIELD:
        _validate_custom_field(spec, metadata)
        return
    if spec.purpose == PURPOSE_SOURCE_IDENTITY:
        _require(spec, "id", "int", spec.target_doctype, "custom_hero_source_id", TRANSFORM_POSITIVE_INT_TO_TEXT)
        return
    if spec.purpose == PURPOSE_RECORD_NAME:
        target = "customer_name" if spec.operation == "HeroCustomers" else "project_name"
        _require(spec, target_doctype=spec.target_doctype, target_field=target, transformation=TRANSFORM_TRIM)
        return
    if spec.purpose == PURPOSE_CUSTOMER_TYPE:
        _require(spec, "company_name", "string|null", "Customer", "customer_type", TRANSFORM_CUSTOMER_TYPE)
        return
    if spec.purpose == PURPOSE_CUSTOMER_CONTACT:
        if spec.operation != "HeroCustomers" or spec.target_doctype != "Customer":
            raise DirectMappingError("Customer contact mappings only apply to HeroCustomers.")
        if spec.target_field not in {"first_name", "last_name", "email_id", "mobile_no"} or spec.transformation != TRANSFORM_TRIM:
            raise DirectMappingError("Customer contact mapping is not an approved native binding.")
        return
    if spec.purpose == PURPOSE_CUSTOMER_HOME_PHONE:
        _require(spec, "phone_home", "string|null", "Contact Phone", "phone", TRANSFORM_TRIM, "Customer Primary Contact")
        return
    if spec.purpose == PURPOSE_LIFECYCLE_FLAG:
        _require(spec, "is_deleted", "boolean|null", "HERO Integration Link", "sync_status", TRANSFORM_IDENTITY)
        return
    if spec.purpose == PURPOSE_CUSTOMER_RELATION:
        _require(spec, "customer_id", "int|null", "Project", "customer", TRANSFORM_HERO_CUSTOMER_LINK, "HERO Customer Link")
        return
    if spec.purpose == PURPOSE_CONFIRMED_COMPANY:
        _require(spec, target_doctype="Project", target_field="company", transformation=TRANSFORM_CONFIRMED_COMPANY, relation="Confirmed ERPNext Company")
        return
    raise DirectMappingError("Mapping purpose is not supported.")


def _validate_custom_field(spec: MappingSpec, metadata) -> None:
    contract = SOURCE_CONTRACTS[spec.operation]
    if spec.target_doctype != contract["target_doctype"]:
        raise DirectMappingError("A field mapping may only write its operation target DocType.")
    if spec.source_path == "partner_notes" and (
        spec.target_doctype != "Project"
        or spec.target_field != "custom_hero_partner_notes"
        or spec.transformation != TRANSFORM_TRIM
    ):
        raise DirectMappingError("HERO partner notes may only map explicitly to Project.custom_hero_partner_notes.")
    protected = SYSTEM_FIELDS | PROTECTED_FIELDS[spec.target_doctype] | RESERVED_TARGET_FIELDS[spec.target_doctype]
    if spec.target_field in protected:
        raise DirectMappingError("Identity, Company, status, and standard bindings are protected.")
    field = metadata.field(spec.target_doctype, spec.target_field)
    if field is None or field.fieldtype not in ALLOWED_TARGET_TYPES or field.read_only:
        raise DirectMappingError("Target field is not a writable supported Frappe field.")
    if not getattr(field, "is_custom_field", False):
        raise DirectMappingError("A site-specific mapping may only write a Custom Field.")
    if field.fieldtype == "Link":
        raise DirectMappingError("Link targets require an approved entity resolver.")


def _require(
    spec: MappingSpec,
    source_path: str | None = None,
    source_type: str | None = None,
    target_doctype: str | None = None,
    target_field: str | None = None,
    transformation: str | None = None,
    relation: str | None = None,
) -> None:
    checks = (
        (source_path, spec.source_path), (source_type, spec.source_type),
        (target_doctype, spec.target_doctype), (target_field, spec.target_field),
        (transformation, spec.transformation), (relation, spec.relation),
    )
    if any(expected is not None and actual != expected for expected, actual in checks):
        raise DirectMappingError("Mapping does not match the protected binding contract.")
