"""One-time: German salutations used by the HERO salutation table (owner decision 2026-11-02)."""
import frappe


def execute():
    for name in ("Herr", "Frau", "Familie", "Eheleute"):
        if not frappe.db.exists("Salutation", name):
            frappe.get_doc({"doctype": "Salutation", "salutation": name}).insert(ignore_permissions=True)
