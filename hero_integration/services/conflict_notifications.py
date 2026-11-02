"""Durable internal administrator notices for newly evidenced field conflicts."""

from __future__ import annotations

import re
from html import escape

import frappe

from hero_integration.services.localized_error import LocalizedError


_DIAGNOSTIC_PATTERNS = (
    (re.compile(r"Field conflict requires review: (.+)\."), "Field conflict requires review: {0}."),
    (re.compile(r"Task fields require review: (.+)"), "Task fields require review: {0}"),
    (re.compile(r"Another HERO run is active: (.+)\."), "Another HERO run is active: {0}."),
    (re.compile(r"HERO response omits the source of: (.+)\."), "HERO response omits the source of: {0}."),
    (re.compile(r"HERO cleared values that the no-clear policy does not write: (.+)\."),
     "HERO cleared values that the no-clear policy does not write: {0}."),
    (re.compile(r"A new target record would be created named (.+)\."),
     "A new target record would be created named {0}."),
    (re.compile(r"ERPNext rejected the record \(([A-Za-z_][A-Za-z0-9_]*)\); see Error Log\."),
     "ERPNext rejected the record ({0}); see Error Log."),
    (re.compile(r"Generated Project\.project_name exceeds its (\d+)-character field limit\."),
     "Generated Project.project_name exceeds its {0}-character field limit."),
    (re.compile(r"Project trade (.+) has no confirmed HERO-to-ERPNext title mapping\."),
     "Project trade {0} has no confirmed HERO-to-ERPNext title mapping."),
    (re.compile(r"(ProjectMatch\.[\w.]+) is required for the ERPNext project title\."),
     "{0} is required for the ERPNext project title."),
    (re.compile(r"(ProjectMatch\.[\w.]+) is required and must be an object\."),
     "{0} is required and must be an object."),
    (re.compile(r"(ProjectMatch\.[\w.]+) is not a complete formatted project address\."),
     "{0} is not a complete formatted project address."),
)

_CUSTOMER_REVIEW_REASONS = (
    "The HERO customer has no unambiguous ERPNext link; the Project has no customer.",
    "The HERO customer is deleted in HERO; the Project has no customer.",
    "The HERO customer is a contact person, not a customer; the Project has no customer.",
)


def recipient_text(user: str, message: str) -> str:
    """Use Frappe's user language, falling back to its system language."""
    from frappe.translate import get_user_lang

    return frappe._(message, lang=get_user_lang(user))


def recipient_format(user: str, message: str, *values: object) -> str:
    """Format a translated template safely, falling back to canonical text."""
    return LocalizedError(message, *values).translated(lambda value: recipient_text(user, value))


def recipient_diagnostic(user: str, message: str) -> str:
    """Translate known diagnostic templates while preserving field names and IDs."""
    translated = recipient_text(user, message)
    if translated != message:
        return translated
    for reason in _CUSTOMER_REVIEW_REASONS:
        if message.startswith(reason + " The project address is missing or incomplete ("):
            return recipient_text(user, reason) + " " + recipient_diagnostic(user, message[len(reason) + 1:])
    address = re.fullmatch(r"The project address is missing or incomplete \((.*)\); the title omits it\.", message)
    if address:
        nested = recipient_diagnostic(user, address.group(1))
        return LocalizedError("The project address is missing or incomplete ({0}); the title omits it.", nested).translated(
            lambda value: recipient_text(user, value))
    for prefix, template in (
        ("Recheck requested: ", "Recheck requested: {0}"),
        ("Awaits administrator approval: ", "Awaits administrator approval: {0}"),
    ):
        if message.startswith(prefix):
            nested = recipient_diagnostic(user, message[len(prefix):])
            return LocalizedError(template, nested).translated(lambda value: recipient_text(user, value))
    for pattern, template in _DIAGNOSTIC_PATTERNS:
        if match := pattern.fullmatch(message):
            return LocalizedError(template, *match.groups()).translated(lambda value: recipient_text(user, value))
    return message


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
            "subject": recipient_format(user, "Field conflict: {0}", field_list),
            "email_content": recipient_diagnostic(user, detail),
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
            "subject": recipient_format(user, subject, *subject_args),
            "email_content": detail(user) if callable(detail) else recipient_diagnostic(user, detail),
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
                row.name, row.source_type, row.source_id, row.result, recipient_diagnostic(user, row.detail or ""),
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
        lines = [f"{escape(email)} | {recipient_format(user, '{0} task(s)', count)}"
                 for email, count in sorted(emails.items())]
        return (recipient_text(user, "Create an ERPNext user with the same e-mail; the next task run assigns the tasks.")
                + "<br>" + "<br>".join(lines))
    return _notify("HERO run {0}: {1} HERO assignees have no ERPNext user", detail, "HERO Integration Run", run_name,
                   subject_args=(run_name, len(emails)))
