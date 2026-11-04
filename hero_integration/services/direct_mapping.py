"""Validated, explicit HERO-to-Frappe mapping definitions."""
from __future__ import annotations

from dataclasses import dataclass
import json
from hero_integration.services.projection_contracts import CONTRACTS as PROJECTIONS, STATUS_PATHS, validate_projection_spec


SOURCE_CONTRACTS = {
    "HeroCustomers": {
        "target_doctype": "Customer",
        # Server-owned bindings; imports, site settings and clients cannot redefine them.
        # Tuple: source path/type, target DocType/field, expected type, transformation.
        # These bindings grant no business approval.
        "native_field_bindings": frozenset(),
        "paths": {
            "title": "string|null",
            "id": "int", "nr": "string|null", "first_name": "string|null",
            "last_name": "string|null", "full_name": "string|null", "email": "string|null",
            "phone_mobile": "string|null", "phone_home": "string|null",
            "company_name": "string|null", "category": "string|null", "type": "string|null",
            "company_legal_form": "string|null", "source": "string|null",
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
            "name": "string|null", "partner_source": "string|null", "partner.email": "string|null",
            "partner_id": "int|null",
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
# HERO says itself whether a customer is private or commercial; this is the native Customer Type.
TRANSFORM_HERO_CUSTOMER_TYPE = "Customer Type From HERO Type"
HERO_CUSTOMER_TYPES = {"commercial": "Company", "private": "Individual"}
# A HERO person (by e-mail) as the enabled ERPNext User with that e-mail; no match stays empty.
TRANSFORM_USER_BY_EMAIL = "User By Email"
# A HERO partner (person) as the ERPNext User that carries its partner ID (set by the people sync).
TRANSFORM_USER_BY_PARTNER = "User By Partner ID"
USER_BINDINGS = {
    ("HeroProjectMatches", "partner.email", "Project", "custom_hero_project_manager"): TRANSFORM_USER_BY_EMAIL,
    ("HeroProjectMatches", "partner_id", "Project", "custom_hero_project_manager"): TRANSFORM_USER_BY_PARTNER,
}
# HERO plain text in a native Text Editor field (inverse: task_send.plain_text).
TRANSFORM_PLAIN_TEXT_HTML = "Plain Text To HTML"
NOTES_BINDING = ("partner_notes", "Project", "notes", TRANSFORM_PLAIN_TEXT_HTML)
# Older mapping revisions keep their app field; new revisions use the native notes.
LEGACY_NOTES_BINDING = ("partner_notes", "Project", "custom_hero_partner_notes", TRANSFORM_TRIM)
TRANSFORM_HERO_CUSTOMER_LINK = "HERO Customer Link"
TRANSFORM_CONFIRMED_COMPANY = "Confirmed ERPNext Company"
TRANSFORMATIONS = {
    TRANSFORM_IDENTITY, TRANSFORM_TRIM, TRANSFORM_POSITIVE_INT_TO_TEXT,
    TRANSFORM_CUSTOMER_TYPE, TRANSFORM_HERO_CUSTOMER_TYPE, TRANSFORM_HERO_CUSTOMER_LINK, TRANSFORM_CONFIRMED_COMPANY,
    TRANSFORM_PLAIN_TEXT_HTML, TRANSFORM_USER_BY_EMAIL, TRANSFORM_USER_BY_PARTNER,
}
TRANSFORMATIONS.update({'JSON Value', 'Scalar Text', 'HERO Project Link'})
TRANSFORMATIONS.update({'Native Adapter', 'Nonempty String ID'})
TRANSFORM_SALUTATION = "Salutation Table"
TRANSFORMATIONS.add(TRANSFORM_SALUTATION)
# Map HERO titles to Frappe Salutations; unknown values preserve the Contact value.
SALUTATION_TABLE = {"herr": "Herr", "frau": "Frau", "familie": "Familie", "eheleute": "Eheleute", "dr.": "Dr", "dr": "Dr"}

ALLOWED_TARGET_TYPES = {"Data", "Small Text", "Int", "Float", "Currency", "Percent", "Check", "Date", "Datetime", "Time",
                        "Link"}
# A value that names an existing record of the Link's DocType; anything else stays empty.
TRANSFORM_EXISTING_RECORD = "Existing Record"
TRANSFORM_SAFE_HTML = "Safe HTML"
TRANSFORMATIONS.update({TRANSFORM_EXISTING_RECORD, TRANSFORM_SAFE_HTML})
# Longer plain-text and choice fields (the picker offers them too).
PLAIN_TEXT_TYPES = frozenset({"Text", "Long Text", "Code", "Select"})
# Read-only HERO observation fields that are not text: (operation, path, field, transform) -> fieldtype.
NATIVE_TYPED_OBSERVATIONS = {
    ("HeroProjectMatches", "volume", "custom_hero_volume", "Identity"): "Currency",  # euros
    # Imported HERO observations are read-only on the Project form.
    ("HeroProjectMatches", "current_project_match_status.name", "custom_hero_observed_status_name", "Identity"): ("Data", "Small Text"),
    ("HeroProjectMatches", "address.full_address", "custom_hero_address_full_address", "Trim"): ("Data", "Small Text"),
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
    if spec.transformation == TRANSFORM_HERO_CUSTOMER_TYPE:
        return hero_customer_type(value)
    if spec.transformation == TRANSFORM_PLAIN_TEXT_HTML:
        return plain_text_html(value)
    if spec.transformation == TRANSFORM_SAFE_HTML:
        if not isinstance(value, str) or not value.strip():
            return None
        from frappe.utils.html_utils import sanitize_html
        return sanitize_html(value)
    if spec.transformation == TRANSFORM_EXISTING_RECORD:
        return existing_record(spec.target_doctype, spec.target_field, value)
    if spec.transformation == TRANSFORM_USER_BY_EMAIL:
        return user_by_email(value)
    if spec.transformation == TRANSFORM_USER_BY_PARTNER:
        from hero_integration.integrations.hero_people import user_by_partner
        return user_by_partner(value)
    return value


def existing_record(doctype: str, fieldname: str, value: object) -> str | None:
    """``value`` when it names an existing record of the Link field's DocType, else None."""
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
        return None
    import frappe

    field = frappe.get_meta(doctype).get_field(fieldname)
    target = getattr(field, "options", None) if field else None
    name = str(value).strip()
    return name if target and frappe.db.exists(target, name) else None


def user_by_email(value: object) -> str | None:
    """The one enabled ERPNext User with this e-mail, else None."""
    if not isinstance(value, str) or "@" not in value:
        return None
    import frappe

    rows = frappe.db.sql("SELECT name FROM `tabUser` WHERE enabled=1 AND LOWER(email)=%s LIMIT 2",
                         (value.strip().lower(),))
    return rows[0][0] if len(rows) == 1 else None


def plain_text_html(value: object) -> str | None:
    """``a\nb`` -> ``<p>a<br>b</p>``; empty text stays empty."""
    from html import escape

    if not isinstance(value, str) or not value.strip():
        return None
    return "<p>" + escape(value.strip()).replace("\r\n", "\n").replace("\n", "<br>") + "</p>"


def hero_customer_type(value: object) -> str:
    """HERO ``commercial`` is a Company; ``private`` and anything else is an Individual."""
    return HERO_CUSTOMER_TYPES.get(value.strip().lower(), "Individual") if isinstance(value, str) else "Individual"


class _Missing:
    """A selected path that the GraphQL response did not contain."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


def lookup(record: dict[str, object], source_path: str) -> object:
    """Read a response path without equating absent keys and explicit nulls.

    A null parent makes nested values None. Missing or non-object parents remain
    MISSING and cannot authorize clearing a value.
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
        if spec.transformation == TRANSFORM_HERO_CUSTOMER_TYPE:
            _require(spec, "type", "string|null", "Customer", "customer_type", TRANSFORM_HERO_CUSTOMER_TYPE)
        else:
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
    binding = (spec.source_path, spec.target_doctype, spec.target_field, spec.transformation)
    if spec.source_path == "partner_notes" and binding != LEGACY_NOTES_BINDING or spec.transformation == TRANSFORM_PLAIN_TEXT_HTML:
        if binding != NOTES_BINDING:
            raise DirectMappingError("HERO partner notes may only map to the native Project notes (Plain Text To HTML).")
        field = metadata.field("Project", "notes")
        if field is None or field.fieldtype != "Text Editor" or field.read_only or spec.relation:
            raise DirectMappingError("ERPNext Project.notes must be an editable Text Editor field.")
        return
    user_key = (spec.operation, spec.source_path, spec.target_doctype, spec.target_field)
    if spec.transformation in (TRANSFORM_USER_BY_EMAIL, TRANSFORM_USER_BY_PARTNER) or user_key in USER_BINDINGS:
        if USER_BINDINGS.get(user_key) != spec.transformation or spec.relation:
            raise DirectMappingError("A HERO person maps only to its reviewed User field.")
        field = metadata.field(spec.target_doctype, spec.target_field)
        if field is None or field.fieldtype != "Link" or (field.options or "") != "User":
            raise DirectMappingError("The responsible person field must link to User.")
        return
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
        ('HeroProjectMatches', 'name', 'custom_hero_designation', 'Trim'),
        ('HeroProjectMatches', 'partner_source', 'custom_hero_lead_source', 'Trim'),
        ('HeroCustomers', 'source', 'custom_hero_lead_source', 'Trim'),
    }
    key = (spec.operation, spec.source_path, spec.target_field, spec.transformation)
    is_native = key in native or key in NATIVE_TYPED_OBSERVATIONS
    allowed_types = NATIVE_TYPED_OBSERVATIONS.get(key, 'Data')
    allowed_types = (allowed_types,) if isinstance(allowed_types, str) else allowed_types
    if is_native and (field is None or field.fieldtype not in allowed_types):
        raise DirectMappingError('Native HERO observation field has an incompatible type.')
    _require_compatible_source(spec)
    # The app's own fields are read-only for users but written by the import.
    app_field = spec.target_field.startswith("custom_hero_") and bool(getattr(field, "is_custom_field", False))
    text_editor = field is not None and field.fieldtype == "Text Editor"
    if (field is None or (field.fieldtype not in ALLOWED_TARGET_TYPES | PLAIN_TEXT_TYPES and not text_editor)
            or (field.read_only and not (is_native or app_field))):
        raise DirectMappingError("Target field is not a writable supported Frappe field.")
    if spec.relation or any(getattr(field, key, None) for key in ("fetch_from", "is_virtual")) or (
            getattr(field, "hidden", 0) and not app_field):
        raise DirectMappingError("Target field is filled automatically or hidden; map its source record instead.")
    if text_editor and spec.transformation not in {TRANSFORM_PLAIN_TEXT_HTML, TRANSFORM_SAFE_HTML}:
        raise DirectMappingError("Formatted text needs the Plain Text To HTML or Safe HTML transformation.")
    if spec.transformation in {TRANSFORM_PLAIN_TEXT_HTML, TRANSFORM_SAFE_HTML} and not text_editor:
        raise DirectMappingError("HTML transformations only target formatted text fields.")
    if field.fieldtype == "Link" and (spec.transformation != TRANSFORM_EXISTING_RECORD or not (field.options or "")):
        raise DirectMappingError("A Link target needs the Existing Record transformation.")
    if spec.transformation == TRANSFORM_EXISTING_RECORD and field.fieldtype != "Link":
        raise DirectMappingError("Existing Record only targets Link fields.")


# Source kinds a transformation accepts (others are rejected before anything is written).
_SOURCE_KINDS = {
    TRANSFORM_TRIM: {"string"}, TRANSFORM_PLAIN_TEXT_HTML: {"string"}, TRANSFORM_SAFE_HTML: {"string"},
    TRANSFORM_EXISTING_RECORD: {"string", "int"}, TRANSFORM_USER_BY_EMAIL: {"string"},
    TRANSFORM_USER_BY_PARTNER: {"int"}, TRANSFORM_POSITIVE_INT_TO_TEXT: {"int"},
    "Scalar Text": {"int", "float", "boolean"},
}


def _require_compatible_source(spec: MappingSpec) -> None:
    kinds = _SOURCE_KINDS.get(spec.transformation)
    if kinds and (spec.source_type or "").split("|")[0] not in kinds:
        raise DirectMappingError("The transformation does not accept this HERO source type.")


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
