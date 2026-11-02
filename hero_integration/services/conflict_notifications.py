"""Durable internal administrator notices for newly evidenced field conflicts."""

from __future__ import annotations

import frappe
from html import escape


def recipient_text(user: str, message: str) -> str:
    """Use Frappe's user language, falling back to its system language."""
    from frappe.translate import get_user_lang

    return frappe._(message, lang=get_user_lang(user))


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

    field_list = ", ".join(sorted(fields))
    if getattr(link, "conflict_detail", "") == detail:
        return False
    recipients = configured_recipients()
    if not recipients:
        return False
    for user in recipients:
        frappe.get_doc({
            "doctype": "Notification Log", "for_user": user, "type": "Alert",
            "document_type": "HERO Integration Link", "document_name": link.name,
            "subject": recipient_text(user, "Field conflict: {0}").format(field_list),
            "email_content": recipient_text(user, detail),
        }).insert(ignore_permissions=True)
    return True


# Results that leave a HERO record without an imported, current ERPNext target.
NOT_IMPORTED_RESULTS = ("BLOCKED_RECORD", "CONFLICT", "SKIPPED_AMBIGUOUS", "UNAVAILABLE", "PENDING_REVIEW")


def _notify(subject: str, detail, document_type: str, document_name: str, *, subject_args=()) -> bool:
    recipients = configured_recipients()
    for user in recipients:
        frappe.get_doc({
            "doctype": "Notification Log", "for_user": user, "type": "Alert",
            "document_type": document_type, "document_name": document_name,
            "subject": recipient_text(user, subject).format(*subject_args),
            "email_content": detail(user) if callable(detail) else recipient_text(user, detail),
        }).insert(ignore_permissions=True)
    return bool(recipients)


def notify_review(link, detail: str) -> bool:
    """Ask administrators to approve or reject one held HERO record."""
    if not link:
        return False
    return _notify("HERO {0} {1} awaits approval or rejection", detail, "HERO Integration Link", link.name,
                   subject_args=(link.target_doctype, link.target_name))


def notify_run_summary(run_name: str) -> bool:
    """One notice per run listing every record that was not imported, grouped by reason."""
    rows = frappe.get_all(
        "HERO Integration Record",
        filters={"run": run_name, "result": ["in", list(NOT_IMPORTED_RESULTS)]},
        fields=["name", "source_type", "source_id", "result", "detail"], order_by="name asc", limit_page_length=0,
    )
    if not rows:
        return False
    def detail(user):
        return "<br>".join(
            " | ".join(escape(str(value or "")) for value in (
                row.name, row.source_type, row.source_id, row.result, recipient_text(user, row.detail or ""),
            ))
            for row in rows
        )
    return _notify("HERO run {0}: {1} records were not imported", detail, "HERO Integration Run", run_name,
                   subject_args=(run_name, len(rows)))


def notify_unmatched_assignees(run_name: str, emails: dict[str, int]) -> bool:
    """One notice per run: HERO assignees without an ERPNext user (tasks stay unassigned)."""
    if not emails:
        return False
    def detail(user):
        lines = [f"{escape(email)} | {recipient_text(user, '{0} task(s)').format(count)}"
                 for email, count in sorted(emails.items())]
        return (recipient_text(user, "Create an ERPNext user with the same e-mail; the next task run assigns the tasks.")
                + "<br>" + "<br>".join(lines))
    return _notify("HERO run {0}: {1} HERO assignees have no ERPNext user", detail, "HERO Integration Run", run_name,
                   subject_args=(run_name, len(emails)))
