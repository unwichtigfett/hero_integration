"""Project native Task descendants onto the approved task.comment assignment."""
from __future__ import annotations

import hashlib
import json

STATUS_LABELS = {"Open": "Ausstehend", "Working": "In Bearbeitung", "Pending Review": "Zur Prüfung",
                 "Overdue": "Überfällig", "Template": "Vorlage", "Completed": "Erledigt",
                 "Cancelled": "Abgebrochen"}


class ProjectionConflict(ValueError):
    """The confirmed generated portion can no longer be identified."""


def is_description(binding):
    return binding and (getattr(binding, "target_doctype", None), binding.target_field, binding.source_field) == (
        "Task", "description", "comment")


def is_assignment(item):
    return (item["source"] == {"doctype": "Task", "field": "description"}
            and item["input_path"] == "task.comment" and item["read_path"] == "comment"
            and item["resolver"] == "PLAIN_TEXT")


def section_for(frappe, root):
    """Read only descendants, one parent-filtered query per populated level."""
    frontier = [root]
    children = {}
    seen = {root}
    completed = 0
    while frontier:
        rows = frappe.get_all("Task", filters={"parent_task": ["in", frontier]},
                              fields=["name", "parent_task", "subject", "status"],
                              order_by="name", limit_page_length=0)
        frontier = []
        for row in rows:
            if row.name in seen:
                raise ProjectionConflict("Die native Aufgabenhierarchie enthält einen Zyklus.")
            frappe.has_permission("Task", "read", doc=row.name, throw=True)
            seen.add(row.name)
            children.setdefault(row.parent_task, []).append(row)
            completed += row.status == "Completed"
            frontier.append(row.name)
    if not children:
        return ""
    lines = [f"Unteraufgaben ({completed}/{len(seen)-1} erledigt):"]
    stack = [(row, 0) for row in reversed(children[root])]
    while stack:
        row, depth = stack.pop()
        subject = " ".join((row.subject or "").splitlines())
        lines.append("  " * depth + f"- {subject}: {STATUS_LABELS.get(row.status, row.status)}")
        stack.extend((child, depth + 1) for child in reversed(children.get(row.name, ())))
    return "\n".join(lines)


def normalized(text):
    return text.replace("\r\n", "\n").replace("\r", "\n") if isinstance(text, str) else text


def section_proof(section, body=None):
    if section:
        proof = {"lines": section.count("\n") + 1, "sha256": hashlib.sha256(section.encode()).hexdigest()}
        if isinstance(body, str) and (body == section or body.endswith("\n\n" + section)):
            proof["ambiguous_body_sha256"] = hashlib.sha256(body.encode()).hexdigest()
        return proof
    return None


def confirmed(binding):
    if not getattr(binding, "acknowledged_value_json", None) or not is_description(binding):
        return None
    value = json.loads(binding.acknowledged_value_json)
    return value.get("task_section_v1") if isinstance(value, dict) else None


def baseline_value(binding):
    value = json.loads(binding.acknowledged_value_json) if binding.acknowledged_value_json else None
    return value["description"] if confirmed(binding) else value


def pack(value, proof):
    return json.dumps({"task_section_v1": proof, "description": value} if proof else value, default=str)


def compose(body, section):
    return "\n\n".join(part for part in (normalized(body), section) if part)


def source_body(raw, proof):
    """Only remove the exact suffix attested by an independent outbound readback."""
    if not proof:
        return raw
    text = normalized(raw)
    if isinstance(text, str):
        pieces = text.rsplit("\n", proof["lines"])
        tail = "\n".join(pieces[-proof["lines"]:])
        if (section_proof(tail) == {k: proof[k] for k in ("lines", "sha256")}
                and hashlib.sha256(text.encode()).hexdigest() != proof.get("ambiguous_body_sha256")):
            if len(pieces) == proof["lines"]:
                return ""
            if pieces[0].endswith("\n"):
                return pieces[0][:-1]
    raise ProjectionConflict("Der bestätigte Unteraufgabenabschnitt wurde in HERO geändert oder entfernt.")


def clean_record(record, binding):
    proof = confirmed(binding)
    if proof and "comment" in record:
        return dict(record, comment=source_body(record["comment"], proof))
    return record


def roots_for(frappe, parents):
    roots = set()
    for name in parents:
        seen = set()
        while name:
            if name in seen:
                raise ProjectionConflict("Die native Aufgabenhierarchie enthält einen Zyklus.")
            seen.add(name)
            parent = frappe.db.get_value("Task", name, "parent_task")
            if not parent:
                roots.add(name)
            name = parent
    return roots


def observe(doc, method=None):
    """Wake the existing bound-task worker in the same transaction as the child save."""
    import frappe
    from hero_integration.services.binding_registry import incarnation, native_fingerprint
    from hero_integration.services.integration_events import record_output

    if frappe.flags.in_migrate or frappe.flags.in_uninstall:
        return
    previous = doc.get_doc_before_save()
    parent = doc.get("parent_task")
    old = previous.get("parent_task") if previous else None
    if method != "after_delete" and previous and all(
            doc.get(k) == previous.get(k) for k in ("subject", "status", "parent_task")):
        return
    owners = roots_for(frappe, {old, parent} - {None, ""})
    if method != "after_delete" and previous and old != parent:
        owners.add(doc.name)  # A formerly independent bound task entering/leaving the hierarchy.
    if not owners:
        return
    bindings = frappe.get_all("HERO Object Binding", filters={"site_scope": frappe.local.site,
        "target_doctype": "Task", "target_name": ["in", sorted(owners)], "lifecycle": "VALID"},
        fields=["name", "target_name", "target_incarnation", "source_instance", "object_type", "external_object_id"])
    for owner in bindings:
        task = frappe.get_doc("Task", owner.target_name)
        if owner.target_incarnation != incarnation(task.creation):
            continue
        if task.get("parent_task"):
            record_output(source_type=owner.object_type, source_id=owner.external_object_id,
                          source_instance=owner.source_instance, result="PENDING_REVIEW", action="task_hierarchy_review",
                          correlation_id=owner.name, target_doctype="Task", target_name=task.name,
                          detail="Die eigenständig gebundene Aufgabe ist jetzt eine native Unteraufgabe.")
            continue
        if not frappe.db.exists("HERO Field Binding", {"object_binding": owner.name,
                                  "target_field": "description", "source_field": "comment"}):
            continue
        transition = [owner.name, doc.name, str(doc.creation), str(doc.modified), method,
                      [previous.get(k) for k in ("subject", "status", "parent_task")] if previous else None,
                      [doc.get(k) for k in ("subject", "status", "parent_task")]]
        # db_set(update_modified=False) may repeat the exact same transition after a later delivery.
        # A fresh event is needed in that case; outbound always rebuilds the current projection.
        from uuid import uuid4
        stamp = str(uuid4()) if previous and previous.modified == doc.modified else ""
        event_id = hashlib.sha256(json.dumps([transition, stamp], default=str).encode()).hexdigest()
        record_output(source_type=owner.object_type, source_id=owner.external_object_id,
                      source_instance=owner.source_instance, result="OBSERVED", action="frappe_change_observed",
                      origin="frappe", correlation_id=owner.name, target_doctype="Task", target_name=task.name,
                      changed_fields=["description"], event_id=event_id,
                      native_state={"description": native_fingerprint(task.description, task.meta.get_field("description").fieldtype)})
