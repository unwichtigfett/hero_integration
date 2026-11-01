"""Project form: HERO values appear as ordinary ERPNext fields (owner decision 2026-11-01).

HERO custom fields keep their data and definitions; only Property Setters change
labels, visibility, read-only state and order, so the HERO schema checks and the
field-wise sync are unaffected.  The native Project Type holds the trade (Gewerk).
"""

from __future__ import annotations

import json

import frappe

# fieldname -> label shown in the form (HERO-owned, therefore read-only).
SHOWN = {
    "custom_hero_related_project_id": "Project ID",
    "custom_hero_display_id": "Project Nr.",
    "custom_hero_source_created": "Creation",
    "custom_hero_source_modified": "Modified",
    "custom_hero_address_full_address": "Project Address",
    "custom_hero_observed_status_name": "Phase",
    "custom_hero_partner_notes": "Notes",
}
# Edited in ERPNext and sent with the Project's "Send to HERO" button.
EDITABLE = {"custom_hero_partner_notes"}
HIDDEN = (
    "custom_hero_source_id", "custom_hero_project_number", "custom_hero_customer_title",
    "custom_hero_customer_full_name", "custom_hero_project_type", "custom_hero_project_type_copy",
    "custom_hero_observed_status_id", "custom_hero_observed_status_status_code",
    "custom_hero_observed_status_short_name", "custom_hero_observed_status_step_id",
    "custom_hero_observed_status_created", "custom_hero_observed_status_modified",
)
# Placement: (field, after) applied in this order to the current field order.
PLACEMENT = (
    ("custom_hero_related_project_id", "project_name"),
    ("custom_hero_display_id", "custom_hero_related_project_id"),
    ("custom_hero_observed_status_name", "project_type"),
    ("custom_hero_source_created", "column_break_5"),
    ("custom_hero_source_modified", "custom_hero_source_created"),
    ("custom_hero_address_full_address", "custom_hero_source_modified"),
    ("custom_hero_partner_notes", "notes"),
)


def setup_project_form() -> dict:
    """Create the trade Project Types, the HERO Writeback role and the Project form Property Setters (idempotent)."""
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    from hero_integration.services.project_naming import TRADE_CATALOG

    if not frappe.db.exists("Role", "HERO Writeback"):
        frappe.get_doc({"doctype": "Role", "role_name": "HERO Writeback", "desk_access": 1}).insert(ignore_permissions=True)
    created_types = []
    for trade_name in TRADE_CATALOG.values():
        if not frappe.db.exists("Project Type", trade_name):
            frappe.get_doc({"doctype": "Project Type", "project_type": trade_name}).insert(ignore_permissions=True)
            created_types.append(trade_name)
    meta = frappe.get_meta("Project")
    present = {field.fieldname for field in meta.fields}
    for fieldname, label in SHOWN.items():
        if fieldname in present:
            make_property_setter("Project", fieldname, "label", label, "Data", validate_fields_for_doctype=False)
            read_only = "0" if fieldname in EDITABLE else "1"
            make_property_setter("Project", fieldname, "read_only", read_only, "Check", validate_fields_for_doctype=False)
            make_property_setter("Project", fieldname, "hidden", "0", "Check", validate_fields_for_doctype=False)
    for fieldname in HIDDEN:
        if fieldname in present:
            make_property_setter("Project", fieldname, "hidden", "1", "Check", validate_fields_for_doctype=False)
    order = field_order([field.fieldname for field in meta.fields])
    make_property_setter("Project", None, "field_order", json.dumps(order), "Data", for_doctype=True,
                         validate_fields_for_doctype=False)
    frappe.clear_cache(doctype="Project")
    return {"project_types_created": created_types, "fields_shown": len(SHOWN), "fields_hidden": len(HIDDEN)}


def field_order(current: list[str]) -> list[str]:
    """Move the shown HERO fields to their place; every other field keeps its relative order."""
    order = list(current)
    for fieldname, after in PLACEMENT:
        if fieldname in order and after in order:
            order.remove(fieldname)
            order.insert(order.index(after) + 1, fieldname)
    return order


def project_type_for_trade(trade_code: str | None) -> str | None:
    """The native Project Type (Gewerk) of a HERO trade, if that Project Type exists."""
    from hero_integration.services.project_naming import TRADE_CATALOG

    name = TRADE_CATALOG.get((trade_code or "").upper())
    return name if name and frappe.db.exists("Project Type", name) else None
