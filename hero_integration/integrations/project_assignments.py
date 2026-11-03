"""HERO project assignees ("Bearbeiter") as native ERPNext assignments (ToDo) on the Project.

Users are matched by e-mail; an unknown e-mail is skipped. Only assignments this
import created (marked in their description) are closed when HERO removes the
assignee; assignments made in ERPNext stay untouched. No e-mail is sent.
"""
from __future__ import annotations

MARKER = "Bearbeiter (Quelle)"


def assignee_emails(record: dict) -> set[str] | None:
    """The assigned users' e-mails, or None when the record did not include assignments."""
    if "project_match_assignments" not in record:
        return None
    emails = set()
    for row in record.get("project_match_assignments") or []:
        user = row.get("user") if isinstance(row, dict) else None
        email = user.get("email") if isinstance(user, dict) else None
        if isinstance(email, str) and "@" in email:
            emails.add(email.strip().lower())
    return emails


def sync(project: str, record: dict, frappe_api=None) -> dict:
    """Open one ToDo per matched HERO assignee; close the import's ToDos of removed assignees."""
    emails = assignee_emails(record)
    if emails is None:
        return {"added": 0, "closed": 0, "unmatched": 0}
    if frappe_api is None:
        import frappe as frappe_api
    from hero_integration.services.task_lifecycle import _user_for
    from hero_integration.services.task_assignment_state import managed_assignment

    users = {email: _user_for(email, frappe_api) for email in sorted(emails)}
    wanted = {user for user in users.values() if user}
    title = frappe_api.db.get_value("Project", project, "project_name") or project
    open_todos = frappe_api.get_all("ToDo", filters={"reference_type": "Project", "reference_name": project,
                                                     "status": "Open"},
                                    fields=["name", "allocated_to", "description"], limit_page_length=0)
    assigned = {row.allocated_to for row in open_todos}
    added = closed = 0
    with managed_assignment():
        for user in sorted(wanted - assigned):
            frappe_api.get_doc({"doctype": "ToDo", "allocated_to": user, "reference_type": "Project",
                                "reference_name": project, "description": f"{MARKER}: {title}",
                                "assigned_by": "Administrator"}).insert(ignore_permissions=True)
            added += 1
        for row in open_todos:
            if row.allocated_to not in wanted and (row.description or "").startswith(MARKER):
                todo = frappe_api.get_doc("ToDo", row.name)
                todo.status = "Cancelled"
                todo.save(ignore_permissions=True)
                closed += 1
    return {"added": added, "closed": closed, "unmatched": sum(1 for user in users.values() if not user)}
