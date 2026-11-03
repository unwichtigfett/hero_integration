"""Fail-closed ERPNext guard and idempotent HERO schema extensions."""
from __future__ import annotations

import frappe

from hero_integration.services.service_schema import ITEM_FIELDS as SERVICE_ITEM_FIELDS
from hero_integration.services.employee_mapping import OBSERVATIONS


ERPNext_VERSION_CONSTRAINT = ">=16.0.0-dev,<17.0.0"

DISPLAY_PROPERTIES = frozenset({"label"})

HERO_CUSTOM_FIELDS = {
    "Comment": [
        {"fieldname":"custom_hero_project", "label":"Source Project", "fieldtype":"Link", "options":"Project",
         "read_only":1, "no_copy":1, "hidden":1},
        {"fieldname":"custom_hero_logbook_context", "label":"Source Context", "fieldtype":"Code", "options":"JSON",
         "read_only":1, "no_copy":1, "hidden":1},
    ],
    # The HERO person behind an ERPNext user: projects, assignments and tasks link it by these IDs.
    "User": [
        {
            "fieldname": "custom_hero_partner_id",
            "fieldtype": "Data",
            "label": "Partner ID",
            "insert_after": "username",
            "read_only": 1,
            "no_copy": 1,
            "unique": 1,
        },
        {
            "fieldname": "custom_hero_user_id",
            "fieldtype": "Data",
            "label": "Source User ID",
            "insert_after": "custom_hero_partner_id",
            "read_only": 1,
            "no_copy": 1,
            "unique": 1,
            "hidden": 1,
        },
    ],
    "Item": [
        *SERVICE_ITEM_FIELDS,
        {
            "fieldname": "custom_hero_manufacturer_nr",
            "fieldtype": "Data",
            "label": "Manufacturer Part No",
            "insert_after": "brand",
            "read_only": 1,
        },
    ],
    "Customer": [
        {
            "fieldname": "custom_hero_source_id",
            "fieldtype": "Data",
            "label": "Source ID",
            "insert_after": "customer_name",
            "read_only": 1,
            "no_copy": 1,
            "unique": 1,
        },
        {
            "fieldname": "custom_hero_customer_number",
            "fieldtype": "Data",
            "label": "Customer Number",
            "insert_after": "custom_hero_source_id",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_customer_category",
            "fieldtype": "Data",
            "label": "Customer Category",
            "insert_after": "custom_hero_customer_number",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_lead_source",
            "fieldtype": "Data",
            "label": "Source",
            "insert_after": "custom_hero_customer_category",
            "read_only": 1,
            "no_copy": 1,
        },
        # The HERO customer type sets the native Customer Type; no separate field.
    ],
    "Project": [
        {
            "fieldname": "custom_hero_source_id",
            "fieldtype": "Data",
            "label": "Source ID",
            "insert_after": "project_name",
            "read_only": 1,
            "no_copy": 1,
            "unique": 1,
        },
        {
            "fieldname": "custom_hero_related_project_id",
            "fieldtype": "Data",
            "label": "Project ID",
            "insert_after": "custom_hero_source_id",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_display_id",
            "fieldtype": "Data",
            "label": "Project Nr.",
            "insert_after": "custom_hero_related_project_id",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_source_created",
            "fieldtype": "Data",
            "label": "Creation",
            "insert_after": "custom_hero_display_id",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_source_modified",
            "fieldtype": "Data",
            "label": "Modified",
            "insert_after": "custom_hero_source_created",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_designation",
            "fieldtype": "Data",
            "label": "Project Designation",
            "insert_after": "custom_hero_display_id",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_hero_lead_source",
            "fieldtype": "Data",
            "label": "Source",
            "insert_after": "custom_hero_source_modified",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            # The responsible HERO partner, matched to an ERPNext user by e-mail.
            "fieldname": "custom_hero_project_manager",
            "fieldtype": "Link",
            "options": "User",
            "label": "Project Responsible",
            "insert_after": "custom_hero_designation",
            "read_only": 1,
            "no_copy": 1,
            "in_standard_filter": 1,
        },
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


for _doctype, _fields in OBSERVATIONS.items():
    HERO_CUSTOM_FIELDS.setdefault(_doctype, []).extend(
        {"fieldname": field, "label": label, "fieldtype": "Data", "read_only": 1, "no_copy": 1}
        for field, label in _fields
    )


def ensure_employee_optional_fields() -> None:
    """Represent the owner's optional birth date/gender policy in metadata."""
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    if not frappe.db.exists("DocType", "Employee"):
        return
    for field in ("gender", "date_of_birth"):
        if frappe.get_meta("Employee").get_field(field).reqd:
            make_property_setter("Employee", field, "reqd", 0, "Check", validate_fields_for_doctype=False)


def before_install() -> None:
    _require_erpnext_schema()


def after_install() -> None:
    ensure_hero_source_fields()
    ensure_employee_optional_fields()
    from hero_integration.app_setup import apply_form_layout, ensure_app_records

    ensure_app_records()
    from hero_integration.services.desk_theme import ensure_navy_theme

    ensure_navy_theme()
    apply_form_layout()
    if _reference_apps_installed():
        from hero_integration.integrations.app_references import setup_reference_access

        setup_reference_access()


def after_migrate() -> None:
    ensure_hero_source_fields()
    ensure_employee_optional_fields()
    from hero_integration.app_setup import ensure_app_records

    ensure_app_records()
    from hero_integration.services.desk_theme import ensure_navy_theme

    ensure_navy_theme()
    if _reference_apps_installed():
        # Create-only; also covers Helpdesk/CRM installed after this app.
        from hero_integration.integrations.app_references import ensure_reference_fields

        ensure_reference_fields()


def _reference_apps_installed() -> bool:
    """Helpdesk or CRM reference ERPNext Customers/Projects."""
    return bool({"helpdesk", "crm"} & set(frappe.get_installed_apps()))


def ensure_hero_source_fields() -> None:
    _require_erpnext_schema()
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    missing = _missing_custom_field_definitions()
    if missing:
        # ``update=False`` makes this a create-only path. Existing fields are
        # inspected first and a different foreign definition aborts migration.
        create_custom_fields(missing, update=False)
    from hero_integration.integrations.field_labels import relabel_fields

    relabel_fields()


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
            # The label is display only; the app sets it itself (field_labels).
            conflicts = [
                key for key, expected in definition.items()
                if key not in DISPLAY_PROPERTIES and _field_value(existing, key) != _normalised_field_value(expected)
            ]
            if conflicts:
                frappe.throw(frappe._("Custom Field {0}.{1} conflicts with the HERO schema ({2}).")
                             .format(doctype, definition['fieldname'], ', '.join(conflicts)))
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
        frappe.throw(frappe._("ERPNext v16 must be installed before HERO Integration."))
    try:
        from erpnext import __version__ as erpnext_version
    except ImportError:
        frappe.throw(frappe._("ERPNext v16 must be importable before HERO Integration."))
    if not str(erpnext_version).startswith("16."):
        frappe.throw(frappe._("HERO Integration requires ERPNext {0}.").format(ERPNext_VERSION_CONSTRAINT))
    missing = [doctype for doctype in _REQUIRED_ERPNext_DOCTYPES if not frappe.db.exists("DocType", doctype)]
    if missing:
        frappe.throw(frappe._("ERPNext schema is incomplete: {0}.").format(', '.join(missing)))
    from hero_integration.integrations.erpnext.project_notes import NativeProjectNotesError, require_native_project_notes

    try:
        require_native_project_notes(frappe.get_meta("Project"))
    except NativeProjectNotesError as error:
        frappe.throw(str(error))
