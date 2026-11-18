"""Narrow metadata update for the configured seven-view Project workspace."""
from __future__ import annotations

import json

import frappe

from hero_integration.services.project_responsibility import FIELD, ensure_field


def compact_details_order(current):
    """Keep every field; only move the explicit section, name and responsibility."""
    order = list(current)
    required = ("customer_details", "project_name", "customer", "monitor_progress_tab", "custom_project_source_section")
    if len(order) != len(set(order)) or any(order.count(name) != 1 for name in required):
        raise ValueError("The configured Project field order is incomplete or ambiguous.")
    order.remove("customer_details")
    order.insert(0, "customer_details")
    order.remove("project_name")
    order.insert(order.index("customer"), "project_name")
    if FIELD in order:
        order.remove(FIELD)
    order.insert(order.index("monitor_progress_tab") + 1, FIELD)
    if "custom_hero_project_manager" in order:
        order.remove("custom_hero_project_manager")
        order.insert(order.index("custom_project_source_section") + 1, "custom_hero_project_manager")
    return order


def apply_compact_details():
    """Update only the existing layout profile; other Project sites are untouched."""
    frappe.only_for("System Manager")
    meta = frappe.get_meta("Project")
    names = [field.fieldname for field in meta.fields]
    if not {"custom_project_documents_tab", "custom_project_images_tab", "custom_project_source_section"} <= set(names):
        return {"state": "not_configured", "changed": False}
    stored = frappe.db.get_value("Property Setter", "Project-main-field_order", "value")
    if not stored:
        frappe.throw(frappe._("The configured Project field order could not be verified."))
    current = json.loads(stored)
    if [name for name in current if name != FIELD] != [name for name in names if name != FIELD]:
        frappe.throw(frappe._("The Project layout changed. Review its field order before continuing."))
    section_index = names.index("customer_details")
    if any(field.fieldtype in ("Section Break", "Column Break", "Tab Break") for field in meta.fields[:section_index]):
        frappe.throw(frappe._("The Project layout has an unexpected opening section."))
    target = compact_details_order(current)
    ensure_field()
    if set(target) != {field.fieldname for field in frappe.get_meta("Project").fields}:
        frappe.throw(frappe._("The Project layout changed. Review its field order before continuing."))
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    changed = False
    if current != target:
        make_property_setter("Project", None, "field_order", json.dumps(target), "Data",
            for_doctype=True, validate_fields_for_doctype=False)
        changed = True
    if meta.get_field("customer_details").label:
        make_property_setter("Project", "customer_details", "label", "", "Data",
            validate_fields_for_doctype=False)
        changed = True
    frappe.clear_cache(doctype="Project")
    return {"state": "configured", "changed": changed, "field_count": len(target)}
