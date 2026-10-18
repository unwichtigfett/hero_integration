"""Helpdesk is deliberately delegated to its native ERPNext customer sync."""
from __future__ import annotations

import frappe


def profile_status() -> tuple[bool, str]:
    apps = set(frappe.get_installed_apps())
    if not {"erpnext", "helpdesk"}.issubset(apps):
        return False, "Helpdesk profile needs both ERPNext and Helpdesk."
    if not frappe.db.exists("DocType", "HD Customer"):
        return False, "The installed Helpdesk app has no HD Customer DocType."
    return True, "ERPNext owns native Customer to HD Customer synchronization."


def ticket_migration_status() -> str:
    return "DISABLED_NO_APPROVED_HERO_TICKET_CONTRACT"
