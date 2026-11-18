"""Local ERPNext project responsibility, separate from HERO observations."""
from __future__ import annotations

import frappe


FIELD = "custom_project_responsible"


def ensure_field():
    """Create-only local User Link; never unlock the HERO source-manager field."""
    field = frappe.get_meta("Project").get_field(FIELD)
    if field:
        if field.fieldtype != "Link" or field.options != "User" or field.read_only or field.permlevel:
            frappe.throw(frappe._("The ERPNext project responsible field has an incompatible definition."))
        return
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields({"Project": [{
        "fieldname": FIELD, "fieldtype": "Link", "options": "User", "label": "Project Responsible",
        "insert_after": "monitor_progress_tab", "no_copy": 1,
        "description": "Internal ERPNext responsibility. Does not change team membership, permissions or the HERO responsible.",
    }]}, update=False)


def validate_project_responsible(document, method=None):
    value = document.get(FIELD)
    if not value or (not document.is_new() and not document.has_value_changed(FIELD)):
        return
    # Eligibility metadata only; no private User fields are returned. Choosing
    # a responsible person grants no role, Project membership or ToDo assignment.
    user = frappe.db.get_value("User", value, ["enabled", "user_type"], as_dict=True)
    if not user or not user.enabled or user.user_type != "System User":
        frappe.throw(frappe._("Choose an enabled system user as the project responsible."))
