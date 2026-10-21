"""Map the former unbounded default priority to the RC16 midpoint once."""

from __future__ import annotations

import frappe


def execute() -> None:
    """Only the legacy default 100 has an unambiguous, lossless migration.

    Other historic values outside 0..10 could encode an intentional relative
    order. Leave those rows disabled for an administrator instead of silently
    inventing a new order.
    """

    frappe.db.set_value("HERO Frappe Mapping", {"priority": 100}, "priority", 5, update_modified=False)
    rows = frappe.get_all(
        "HERO Frappe Mapping", filters={"priority": ["not in", list(range(0, 11))]}, fields=["name"]
    )
    for row in rows:
        frappe.db.set_value("HERO Frappe Mapping", row.name, "status", "Disabled", update_modified=False)
        frappe.db.set_value(
            "HERO Frappe Mapping", row.name, "validation_summary",
            "Disabled by RC16: historic priority is outside 0..10 and needs explicit review.",
            update_modified=False,
        )
