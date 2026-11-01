"""Helpdesk references ERPNext Customers/Projects directly; it keeps no copies (2026-11-01)."""
from __future__ import annotations

import frappe


def profile_status() -> tuple[bool, str]:
    apps = set(frappe.get_installed_apps())
    if not {"erpnext", "helpdesk"}.issubset(apps):
        return False, "Helpdesk profile needs both ERPNext and Helpdesk."
    if not frappe.db.exists("DocType", "HD Customer"):
        return False, "The installed Helpdesk app has no HD Customer DocType."
    if frappe.db.get_single_value("ERPNext HD Settings", "enabled"):
        return False, ("Disable ERPNext HD Settings: it mirrors every ERPNext Customer as an HD Customer; "
                       "tickets reference the ERPNext Customer and Project directly.")
    return True, "Tickets reference the ERPNext Customer and Project directly; no HD Customer copies."


def ticket_migration_status() -> str:
    return "DISABLED_NO_APPROVED_HERO_TICKET_CONTRACT"
