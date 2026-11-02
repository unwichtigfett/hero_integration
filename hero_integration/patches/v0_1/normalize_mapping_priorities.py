"""Map the former unbounded default priority to the RC16 midpoint once."""

from __future__ import annotations

import frappe


def execute() -> None:
    """Migrate only legacy priority 100 losslessly.
    Disable other out-of-range values for review rather than inventing their order."""

    # This historical patch bypasses DocType validation. Do not silently
    # rewrite or disable an active rule referenced by saved mapping JSON.
    active = frappe.get_all("HERO Frappe Mapping", filters={"status": "Active"}, fields=["name", "priority"])
    if any(type(row.priority) is not int or row.priority not in range(0, 11) for row in active):
        frappe.throw(frappe._("Active field-rule priorities require explicit revision-safe reconciliation before migration."))
    frappe.db.set_value("HERO Frappe Mapping", {"priority": 100, "status": ["!=", "Active"]},
                        "priority", 5, update_modified=False)
    rows = frappe.get_all(
        "HERO Frappe Mapping", filters={"priority": ["not in", list(range(0, 11))],
                                        "status": ["!=", "Active"]}, fields=["name"]
    )
    for row in rows:
        frappe.db.set_value("HERO Frappe Mapping", {"name": row.name, "status": ["!=", "Active"]},
                            "status", "Disabled", update_modified=False)
        frappe.db.set_value(
            "HERO Frappe Mapping", {"name": row.name, "status": ["!=", "Active"]}, "validation_summary",
            "Disabled by RC16: historic priority is outside 0..10 and needs explicit review.",
            update_modified=False,
        )
