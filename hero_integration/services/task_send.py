"""Send a Task to HERO, both ways (owner decisions 2026-11-02).

One "Send to HERO" button per Task, built like the Project button: the preview
reads the live HERO task, shows what would change and refuses a field HERO
changed since the last import.  Sent are title, description, end date (HERO
"erledigen bis"), done and the assignee (an ERPNext user matched to a HERO user
by e-mail; HERO allows one assignee).  A task created in ERPNext on a HERO
project is created in HERO and linked.  Sending is one ``update_task`` mutation
behind the global mutation gate and the mutation audit; it is verified by
reading HERO again and the sync baseline is advanced so the next import sees no
change.  Only System Managers and the role "HERO Writeback" may send.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, time

import frappe

from hero_integration.services import task_lifecycle as life
from hero_integration.services.project_send import (SEND_ROLES, _decode, _hash, _mutation_client, _read_client,
                                                    _types)

READ_DOCUMENT = (
    "query HeroTaskSendRead($ids: [Int]) { tasks(ids: $ids, show_deleted: true) { id title comment due_date "
    "done_date target_user_id target_project_match_id is_deleted created } }"
)
MUTATION_DOCUMENT = (
    "mutation HeroTaskSend($task: TaskInput) { update_task(task: $task) { id title comment due_date done_date "
    "target_user_id target_project_match_id } }"
)
SENT_INPUTS = {"id", "title", "comment", "due_date", "done_date", "target_user_id", "target_project_match_id"}
# label, HERO input, baseline field of the inbound import
FIELDS = (
    ("Title", "title", "subject"),
    ("Description", "comment", "description"),
    ("End date", "due_date", life.END),
    ("Done", "done_date", life.DONE),
    ("Assignee", "target_user_id", life.ASSIGNEE),
)


class TaskSendError(ValueError):
    """The task cannot be sent safely; nothing was sent."""


def may_send() -> bool:
    return bool(set(SEND_ROLES) & set(frappe.get_roles()))


def preview(task: str, client=None) -> dict:
    """What a send would change, computed from current server and HERO state."""
    if not may_send():
        raise TaskSendError("Only System Managers and HERO Writeback users may send tasks to HERO.")
    document = frappe.get_doc("Task", task)
    frappe.has_permission("Task", "write", doc=document, throw=True)
    client = client or _read_client()
    link = _task_link(task)
    from hero_integration.sources.hero import GraphQLServiceSource
    from types import SimpleNamespace

    emails = GraphQLServiceSource(client, SimpleNamespace(), SimpleNamespace(), "default").user_emails()
    local = _local_values(document, emails)
    if link is None:
        project = _hero_project_id(document.project)
        if not local["title"]:
            raise TaskSendError("A task needs a subject to be created in HERO.")
        hero, baseline = {}, {}
        local["target_project_match_id"] = project
    else:
        hero = _read_hero(client, link.source_id)
        if hero.get("is_deleted"):
            raise TaskSendError("The task was deleted in HERO.")
        baseline = _decode(link.source_field_hashes)
    changes, conflicts = [], []
    hero_view = _hero_values(hero, emails)
    for label, key, base_field in FIELDS:
        if local.get(key, _UNSET) is _UNSET or local[key] == hero_view.get(key):
            continue
        change = {"field": key, "label": label, "hero": _show(key, hero_view.get(key), emails),
                  "new": _show(key, local[key], emails), "value": local[key]}
        hero_changed = link is not None and base_field in baseline and baseline[base_field] != _inbound_hash(
            base_field, hero, emails)
        (conflicts if hero_changed else changes).append(change)
    if link is None:
        changes.append({"field": "target_project_match_id", "label": "Project", "hero": None,
                        "new": document.project, "value": local["target_project_match_id"]})
    from hero_integration.api import _mutation_execution_state

    enabled = bool(_mutation_execution_state()["mutation_execution_enabled"])
    fingerprint = hashlib.sha256(json.dumps(
        [task, link.source_id if link else None, hero_view, changes], sort_keys=True, default=str).encode()).hexdigest()
    return {"task": task, "hero_id": link.source_id if link else None, "create": link is None,
            "changes": changes, "conflicts": conflicts, "fingerprint": fingerprint,
            "can_send": bool(changes) and not conflicts and enabled, "sending_enabled": enabled}


def send(task: str, fingerprint: str = "", confirmed=0, client=None, mutation_client=None) -> dict:
    """Send exactly the previewed changes once; verify and advance the sync baseline."""
    from hero_integration.api import _assert_mutation_execution_released, _finalize_mutation, _reserve_mutation

    _assert_mutation_execution_released()
    read_client = client or _read_client()
    current = preview(task, client=read_client)
    if confirmed not in (True, 1, "1", "true") or fingerprint != current["fingerprint"]:
        raise TaskSendError("HERO or ERPNext changed since the preview; open the preview again.")
    if current["conflicts"]:
        raise TaskSendError("A field was changed in HERO since the last import; it is not overwritten.")
    if not current["changes"]:
        raise TaskSendError("There is nothing to send.")
    snapshot = _require_schema()
    variables = {"task": {change["field"]: _wire(change["field"], change["value"], current) for change in current["changes"]}}
    if not current["create"]:
        variables["task"]["id"] = int(current["hero_id"])
    audit = _reserve_mutation(operation_name="update_task", schema_fingerprint=snapshot.schema_fingerprint,
                              mapping_revision=1, request_fingerprint=current["fingerprint"])
    try:
        returned = (mutation_client or _mutation_client()).execute_once(MUTATION_DOCUMENT, variables)
        result = returned.get("update_task") if isinstance(returned, dict) else None
        hero_id = result.get("id") if isinstance(result, dict) else None
        if type(hero_id) is not int or hero_id <= 0 or (not current["create"] and str(hero_id) != str(current["hero_id"])):
            raise TaskSendError("HERO did not confirm the task.")
        after = _read_hero(read_client, str(hero_id))
        from hero_integration.sources.hero import GraphQLServiceSource
        from types import SimpleNamespace

        emails = GraphQLServiceSource(read_client, SimpleNamespace(), SimpleNamespace(), "default").user_emails()
        stored = _hero_values(after, emails)
        for change in current["changes"]:
            if stored.get(change["field"]) != change["value"]:
                raise TaskSendError(f"HERO did not store {change['label']}.")
    except Exception:
        _finalize_mutation(audit, "Failed", "Task send not confirmed; no retry. Check the task in HERO before sending again.")
        raise TaskSendError("Sending to HERO could not be confirmed; nothing is retried automatically. "
                            "Check the task in HERO.") from None
    _apply_locally(task, after, emails, create=current["create"])
    _finalize_mutation(audit, "Succeeded", "Task sent and verified; values are not kept in the audit.")
    return {"status": "SENT", "hero_id": hero_id, "changes": [change["label"] for change in current["changes"]],
            "correlation_id": audit.correlation_id}


_UNSET = object()


def _local_values(task, emails: dict) -> dict:
    """ERPNext values in HERO terms (dates as local days, assignee as HERO user id)."""
    values = {"title": (task.subject or "").strip() or None, "comment": plain_text(task.description),
              "due_date": life._day(task.get(life.END)),
              "done_date": life._day(task.completed_on) if task.status == "Completed" else None}
    users = frappe.get_all("ToDo", filters={"reference_type": "Task", "reference_name": task.name, "status": "Open"},
                           pluck="allocated_to")
    users = sorted(set(users))
    if len(users) > 1:
        raise TaskSendError("HERO allows one assignee; the task has several open assignments in ERPNext.")
    if users:
        email = (frappe.db.get_value("User", users[0], "email") or "").strip().lower()
        ids = [user_id for user_id, address in emails.items() if address.strip().lower() == email]
        if len(ids) != 1:
            raise TaskSendError("The assigned ERPNext user has no HERO user with the same e-mail.")
        values["target_user_id"] = ids[0]
    elif task.status == "Completed" and task.get(life.ASSIGNEE):
        # ERPNext closes assignments when a task is completed; the HERO assignee stays.
        pass
    return values


def _hero_values(hero: dict, emails: dict) -> dict:
    from hero_integration.services.migration import _external_task_deadline

    return {"title": (hero.get("title") or "").strip() or None, "comment": (hero.get("comment") or "").strip() or None,
            "due_date": life._day(_external_task_deadline(hero.get("due_date"))),
            "done_date": life._day(_external_task_deadline(hero.get("done_date"))),
            "target_user_id": hero.get("target_user_id"),
            "target_project_match_id": hero.get("target_project_match_id")}


def _inbound_hash(base_field: str, hero: dict, emails: dict) -> str:
    """Hash of the value the inbound import would derive from the live HERO task."""
    from hero_integration.services.native_projections import _html

    if base_field == "subject":
        return _hash(base_field, (hero.get("title") or "").strip() or None)
    if base_field == "description":
        text = hero.get("comment")
        return _hash(base_field, _html(text.strip()) if isinstance(text, str) and text.strip() else None)
    from frappe.utils import getdate
    from hero_integration.services.migration import _external_task_deadline

    derived = life.source_values(hero, _external_task_deadline, emails, getdate())
    return _hash(base_field, derived.get(base_field))


def _wire(field: str, value, current: dict):
    """HERO input value: dates become offset datetimes at noon local time (today: now).

    HERO ignores null; an empty string clears a value (reopens a done task).
    """
    if value is None and field in ("comment", "due_date", "done_date"):
        return ""
    if field in ("due_date", "done_date") and value:
        from zoneinfo import ZoneInfo
        from frappe.utils import getdate, now_datetime

        zone = ZoneInfo(frappe.db.get_single_value("System Settings", "time_zone"))
        day = getdate(value)
        moment = now_datetime() if field == "done_date" and day == getdate() else datetime.combine(day, time(12))
        # HERO silently ignores a timestamp with microseconds (live test 2026-11-02).
        return moment.replace(microsecond=0, tzinfo=zone).isoformat()
    return value


def _show(field, value, emails):
    if field == "target_user_id" and value:
        return emails.get(value) or f"HERO user {value}"
    return value


def plain_text(value) -> str | None:
    """Text of a Text Editor value (inverse of the inbound <p>…<br>…</p>)."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</p>\s*<p[^>]*>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    return text or None


def _task_link(task: str):
    rows = frappe.get_all("HERO Integration Link",
                          filters={"source_system": "HERO", "source_type": "Task", "target_doctype": "Task",
                                   "target_name": task},
                          fields=["name", "source_id", "source_instance", "sync_status", "source_field_hashes",
                                  "target_field_hashes"])
    if len(rows) > 1:
        raise TaskSendError("The task is linked to several HERO tasks.")
    if not rows:
        return None
    link = rows[0]
    if link.sync_status != "Synchronized":
        raise TaskSendError(f"The HERO link is {link.sync_status}; resolve it before sending.")
    from hero_integration.client.settings import get_configuration

    if (link.source_instance or "default") != (get_configuration().source_instance or "default"):
        raise TaskSendError("The task belongs to another HERO account.")
    return link


def _hero_project_id(project: str | None) -> int:
    if not project:
        raise TaskSendError("Only tasks of a HERO project can be created in HERO.")
    rows = frappe.get_all("HERO Integration Link", filters={"source_system": "HERO", "source_type": "project",
                                                            "target_doctype": "Project", "target_name": project},
                          fields=["source_id", "sync_status"])
    if len(rows) != 1 or rows[0].sync_status != "Synchronized":
        raise TaskSendError("The task's project is not a synchronized HERO project.")
    return int(rows[0].source_id)


def _read_hero(client, hero_id) -> dict:
    data = client.query(READ_DOCUMENT, {"ids": [int(hero_id)]})
    data = data.get("data", data) if isinstance(data, dict) else {}
    rows = data.get("tasks") if isinstance(data, dict) else None
    row = rows[0] if isinstance(rows, list) and len(rows) == 1 else None
    if not isinstance(row, dict) or str(row.get("id")) != str(hero_id):
        raise TaskSendError("HERO did not return this task.")
    return row


def _require_schema():
    names = frappe.get_all("HERO GraphQL Schema Snapshot", filters={"analysis_status": "Validated"},
                           order_by="creation desc", pluck="name", limit_page_length=1)
    if not names:
        raise TaskSendError("No validated HERO schema snapshot is available.")
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", names[0])
    types = _types(json.loads(snapshot.snapshot_json))
    inputs = {field["name"] for field in (types.get("TaskInput") or {}).get("inputFields") or []}
    mutations = {field["name"] for entry in types.values() if entry.get("kind") == "OBJECT"
                 for field in entry.get("fields") or [] if field.get("name") == "update_task"}
    if not SENT_INPUTS <= inputs or not mutations:
        raise TaskSendError("The HERO schema no longer offers the task fields this button sends.")
    return snapshot


def _apply_locally(task: str, after: dict, emails: dict, *, create: bool) -> None:
    """Store HERO-owned values, then make the baseline equal to what the next import will see."""
    from frappe.utils import getdate
    from hero_integration.services.change_detection import encode, field_hashes
    from hero_integration.services.migration import _digest, _external_task_deadline
    from hero_integration.services.native_projections import _html

    derived = life.source_values(after, _external_task_deadline, emails, getdate())
    deadline = _external_task_deadline(after.get("due_date"))
    frappe.db.set_value("Task", task, {"custom_hero_due_date": deadline, life.ASSIGNEE: derived.get(life.ASSIGNEE)},
                        update_modified=False)
    document = frappe.get_doc("Task", task)
    comment = after.get("comment")
    source = {"subject": (after.get("title") or "").strip() or None,
              "description": _html(comment.strip()) if isinstance(comment, str) and comment.strip() else None,
              "project": document.project, "custom_hero_due_date": deadline, **derived}
    target = {"subject": document.subject, "description": document.description, "project": document.project,
              "custom_hero_due_date": document.get("custom_hero_due_date"), **life.target_values(document)}
    if source["description"] is None:
        source.pop("description"); target.pop("description")
    if create:
        version = frappe.get_all("HERO Integration Link", filters={"source_type": "Task", "sync_status": "Synchronized"},
                                 order_by="modified desc", pluck="mapping_version", limit_page_length=1)
        if not version:
            raise TaskSendError("No imported HERO task defines the mapping version for a new link.")
        from hero_integration.client.settings import get_configuration

        frappe.get_doc({
            "doctype": "HERO Integration Link", "source_system": "HERO", "source_type": "Task",
            "source_id": str(after["id"]), "source_instance": get_configuration().source_instance or "default",
            "sync_status": "Synchronized", "conflict_detail": "", "target_doctype": "Task", "target_name": task,
            "source_payload_hash": _digest(after), "target_payload_hash": _digest(target),
            "source_field_hashes": encode(field_hashes(source)), "target_field_hashes": encode(field_hashes(target)),
            "mapping_version": version[0], "last_synced_on": frappe.utils.now_datetime(),
        }).insert(ignore_permissions=True)
        return
    link = _task_link(task)
    base_source, base_target = _decode(link.source_field_hashes), _decode(link.target_field_hashes)
    base_source.update(field_hashes(source))
    base_target.update(field_hashes(target))
    frappe.db.set_value("HERO Integration Link", link.name,
                        {"source_field_hashes": encode(base_source), "target_field_hashes": encode(base_target)},
                        update_modified=False)
