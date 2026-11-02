"""Send a Project's Notes and Phase to HERO (owner decision 2026-11-01).

One button per Project: the preview reads the live HERO values, shows what would
change and refuses a field that HERO changed since the last import.  Sending is
one ``update_project_match`` mutation behind the global mutation gate, an exact
preview fingerprint and the existing mutation audit; it is verified by reading
HERO again, and the sync baseline is advanced so the next import sees no change.
Only System Managers and the role "HERO Writeback" may send.
"""

from __future__ import annotations

import hashlib
import json

import frappe

from hero_integration.services.localized_error import LocalizedError
SEND_ROLES = ("System Manager", "HERO Writeback")
NOTES_FIELD = "custom_hero_partner_notes"
PHASE_ID_FIELD = "custom_hero_observed_status_id"
PHASE_NAME_FIELD = "custom_hero_observed_status_name"
# A phase is a ProjectStatusStep of the project's HERO project type; the project's
# statuses are its history.  The first live test (2026-11-01) showed that HERO
# ignores ProjectMatchInput.step_id; a new status is passed as the nested
# ProjectMatchInput.current_project_match_status { step_id }.
PHASE_INPUT = "current_project_match_status"
READ_DOCUMENT = (
    "query HeroProjectSendRead($id: Int) { project_match(project_match_id: $id) { id partner_notes "
    "current_project_match_status_id current_project_match_status { id name step_id step { name } } "
    "type { project_status_steps { id name sort_order is_active status_code } } } }"
)
MUTATION_DOCUMENT = (
    "mutation HeroProjectSend($project_match: ProjectMatchInput) { update_project_match(project_match: $project_match) "
    "{ id partner_notes current_project_match_status { step_id step { name } } } }"
)


class ProjectSendError(LocalizedError):
    """The project cannot be sent safely; nothing was sent."""


def may_send() -> bool:
    return bool(set(SEND_ROLES) & set(frappe.get_roles()))


def preview(project: str, phase_id=None, client=None) -> dict:
    """What a send would change, computed from current server and HERO state."""
    if not may_send():
        raise ProjectSendError("Only System Managers and HERO Writeback users may send projects to HERO.")
    document = frappe.get_doc("Project", project)
    frappe.has_permission("Project", "write", doc=document, throw=True)
    link = _bound_link(project)
    hero = _read_hero(client or _read_client(), link)
    steps = [row for row in ((hero.get("type") or {}).get("project_status_steps") or [])
             if isinstance(row, dict) and type(row.get("id")) is int and row.get("is_active") is not False]
    phases = [{"id": row["id"], "name": row.get("name")}
              for row in sorted(steps, key=_hero_step_order)]
    baseline = _decode(link.source_field_hashes)
    changes, conflicts = [], []

    local_notes = _text(document.get(NOTES_FIELD))
    hero_notes = _text(hero.get("partner_notes"))
    if local_notes != hero_notes:
        hero_changed = NOTES_FIELD in baseline and baseline[NOTES_FIELD] != _hash(NOTES_FIELD, hero_notes)
        (conflicts if hero_changed else changes).append(
            {"field": "partner_notes", "label": "Notes", "hero": hero_notes, "new": local_notes})

    current_phase = (hero.get("current_project_match_status") or {}).get("step_id")
    if phase_id not in (None, "", 0, "0"):
        phase_id = int(phase_id)
        if phase_id not in {phase["id"] for phase in phases}:
            raise ProjectSendError("The selected phase is not an active phase of this project's HERO project type.")
        if phase_id != current_phase:
            name = next(phase["name"] for phase in phases if phase["id"] == phase_id)
            current_name = _step_name(hero)
            changes.append({"field": PHASE_INPUT, "label": "Phase", "hero": current_name, "new": name,
                            "value": {"step_id": phase_id}})

    fingerprint = hashlib.sha256(json.dumps(
        [project, link.source_instance, link.source_id, hero_notes, current_phase, changes], sort_keys=True, default=str,
    ).encode()).hexdigest()
    from hero_integration.api import _mutation_execution_state

    return {
        "project": project, "hero_id": link.source_id,
        "hero": {"notes": hero_notes, "phase_id": current_phase,
                 "phase_name": _step_name(hero)},
        "phases": phases, "changes": changes, "conflicts": conflicts, "fingerprint": fingerprint,
        "can_send": bool(changes) and not conflicts and bool(_mutation_execution_state()["mutation_execution_enabled"]),
        "sending_enabled": bool(_mutation_execution_state()["mutation_execution_enabled"]),
    }


def send(project: str, phase_id=None, fingerprint: str = "", confirmed=0, client=None, mutation_client=None) -> dict:
    """Send exactly the previewed changes once; verify and advance the sync baseline."""
    from hero_integration.api import _assert_mutation_execution_released, _finalize_mutation, _reserve_mutation

    _assert_mutation_execution_released()
    read_client = client or _read_client()
    current = preview(project, phase_id, client=read_client)
    if confirmed not in (True, 1, "1", "true") or fingerprint != current["fingerprint"]:
        raise ProjectSendError("HERO or ERPNext changed since the preview; open the preview again.")
    if current["conflicts"]:
        raise ProjectSendError("A field was changed in HERO since the last import; it is not overwritten.")
    if not current["changes"]:
        raise ProjectSendError("There is nothing to send.")
    link = _bound_link(project)
    snapshot = _require_schema(PHASE_INPUT)
    variables = {"project_match": {"id": int(link.source_id)}}
    for change in current["changes"]:
        variables["project_match"][change["field"]] = change.get("value", change["new"])

    audit = _reserve_mutation(operation_name="update_project_match", schema_fingerprint=snapshot.schema_fingerprint,
                              mapping_revision=1, request_fingerprint=current["fingerprint"])
    try:
        returned = (mutation_client or _mutation_client()).execute_once(MUTATION_DOCUMENT, variables)
        # The client returns the GraphQL "data" object itself.
        result = returned.get("update_project_match") if isinstance(returned, dict) else None
        if not isinstance(result, dict) or str(result.get("id")) != str(link.source_id):
            raise ProjectSendError("HERO did not confirm the project.")
        after = _read_hero(read_client, link)
        for change in current["changes"]:
            if change["field"] == PHASE_INPUT:
                stored, expected = (after.get("current_project_match_status") or {}).get("step_id"), change["value"]["step_id"]
            else:
                stored, expected = after.get(change["field"]), change.get("value", change["new"])
            if (_text(stored) if change["field"] == "partner_notes" else stored) != expected:
                raise ProjectSendError("HERO did not store {0}.", change['label'])
    except Exception:
        _finalize_mutation(audit, "Failed", "Project send not confirmed; no retry. Check the project in HERO "
                                            "before sending again.")
        raise ProjectSendError("Sending to HERO could not be confirmed; nothing is retried automatically. "
                               "Check the project in HERO.") from None
    _apply_locally(project, link, current["changes"], after)
    _finalize_mutation(audit, "Succeeded", "Project Notes/Phase sent and verified; values are not kept in the audit.")
    return {"status": "SENT", "changes": [change["label"] for change in current["changes"]],
            "correlation_id": audit.correlation_id}


def _apply_locally(project, link, changes, after) -> None:
    """Store the confirmed HERO values and advance the baseline of exactly these fields."""
    from hero_integration.services.change_detection import encode

    updates = {}
    if any(change["field"] == "partner_notes" for change in changes):
        updates[NOTES_FIELD] = after.get("partner_notes")
    if any(change["field"] == PHASE_INPUT for change in changes):
        updates[PHASE_ID_FIELD] = str(after.get("current_project_match_status_id"))
        updates[PHASE_NAME_FIELD] = (after.get("current_project_match_status") or {}).get("name")
        updates["custom_hero_phase"] = _step_name(after)
        updates["custom_hero_observed_status_step_id"] = str((after.get("current_project_match_status") or {}).get("step_id"))
    frappe.db.set_value("Project", project, updates, update_modified=False)
    source, target = _decode(link.source_field_hashes), _decode(link.target_field_hashes)
    for field, value in updates.items():
        source[field] = target[field] = _hash(field, value)
    frappe.db.set_value("HERO Integration Link", link.name,
                        {"source_field_hashes": encode(source), "target_field_hashes": encode(target)},
                        update_modified=False)


def _hero_step_order(step) -> tuple:
    """HERO lists steps by status code, then sort order, then ID (matches the HERO screen)."""
    return tuple((step.get(key) is None, step.get(key) or 0) for key in ("status_code", "sort_order", "id"))


def _step_name(row) -> str | None:
    """HERO shows the step of the current status as the project's status."""
    status = row.get("current_project_match_status") or {}
    return (status.get("step") or {}).get("name") or status.get("name")


def _bound_link(project: str):
    rows = frappe.get_all("HERO Integration Link",
                          filters={"source_system": "HERO", "source_type": "project", "target_doctype": "Project",
                                   "target_name": project},
                          fields=["name", "source_id", "source_instance", "sync_status", "source_field_hashes",
                                  "target_field_hashes"])
    if len(rows) != 1:
        raise ProjectSendError("This project is not imported from HERO.")
    link = rows[0]
    if link.sync_status != "Synchronized":
        raise ProjectSendError("The HERO link is {0}; resolve it before sending.", link.sync_status)
    from hero_integration.client.settings import get_configuration

    if (link.source_instance or "default") != (get_configuration().source_instance or "default"):
        raise ProjectSendError("The project belongs to another HERO account.")
    return link


def _read_hero(client, link) -> dict:
    data = client.query(READ_DOCUMENT, {"id": int(link.source_id)})
    data = data.get("data", data) if isinstance(data, dict) else {}
    row = data.get("project_match") if isinstance(data, dict) else None
    if not isinstance(row, dict) or str(row.get("id")) != str(link.source_id):
        raise ProjectSendError("HERO did not return this project.")
    return row


def _require_schema(phase_input: str):
    """The latest validated schema must still offer the mutation and its fields."""
    names = frappe.get_all("HERO GraphQL Schema Snapshot", filters={"analysis_status": "Validated"},
                           order_by="creation desc", pluck="name", limit_page_length=1)
    if not names:
        raise ProjectSendError("No validated HERO schema snapshot is available.")
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", names[0])
    types = _types(json.loads(snapshot.snapshot_json))
    inputs = {field["name"] for field in (types.get("ProjectMatchInput") or {}).get("inputFields") or []}
    if not {"id", "partner_notes", phase_input} <= inputs:
        raise ProjectSendError("The HERO schema no longer offers the project fields this button sends.")
    return snapshot


def _types(obj) -> dict:
    stack = [obj]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            types = item.get("types")
            if isinstance(types, list) and types and isinstance(types[0], dict) and "kind" in types[0]:
                return {entry["name"]: entry for entry in types}
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return {}


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _hash(field, value):
    from hero_integration.services.change_detection import field_hashes

    return field_hashes({field: value})[field]


def _decode(value) -> dict:
    from hero_integration.services.change_detection import decode

    return dict(decode(value) or {})


def _read_client():
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.client.settings import get_configuration

    config = get_configuration()
    return HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=2)


def _mutation_client():
    from hero_integration.client.graphql_mutation import HeroMutationClient
    from hero_integration.client.settings import get_configuration

    config = get_configuration()
    return HeroMutationClient(config.endpoint, lambda: config.api_token, config.timeout_seconds)
