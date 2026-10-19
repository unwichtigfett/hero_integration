"""Fail-closed ERPNext guard and idempotent HERO schema extensions."""
from __future__ import annotations

import frappe


ERPNext_VERSION_CONSTRAINT = ">=16.0.0-dev,<17.0.0"

HERO_CUSTOM_FIELDS = {
    "Customer": [
        {
            "fieldname": "custom_hero_source_id",
            "fieldtype": "Data",
            "label": "HERO Source ID",
            "insert_after": "customer_name",
            "read_only": 1,
            "no_copy": 1,
            "unique": 1,
        },
        {
            "fieldname": "custom_hero_customer_number",
            "fieldtype": "Data",
            "label": "HERO Customer Number",
            "insert_after": "custom_hero_source_id",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_customer_category",
            "fieldtype": "Data",
            "label": "HERO Customer Category",
            "insert_after": "custom_hero_customer_number",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_customer_type",
            "fieldtype": "Data",
            "label": "HERO Customer Type",
            "insert_after": "custom_hero_customer_category",
            "read_only": 1,
            "no_copy": 1,
        }
    ],
    "Project": [
        {
            "fieldname": "custom_hero_source_id",
            "fieldtype": "Data",
            "label": "HERO Source ID",
            "insert_after": "project_name",
            "read_only": 1,
            "no_copy": 1,
            "unique": 1,
        },
        {
            "fieldname": "custom_hero_related_project_id",
            "fieldtype": "Data",
            "label": "HERO Related Project ID",
            "insert_after": "custom_hero_source_id",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_display_id",
            "fieldtype": "Data",
            "label": "HERO Display ID",
            "insert_after": "custom_hero_related_project_id",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_source_created",
            "fieldtype": "Data",
            "label": "HERO Source Created",
            "insert_after": "custom_hero_display_id",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_source_modified",
            "fieldtype": "Data",
            "label": "HERO Source Modified",
            "insert_after": "custom_hero_source_created",
            "read_only": 1,
            "no_copy": 1,
        }
    ],
}

_REQUIRED_ERPNext_DOCTYPES = (
    "Customer",
    "Contact",
    "Contact Email",
    "Contact Phone",
    "Address",
    "Project",
)


def before_install() -> None:
    _require_erpnext_schema()


def after_install() -> None:
    ensure_hero_source_fields()


def after_migrate() -> None:
    ensure_hero_source_fields()


def ensure_hero_source_fields() -> None:
    _require_erpnext_schema()
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    missing = _missing_custom_field_definitions()
    if missing:
        # ``update=False`` makes this a create-only path. Existing fields are
        # inspected first and a different foreign definition aborts migration.
        create_custom_fields(missing, update=False)


def _missing_custom_field_definitions() -> dict[str, list[dict[str, object]]]:
    missing: dict[str, list[dict[str, object]]] = {}
    for doctype, definitions in HERO_CUSTOM_FIELDS.items():
        for definition in definitions:
            existing_name = frappe.db.exists(
                "Custom Field", {"dt": doctype, "fieldname": definition["fieldname"]}
            )
            if not existing_name:
                missing.setdefault(doctype, []).append(definition)
                continue
            existing = frappe.get_doc("Custom Field", existing_name)
            conflicts = [
                key for key, expected in definition.items()
                if _field_value(existing, key) != _normalised_field_value(expected)
            ]
            if conflicts:
                frappe.throw(
                    f"Custom Field {doctype}.{definition['fieldname']} conflicts with the HERO schema "
                    f"({', '.join(conflicts)})."
                )
    return missing


def _field_value(document, fieldname: str) -> object:
    value = document.get(fieldname) if hasattr(document, "get") else getattr(document, fieldname, None)
    return _normalised_field_value(value)


def _normalised_field_value(value: object) -> object:
    if isinstance(value, bool):
        return int(value)
    if value in (None, ""):
        return 0 if isinstance(value, bool) else value
    return value


def _require_erpnext_schema() -> None:
    if "erpnext" not in frappe.get_installed_apps():
        frappe.throw("ERPNext v16 must be installed before HERO Integration.")
    try:
        from erpnext import __version__ as erpnext_version
    except ImportError:
        frappe.throw("ERPNext v16 must be importable before HERO Integration.")
    if not str(erpnext_version).startswith("16."):
        frappe.throw(f"HERO Integration requires ERPNext {ERPNext_VERSION_CONSTRAINT}.")
    missing = [doctype for doctype in _REQUIRED_ERPNext_DOCTYPES if not frappe.db.exists("DocType", doctype)]
    if missing:
        frappe.throw(f"ERPNext schema is incomplete: {', '.join(missing)}.")
    from hero_integration.integrations.erpnext.project_notes import NativeProjectNotesError, require_native_project_notes

    try:
        require_native_project_notes(frappe.get_meta("Project"))
    except NativeProjectNotesError as error:
        frappe.throw(str(error))
