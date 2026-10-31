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


# Results that leave a HERO record without an imported, current ERPNext target.
NOT_IMPORTED_RESULTS = ("BLOCKED_RECORD", "CONFLICT", "SKIPPED_AMBIGUOUS", "UNAVAILABLE", "PENDING_REVIEW")


def _notify(subject: str, detail: str, document_type: str, document_name: str) -> bool:
    recipients = configured_recipients()
    for user in recipients:
        frappe.get_doc({
            "doctype": "Notification Log", "for_user": user, "type": "Alert",
            "document_type": document_type, "document_name": document_name,
            "subject": subject, "email_content": detail,
        }).insert(ignore_permissions=True)
    return bool(recipients)


def notify_review(link, detail: str) -> bool:
    """Ask administrators to approve or reject one held HERO record."""
    if not link:
        return False
    subject = f"HERO {link.target_doctype} {link.target_name} awaits approval or rejection"
    return _notify(subject, detail, "HERO Integration Link", link.name)


def notify_run_summary(run_name: str) -> bool:
    """One notice per run listing every record that was not imported, grouped by reason."""
    rows = frappe.get_all(
        "HERO Integration Record",
        filters={"run": run_name, "result": ["in", list(NOT_IMPORTED_RESULTS)]},
        fields=["source_type", "result", "detail"], limit_page_length=0,
    )
    if not rows:
        return False
    groups: dict[tuple[str, str, str], int] = {}
    for row in rows:
        key = (row.source_type or "", row.result or "", (row.detail or "")[:200])
        groups[key] = groups.get(key, 0) + 1
    lines = [f"{count} × {source} {result}: {detail}" for (source, result, detail), count
             in sorted(groups.items(), key=lambda item: (-item[1], item[0]))]
    subject = f"HERO run {run_name}: {len(rows)} records were not imported"
    return _notify(subject, "\n".join(lines), "HERO Integration Run", run_name)
