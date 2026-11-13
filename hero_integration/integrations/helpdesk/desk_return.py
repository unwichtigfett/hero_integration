"""Helpdesk's own "Desk" entry opens the Helpdesk workspace inside the Desk. A Helpdesk setting
decides whether that stop is kept or the Desk's start page opens directly."""
from __future__ import annotations

import frappe

SETTINGS = "HD Settings"
FIELD = "hero_desk_via_helpdesk_workspace"
WORKSPACE_ROUTE, DESK_ROUTE = "/desk/helpdesk", "/desk"


def ensure_setting() -> None:
    """Create-only; the default keeps Helpdesk's own behavior."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    if not frappe.db.exists("DocType", SETTINGS) or frappe.db.exists("Custom Field", {"dt": SETTINGS, "fieldname": FIELD}):
        return
    create_custom_fields({SETTINGS: [{
        "fieldname": FIELD, "fieldtype": "Check", "default": "1", "insert_after": "favicon",
        "label": "Open the Helpdesk workspace when returning to the Desk",
        "description": "Off: the Desk entry in Helpdesk's Apps menu opens the Desk's start page directly.",
    }]}, update=False)
    # A single DocType does not take a new field's default: store it, so nothing changes on its own.
    frappe.db.set_single_value(SETTINGS, FIELD, 1)


def opens_desk_directly() -> bool:
    """False whenever the setting is missing or unreadable: Helpdesk then behaves as shipped."""
    try:
        if not frappe.get_meta(SETTINGS).has_field(FIELD):
            return False
        return not frappe.db.get_single_value(SETTINGS, FIELD)
    except Exception:
        return False
