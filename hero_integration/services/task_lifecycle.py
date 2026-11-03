"""Reconcile HERO task dates, completion and assignee field by field.

Start/end dates use HERO creation/due dates; done_date records completion.
Assignees match ERPNext users by email; anyone may assign anyone. Missing
baselines bootstrap only untouched local defaults; other values need review.
"""

from __future__ import annotations

from datetime import date, datetime

START, END, DONE, ASSIGNEE = "exp_start_date", "exp_end_date", "hero_done_on", "custom_hero_assignee"
AUTHOR = "custom_hero_author"  # Link to the ERPNext User who created the task in HERO (matched by e-mail)
FIELDS = (START, END, DONE, ASSIGNEE, AUTHOR)
ASSIGNEE_FIELD = ASSIGNEE


def _day(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def source_values(record: dict, local_instant, directory: dict[int, str], today: date, user_for=None) -> dict:
    """ERPNext-side values from a HERO task; a path HERO did not return stays omitted.

    ``user_for`` (e-mail -> User or None) enables the Created By field."""
    values = {}
    if user_for is not None and "author_user_id" in record:
        author = record.get("author_user_id")
        email = directory.get(author) if type(author) is int and author > 0 else None
        values[AUTHOR] = user_for(email.strip().lower()) if email else None
    created = local_instant(record.get("created")) if "created" in record else None
    due = local_instant(record.get("due_date")) if "due_date" in record else None
    if "due_date" in record:
        values[END] = _day(due)
    if "created" in record:
        start = _day(created)
        # ERPNext refuses a start after the end: a task created after its due date starts on it.
        if start and values.get(END) and start > values[END]:
            start = values[END]
        values[START] = start
    if "done_date" in record:
        done = _day(local_instant(record.get("done_date")))
        values[DONE] = min(done, today.isoformat()) if done else None
    if "target_user_id" in record:
        user = record.get("target_user_id")
        if type(user) is int and user > 0:
            values[ASSIGNEE] = (directory.get(user) or f"HERO user {user}").strip().lower()
        else:
            values[ASSIGNEE] = None
    return values


def target_values(task) -> dict:
    return {
        START: _day(task.get(START)),
        END: _day(task.get(END)),
        DONE: _day(task.get("completed_on")) if task.get("status") == "Completed" else None,
        ASSIGNEE: (task.get(ASSIGNEE) or None),
        AUTHOR: (task.get(AUTHOR) or None),
    }


def decide(field, source_hash, target_hash, source_base, target_base, target_value) -> str:
    """APPLY / CONFIRM / KEEP_LOCAL / CONFLICT for one lifecycle field."""
    if source_hash == target_hash:
        return "CONFIRM"
    if source_base is None or target_base is None:
        # No baseline yet: ERPNext still holds its default → take HERO's value.
        return "APPLY" if target_value is None else "CONFLICT"
    if source_hash == source_base:
        return "KEEP_LOCAL"
    if target_hash == target_base:
        return "APPLY"
    return "CONFLICT"


def insert_values(values: dict) -> dict:
    """Task fields for a new task."""
    fields = {field: values[field] for field in (START, END, ASSIGNEE, AUTHOR) if values.get(field)}
    if values.get(DONE):
        fields.update(status="Completed", completed_on=values[DONE])
    return fields


def apply(task, updates: dict) -> None:
    """Set the HERO values on the Task document (not saved)."""
    for field in (START, END, ASSIGNEE, AUTHOR):
        if field in updates:
            task.set(field, updates[field])
    if DONE in updates:
        if updates[DONE]:
            task.status = "Completed"
            task.completed_on = updates[DONE]
        elif task.status == "Completed":
            task.status = "Open"


def sync_assignment(task, previous_email: str | None, frappe_api=None) -> str:
    """Give the HERO assignee an open ERPNext assignment (ToDo) if a user has that e-mail.

    Returns a short note; an unknown e-mail never blocks the task import.
    """
    if frappe_api is None:
        import frappe as frappe_api
    from hero_integration.services.task_assignment_state import managed_assignment

    email = task.get(ASSIGNEE)
    with managed_assignment():
        if previous_email and previous_email != email:
            old_user = _user_for(previous_email, frappe_api)
            for name in frappe_api.get_all("ToDo", filters={"reference_type": "Task", "reference_name": task.name,
                                                             "allocated_to": old_user, "status": "Open"}, pluck="name") if old_user else []:
                todo = frappe_api.get_doc("ToDo", name)
                todo.status = "Cancelled"
                todo.save(ignore_permissions=True)
        if not email or task.status in ("Completed", "Cancelled"):
            return ""
        user = _user_for(email, frappe_api)
        if not user:
            return "No ERPNext user with the HERO assignee e-mail; the task stays unassigned."
        if frappe_api.db.exists("ToDo", {"reference_type": "Task", "reference_name": task.name,
                                         "allocated_to": user, "status": "Open"}):
            return ""
        frappe_api.get_doc({
            "doctype": "ToDo", "allocated_to": user, "reference_type": "Task", "reference_name": task.name,
            "description": task.subject, "date": task.get(END), "assigned_by": "Administrator",
        }).insert(ignore_permissions=True)
    return ""


def _user_for(email: str, frappe_api) -> str | None:
    if not email or "@" not in email:
        return None
    rows = frappe_api.db.sql("SELECT name FROM `tabUser` WHERE enabled=1 AND LOWER(email)=%s LIMIT 2", (email.lower(),))
    return rows[0][0] if len(rows) == 1 else None
