"""Validated, explicit HERO-to-Frappe mapping definitions."""
from __future__ import annotations

from dataclasses import dataclass
import json
from hero_integration.services.projection_contracts import CONTRACTS as PROJECTIONS, STATUS_PATHS, validate_projection_spec


SOURCE_CONTRACTS = {
    "HeroCustomers": {
        "target_doctype": "Customer",
        # Reviewed bindings are shipped with the server contract, never read
        # from a mapping, import payload, site configuration, or client.
        # Tuple: source path, source type, target DocType, target field,
        # expected field type, transformation. No business approvals yet.
        "native_field_bindings": frozenset(),
        "paths": {
            "title": "string|null",
            "id": "int", "nr": "string|null", "first_name": "string|null",
            "last_name": "string|null", "full_name": "string|null", "email": "string|null",
            "phone_mobile": "string|null", "phone_home": "string|null",
            "company_name": "string|null", "category": "string|null", "type": "string|null",
            "company_legal_form": "string|null",
            "is_contact_person": "boolean|null", "is_deleted": "boolean|null",
            "address.id": "int|null", "address.street": "string|null", "address.zipcode": "string|null",
            "address.city": "string|null", "address.line_2": "string|null", "address.country.code": "string|null",
        },
    },
    "HeroProjectMatches": {
        "target_doctype": "Project",
        "paths": {
            "volume": "float|null",
            "id": "int", "customer_id": "int|null", "project_id": "int|null",
            "project_title": "string", "project_nr": "string", "project_type": "string|null",
            "partner_notes": "string|null", "customer.title": "string", "customer.full_name": "string",
            "address.full_address": "string|null", "display_id": "string|null", "created": "string|null",
            "modified": "string|null",
        },
    },
}
SOURCE_CONTRACTS.update({name: {'target_doctype': value['target_doctype'], 'paths': value['paths']} for name, value in PROJECTIONS.items()})
SOURCE_CONTRACTS['HeroProjectMatches']['paths'].update(STATUS_PATHS)

PURPOSE_FIELD = "Field"
PURPOSE_SOURCE_IDENTITY = "Source Identity"
PURPOSE_RECORD_NAME = "Record Name"
PURPOSE_CUSTOMER_TYPE = "Customer Type"
PURPOSE_CUSTOMER_CONTACT = "Customer Primary Contact"
PURPOSE_CUSTOMER_HOME_PHONE = "Customer Home Phone"
PURPOSE_LIFECYCLE_FLAG = "Lifecycle Flag"
PURPOSE_CUSTOMER_RELATION = "Customer Relation"
PURPOSE_CONFIRMED_COMPANY = "Confirmed Company"
PURPOSE_NATIVE_BINDING = "Native Binding"
RELATION_CUSTOMER_PRIMARY_ADDRESS = "Customer Primary Address"
PURPOSES = {
    PURPOSE_FIELD, PURPOSE_SOURCE_IDENTITY, PURPOSE_RECORD_NAME, PURPOSE_CUSTOMER_TYPE,
    PURPOSE_CUSTOMER_CONTACT, PURPOSE_CUSTOMER_HOME_PHONE, PURPOSE_LIFECYCLE_FLAG,
    PURPOSE_CUSTOMER_RELATION, PURPOSE_CONFIRMED_COMPANY,
}
PURPOSES.add('Project Relation')
PURPOSES.add(PURPOSE_NATIVE_BINDING)

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
TRANSFORMATIONS.update({'JSON Value', 'Scalar Text', 'HERO Project Link'})
TRANSFORMATIONS.update({'Native Adapter', 'Nonempty String ID'})
TRANSFORM_SALUTATION = "Salutation Table"
TRANSFORMATIONS.add(TRANSFORM_SALUTATION)
# HERO customer title -> Frappe Salutation (owner decision 2026-11-02). Unknown
# values are not written; the stored Contact salutation then stays as it is.
SALUTATION_TABLE = {"herr": "Herr", "frau": "Frau", "familie": "Familie", "eheleute": "Eheleute", "dr.": "Dr", "dr": "Dr"}

ALLOWED_TARGET_TYPES = {"Data", "Small Text", "Int", "Float", "Currency", "Check", "Date", "Datetime", "Link"}
# Read-only HERO observation fields that are not text: (operation, path, field, transform) -> fieldtype.
NATIVE_TYPED_OBSERVATIONS = {
    ("HeroProjectMatches", "volume", "custom_hero_volume", "Identity"): "Currency",  # euros, owner 2026-11-01
}
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
    priority: int = 5
    definition_id: str = ""


NATIVE_TASK_RULES = {
    'id': ('int', 'HERO Integration Link', 'source_id', PURPOSE_SOURCE_IDENTITY, TRANSFORM_POSITIVE_INT_TO_TEXT, 'Data'),
    'title': ('string|null', 'Task', 'subject', PURPOSE_RECORD_NAME, TRANSFORM_TRIM, 'Data'),
    'comment': ('string|null', 'Task', 'description', PURPOSE_NATIVE_BINDING, 'Native Adapter', 'Text Editor'),
    'target_user_id': ('int|null', 'ToDo', 'allocated_to', PURPOSE_NATIVE_BINDING, 'Native Adapter', 'Link'),
    'target_project_match_id': ('int|null', 'Task', 'project', PURPOSE_NATIVE_BINDING, 'Native Adapter', 'Link'),
    'due_date': ('string|null', 'Task', 'custom_hero_due_date', PURPOSE_NATIVE_BINDING, 'Native Adapter', 'Datetime'),
}


def validate_native_task_rule(spec: MappingSpec, metadata=None) -> None:
    rule = NATIVE_TASK_RULES.get(spec.source_path)
    actual = (spec.source_type, spec.target_doctype, spec.target_field, spec.purpose, spec.transformation)
    if spec.operation != 'HeroTasks' or not rule or actual != rule[:5] or spec.relation:
        raise DirectMappingError('Task rule is not a server-approved native field binding.')
    if metadata is not None:
        metadata.ensure_write(spec.target_doctype)
        field = metadata.field(spec.target_doctype, spec.target_field)
        if field is None or field.fieldtype != rule[5] or (spec.source_path not in {'id', 'due_date'} and getattr(field, 'read_only', 0)):
            raise DirectMappingError('Native Task field is unavailable or incompatible.')
        if spec.source_path == 'due_date' and not getattr(field, 'read_only', 0):
            raise DirectMappingError('Native Task external deadline must be read-only.')
        if spec.source_path == 'target_user_id' and (field.options or '') != 'User':
            raise DirectMappingError('Native ToDo assignment must reference a User.')
        if spec.source_path == 'target_project_match_id' and (field.options or '') != 'Project':
            raise DirectMappingError('Native Task project must reference a Project.')


def validate_spec(spec: MappingSpec, metadata) -> None:
    contract = SOURCE_CONTRACTS.get(spec.operation)
    if not contract or spec.source_path not in contract["paths"]:
        raise DirectMappingError("Source path is not selected by the approved HERO operation.")
    if spec.source_type != contract["paths"][spec.source_path]:
        raise DirectMappingError("Source type does not match the approved HERO contract.")
    if spec.purpose not in PURPOSES or spec.transformation not in TRANSFORMATIONS:
        raise DirectMappingError("Mapping purpose or transformation is not supported.")
    if isinstance(spec.priority, bool) or not isinstance(spec.priority, int) or not 0 <= spec.priority <= 10:
        raise DirectMappingError("Mapping priority must be an integer from 0 through 10.")
    ensure_write = getattr(metadata, "ensure_write", None)
    if ensure_write:
        ensure_write(spec.target_doctype)
    if spec.operation == 'HeroTasks' and spec.target_doctype in {'Task', 'HERO Integration Link', 'ToDo'}:
        validate_native_task_rule(spec, metadata)
        return
    if spec.operation in PROJECTIONS:
        validate_projection_spec(spec, metadata)
        return
    _validate_binding(spec, metadata)


def transformed_value(spec: MappingSpec, value: object) -> object | None:
    # Authoritative business identifiers are never trimmed by a generic text rule.
    if (spec.target_doctype, spec.target_field, spec.source_path) in {
        ("Customer", "custom_hero_customer_number", "nr"),
        ("Project", "custom_hero_display_id", "display_id"),
    } and isinstance(value, str):
        return value
    if spec.transformation == TRANSFORM_IDENTITY:
        return value
    if spec.transformation == TRANSFORM_SALUTATION:
        return SALUTATION_TABLE.get(value.strip().lower()) if isinstance(value, str) else None
    if spec.transformation == TRANSFORM_TRIM:
        return value.strip() if isinstance(value, str) and value.strip() else None
    if spec.transformation == TRANSFORM_POSITIVE_INT_TO_TEXT:
        return str(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
    if spec.transformation == 'Nonempty String ID':
        return value if isinstance(value, str) and value == value.strip() and 0 < len(value) <= 140 and all(ord(c) >= 32 for c in value) else None
    if spec.transformation == 'JSON Value':
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if spec.transformation == 'Scalar Text':
        if value is None:
            return None
        if not isinstance(value, (int, float, bool)):
            raise DirectMappingError('Scalar Text requires a numeric or boolean value.')
        return json.dumps(value, allow_nan=False)
    if spec.transformation == TRANSFORM_CUSTOMER_TYPE:
        return "Company" if isinstance(value, str) and value.strip() else "Individual"
    return value


class _Missing:
    """A selected path that the GraphQL response did not contain."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


def lookup(record: dict[str, object], source_path: str) -> object:
    """Read one response path, keeping an absent key distinct from ``null``.

    An explicit ``null`` parent object makes every nested selection ``None``
    (GraphQL nulls the whole object); an absent key or a non-object parent is
    ``MISSING`` and must never be treated as an authoritative empty value.
    """
    current: object = record
    for segment in source_path.split("."):
        if current is None:
            return None
        if not isinstance(current, dict) or segment not in current:
            return MISSING
        current = current[segment]
    return current


def source_value(record: dict[str, object], source_path: str) -> object | None:
    """Read one approved finite HERO response path without inventing fallbacks."""
    value = lookup(record, source_path)
    return None if value is MISSING else value


def _validate_binding(spec: MappingSpec, metadata) -> None:
    if spec.purpose == PURPOSE_FIELD:
        _validate_custom_field(spec, metadata)
        return
    if spec.purpose == PURPOSE_SOURCE_IDENTITY:
        _require(spec, "id", "int", SOURCE_CONTRACTS[spec.operation]['target_doctype'], "custom_hero_source_id", TRANSFORM_POSITIVE_INT_TO_TEXT)
        return
    if spec.purpose == PURPOSE_RECORD_NAME:
        target = "customer_name" if spec.operation == "HeroCustomers" else "project_name"
        _require(spec, target_doctype=SOURCE_CONTRACTS[spec.operation]['target_doctype'], target_field=target, transformation=TRANSFORM_TRIM)
        return
    if spec.purpose == PURPOSE_CUSTOMER_TYPE:
        _require(spec, "company_name", "string|null", "Customer", "customer_type", TRANSFORM_CUSTOMER_TYPE)
        return
    if spec.purpose == PURPOSE_CUSTOMER_CONTACT:
        if spec.operation != "HeroCustomers" or spec.target_doctype != "Customer":
            raise DirectMappingError("Customer contact mappings only apply to HeroCustomers.")
        if spec.target_field == "salutation":
            _require(spec, "title", "string|null", "Customer", "salutation", TRANSFORM_SALUTATION)
            return
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
    if spec.purpose == PURPOSE_NATIVE_BINDING:
        if spec.operation != "HeroCustomers":
            raise DirectMappingError("Native bindings outside reviewed projections are not supported.")
        _require(
            spec, "address.line_2", "string|null", "Address", "address_line2", TRANSFORM_TRIM,
            RELATION_CUSTOMER_PRIMARY_ADDRESS,
        )
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
    native = {
        ('HeroCustomers', 'nr', 'custom_hero_customer_number', 'Trim'),
        ('HeroCustomers', 'category', 'custom_hero_customer_category', 'Trim'),
        ('HeroCustomers', 'type', 'custom_hero_customer_type', 'Trim'),
        ('HeroCustomers', 'company_legal_form', 'custom_hero_company_legal_form', 'Trim'),
        ('HeroProjectMatches', 'project_id', 'custom_hero_related_project_id', 'Positive Int To Text'),
        ('HeroProjectMatches', 'display_id', 'custom_hero_display_id', 'Trim'),
        ('HeroProjectMatches', 'created', 'custom_hero_source_created', 'Trim'),
        ('HeroProjectMatches', 'modified', 'custom_hero_source_modified', 'Trim'),
    }
    key = (spec.operation, spec.source_path, spec.target_field, spec.transformation)
    is_native = key in native or key in NATIVE_TYPED_OBSERVATIONS
    if is_native and (field is None or field.fieldtype != NATIVE_TYPED_OBSERVATIONS.get(key, 'Data')):
        raise DirectMappingError('Native HERO observation field has an incompatible type.')
    if field is None or field.fieldtype not in ALLOWED_TARGET_TYPES or (field.read_only and not is_native):
        raise DirectMappingError("Target field is not a writable supported Frappe field.")
    if not getattr(field, "is_custom_field", False):
        binding = (spec.source_path, spec.source_type, spec.target_doctype,
                   spec.target_field, field.fieldtype, spec.transformation)
        allowed_native = (
            binding in contract.get("native_field_bindings", frozenset())
            and not spec.relation
            and not any(getattr(field, key, None) for key in ("hidden", "fetch_from", "is_virtual"))
        )
        if not allowed_native:
            raise DirectMappingError("A site-specific mapping requires an approved native field or a Custom Field.")
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
