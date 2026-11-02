"""Task form: show who is responsible for the task in HERO."""

import frappe

from hero_integration.services.task_lifecycle import ASSIGNEE_FIELD


def setup_task_form() -> None:
    """Create the read-only HERO Assignee field on Task (idempotent)."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    if frappe.db.exists("Custom Field", {"dt": "Task", "fieldname": ASSIGNEE_FIELD}):
        return
    after = "custom_hero_due_date" if frappe.get_meta("Task").has_field("custom_hero_due_date") else "exp_end_date"
    create_custom_fields({"Task": [{
        "fieldname": ASSIGNEE_FIELD, "label": "HERO Assignee", "fieldtype": "Data", "read_only": 1,
        "insert_after": after, "in_standard_filter": 1,
        "description": "E-mail of the HERO user the task is assigned to. A matching ERPNext user gets the assignment.",
    }]}, update=False)
