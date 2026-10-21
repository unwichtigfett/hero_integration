"""Durable internal administrator notices for newly evidenced field conflicts."""

from __future__ import annotations

import frappe


def configured_recipients(settings=None) -> list[str]:
    settings = settings or frappe.get_single("HERO Integration Settings")
    configured = {
        line.strip() for line in str(getattr(settings, "admin_notification_recipients", "") or "").splitlines()
        if line.strip()
    }
    if configured:
        return [
            user for user in sorted(configured)
            if frappe.db.exists("Has Role", {"parent": user, "role": "System Manager"})
            and frappe.db.get_value("User", user, "enabled")
        ]
    candidates = frappe.get_all(
        "Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent"
    )
    return [user for user in candidates if frappe.db.get_value("User", user, "enabled")]


def notify_conflict_once(link, fields: set[str], detail: str) -> bool:
    """Create Frappe Notification Log rows only for materially new evidence."""

    marker = f"Field conflict: {', '.join(sorted(fields))}"
    if getattr(link, "conflict_detail", "") == detail:
        return False
    recipients = configured_recipients()
    if not recipients:
        return False
    for user in recipients:
        frappe.get_doc({
            "doctype": "Notification Log", "for_user": user, "type": "Alert",
            "document_type": "HERO Integration Link", "document_name": link.name,
            "subject": marker, "email_content": detail,
        }).insert(ignore_permissions=True)
    return True
