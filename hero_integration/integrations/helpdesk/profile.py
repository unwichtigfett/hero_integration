"""Helpdesk references ERPNext Customers/Projects; ERPNext owns the customer data (2026-11-12)."""
from __future__ import annotations

import frappe


def profile_status() -> tuple[bool, str]:
    apps = set(frappe.get_installed_apps())
    if not {"erpnext", "helpdesk"}.issubset(apps):
        return False, "Helpdesk profile needs both ERPNext and Helpdesk."
    if not frappe.db.exists("DocType", "HD Customer"):
        return False, "The installed Helpdesk app has no HD Customer DocType."
    if frappe.db.get_single_value("ERPNext HD Settings", "enabled"):
        return True, ("Helpdesk lists every ERPNext Customer through its linked HD Customer; "
                      "customers are created, changed and deleted in ERPNext only.")
    return True, "Tickets reference the ERPNext Customer and Project directly; Helpdesk lists no customers."


def ticket_migration_status() -> str:
    return "DISABLED_NO_APPROVED_HERO_TICKET_CONTRACT"
