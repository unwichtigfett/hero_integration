"""HERO project assignees ("Bearbeiter") as native ERPNext assignments (ToDo) on the Project.

Users are matched by the HERO user ID stored on the ERPNext user (people sync);
records of the older e-mail contract are matched by e-mail. Unknown people are skipped. Only assignments this
import created (marked in their description) are closed when HERO removes the
assignee; assignments made in ERPNext stay untouched. No e-mail is sent.
"""
from __future__ import annotations

MARKER = "Bearbeiter (Quelle)"


def assignees(record: dict) -> list[tuple[str, object]] | None:
    """("id", HERO user id) or ("email", e-mail) per assigned user; None when the record has no assignments."""
    if "project_match_assignments" not in record:
        return None
    result = []
    for row in record.get("project_match_assignments") or []:
        if not isinstance(row, dict):
            continue
        if type(row.get("user_id")) is int and row["user_id"] > 0:
            result.append(("id", row["user_id"]))
            continue
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        email = user.get("email")
        if isinstance(email, str) and "@" in email:
            result.append(("email", email.strip().lower()))
    return sorted(set(result), key=str)


def assignee_emails(record: dict) -> set[str] | None:
    found = assignees(record)
    return None if found is None else {value for kind, value in found if kind == "email"}


def sync(project: str, record: dict, frappe_api=None) -> dict:
    """Open one ToDo per matched HERO assignee; close the import's ToDos of removed assignees."""
    found = assignees(record)
    if found is None:
        return {"added": 0, "closed": 0, "unmatched": 0}
    if frappe_api is None:
        import frappe as frappe_api
    from hero_integration.integrations.hero_people import user_by_hero_user
    from hero_integration.services.task_lifecycle import _user_for
    from hero_integration.services.task_assignment_state import managed_assignment

    users = {key: (user_by_hero_user(key[1], source_instance=record.get('_hero_source_instance')) if key[0] == "id" else _user_for(key[1], frappe_api)) for key in found}
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
