"""Read the committed run, checkpoint and outcome owners for the administrator view."""
import json

import frappe
from frappe.utils import get_datetime, now_datetime

from hero_integration.services import file_migration
from hero_integration.services.execution_budget import ExecutionBudget, LEGACY_IDENTITY_LIMIT
from hero_integration.services.sync_page import ACTIVE_STATES

RUN = "HERO Integration Run"
SYNC = "HERO Sync Run Ledger"
MONITOR = "HERO Polling Event"
RUN_FIELDS = ["name", "action", "state", "creation", "modified", "started_on", "finished_on",
              "resume_phase", "resume_offset", "resume_record_index", "source_instance", "processed",
              "progress_counts_json", "execution_budget_run", "testing_max_actions", "testing_records_received",
              "testing_status", "testing_enabled", "budget_contract_version"]
OUTCOMES = ("processed", "created", "updated", "unchanged", "skipped", "conflicts", "errors", "rows_read")


def has_activity():
    return any(frappe.db.exists(dt, filters) for dt, filters in (
        (RUN, {"state": ["in", ACTIVE_STATES]}),
        (SYNC, {"site_scope": frappe.local.site, "state": "RUNNING"}),
        (MONITOR, {"mode": "MONITOR", "status": "Started"})))


def _budget(scope, name, source, limit):
    key = ExecutionBudget(scope, name, source, limit).budget_id
    used = frappe.db.get_value("HERO Execution Budget", key, "used") or 0
    return {"used": used, "limit": limit, "remaining": limit - used}


def _native_counts(names):
    # Distinct pairs prevent replay and overlap from increasing identity progress.
    # A result may name a customer and its linked objects; count only named targets.
    row = frappe.db.sql("""SELECT COUNT(DISTINCT IF(result='DEFERRED',NULL,source_type),NULLIF(source_id,'unknown')) AS processed,
        COUNT(DISTINCT IF(result='CREATED',target_doctype,NULL),target_name) AS created,
        COUNT(DISTINCT IF(result='UPDATED',target_doctype,NULL),target_name) AS updated,
        COUNT(DISTINCT IF(result='CONVERGED',target_doctype,NULL),target_name) AS unchanged,
        COUNT(DISTINCT IF(result LIKE 'SKIPPED%%',source_type,NULL),source_id) AS skipped,
        COUNT(DISTINCT IF(result IN ('CONFLICT','SKIPPED_AMBIGUOUS','PENDING_REVIEW'),source_type,NULL),source_id) AS conflicts,
        COUNT(DISTINCT IF(result IN ('BLOCKED_RECORD','UNAVAILABLE'),source_type,NULL),source_id) AS errors
        FROM `tabHERO Integration Record` WHERE run IN %(names)s
        AND technical_action IN ('hero_to_frappe','preview')""", {"names": tuple(names)}, as_dict=True)[0]
    return dict(row)


def _sum_counts(parts):
    return {key: sum(item[key] for item in parts) if all(item.get(key) is not None for item in parts)
            else None for key in OUTCOMES}


def _card(row, kind, action, status):
    now = now_datetime()
    modified = get_datetime(row.modified)
    start = row.get("started_on") or row.creation
    active = status in (*ACTIVE_STATES, "RUNNING", "Started", "Waiting for dependencies")
    return {"name": row.name, "kind": kind, "action": action, "state": status, "active": active,
            "start": start, "modified": modified, "phase": None, "checkpoint": None,
            "children": [], "budget": None, "pending": None, "counts": {},
            "elapsed": max(0, int(((now if active else get_datetime(row.get("finished_on") or modified))
                                   - get_datetime(start)).total_seconds())),
            "quiet": active and (now - modified).total_seconds() >= 300}


def migration(row):
    full = row.action == file_migration.FULL_ACTION
    chain = file_migration._full(row.name) if full else {}
    names = list(dict.fromkeys([*chain.get("completed_children", []), *chain.get("wave_children", {}).values(),
                                *([chain["child"]] if chain.get("child") else [])]))
    children = frappe.get_all(RUN, filters={"name": ["in", names], "source_instance": row.source_instance},
                              fields=RUN_FIELDS, limit_page_length=0) if names else []
    by_name = {child.name: child for child in children}
    steps = chain.get("steps") or file_migration.FULL_STEPS
    members = children if full else [row]
    card = _card(row, "migration", row.action, row.state)
    if full:
        by_action = {child.action: child for child in children}
        blocked = chain.get("blocked_areas", {})
        card["children"] = [{"action": action,
                             "state": by_action[action].state if action in by_action else
                                      "Blocked" if action in blocked else "Pending",
                             **({"reason": blocked[action]} if action in blocked else {})}
                            for action in steps]
        card["phase"] = ("Relationship reconciliation" if chain.get("step", 0) >= len(steps)
                         else steps[chain.get("step", 0)])
    discovery = chain.get('related_test_discovery') or {}
    if full and not children and discovery and not chain.get('related_test_ready'):
        index = discovery['operation_index']
        operations = file_migration.RELATED_DISCOVERY_OPERATIONS
        if chain.get('scoped_project_reads'):
            from hero_integration.services.scoped_test_reads import SCOPED_STEPS
            operations = SCOPED_STEPS
        if 0 <= index < len(operations):
            card['phase'] = operations[index]
            if operations[index] in ('HeroDocuments', 'HeroPictures'):
                card['phase'] += ' / HeroFileDetails'
            card['checkpoint'] = discovery['offset']
    parts = []
    native = [item for item in members if item.action not in file_migration.SLICE_ACTIONS]
    if native:
        counts = _native_counts([item.name for item in native])
        read_counts = [json.loads(item.progress_counts_json or "{}").get("rows_read") for item in native]
        counts["rows_read"] = sum(read_counts) if all(n is not None for n in read_counts) else None
        parts.append(counts)
    observed = [item.name for item in members if item.action in (
        file_migration.PRODUCT_ACTION, *file_migration.FILE_ACTIONS)]
    for item in members:
        if item.action in file_migration.SLICE_ACTIONS:
            counts = (json.loads(item.progress_counts_json) if item.progress_counts_json else
                      {key: (0 if item.state == "Queued" else None) for key in OUTCOMES})
            if item.name in observed and item.progress_counts_json:
                # These two readers report identities through page receipts below.
                counts["processed"] = 0
            parts.append(counts)
    card["counts"] = _sum_counts(parts)
    if full and not children and chain.get('scoped_project_reads'):
        card['counts']['rows_read'] = discovery.get('rows_read') if discovery else None
        if not discovery and not chain.get('related_test_ready'):
            card['phase'] = 'Project cohort selection'
    if observed and card["counts"]["processed"] is not None:
        observed_count = frappe.db.sql("""SELECT COUNT(DISTINCT source_type,source_id)
            FROM `tabHERO Integration Record` WHERE run IN %s AND technical_action='identity_checkpoint'""",
            (tuple(observed),))[0][0]
        card["counts"]["processed"] += observed_count
    if members:
        ids = [item.name for item in members]
        card["pending"] = frappe.db.count("HERO Pending Reference", {"origin_run": ["in", ids], "status": "Waiting"})
    else:
        card["pending"] = 0
    if full and chain.get("wave_children"):
        card["phase"] = " / ".join(action for action, name in chain["wave_children"].items()
                                  if name in by_name and by_name[name].state in ACTIVE_STATES) or card["phase"]
    active = by_name.get(chain.get("child")) if full else row
    if active:
        card["phase"] = active.resume_phase or active.action
        card["checkpoint"] = [active.resume_offset or 0, active.resume_record_index or 0] if (
            active.resume_phase and active.action not in file_migration.RUN_ACTIONS) else None
        if active.progress_counts_json and active.action in file_migration.SLICE_ACTIONS:
            card["checkpoint"] = json.loads(active.progress_counts_json).get("offset")
    if full and card["phase"] == "Relationship reconciliation" and card["pending"] and card["active"]:
        card["state"] = "Waiting for dependencies"
    if full and row.state in file_migration.SUCCESS and (
            card["pending"] or len(children) != len(names) or any(c.state in ACTIVE_STATES for c in children)
            or (steps == list(file_migration.NEW_FULL_STEPS)
                and (not chain.get("reconciliation_done") or set(by_action) != set(steps)
                     or any(c.state not in file_migration.SUCCESS for c in children)))):
        card.update(state="Waiting for dependencies", active=True)
    if not full and row.state == "Completed" and card["pending"]:
        card["state"] = "Waiting for dependencies"
    if children:
        card["modified"] = max([card["modified"], *(get_datetime(c.modified) for c in children)])
        card["quiet"] = card["active"] and (now_datetime() - card["modified"]).total_seconds() >= 300
    if row.execution_budget_run:
        card["budget"] = _budget("migration", row.execution_budget_run, row.source_instance, row.testing_max_actions if row.budget_contract_version in (2, 3) else min(LEGACY_IDENTITY_LIMIT, row.testing_max_actions))
    return card


def synchronization(row):
    card = _card(row, "sync", "Automatic sync" if row.automatic else "Manual synchronization", row.state)
    row_counts = frappe.db.sql("""SELECT COUNT(DISTINCT object_type,external_id) AS processed,
        COUNT(DISTINCT IF(outcome='CONFLICT',object_type,NULL),external_id) AS conflicts,
        SUM(updated_documents) AS updated, SUM(unchanged_documents) AS unchanged
        FROM `tabHERO Sync Change Event` WHERE run_id=%s AND site_scope=%s""", (row.name, frappe.local.site), as_dict=True)[0]
    card["counts"] = dict(row_counts, created=0, skipped=row.skipped_count, errors=int(row.state == "FAILED"),
                          rows_read=row.rows_read, updated=row_counts.updated or 0, unchanged=row_counts.unchanged or 0)
    card["phase"] = row.execution_scope or card["action"]
    card["checkpoint"] = row.confirmed_position or None
    if row.execution_scope in ("BOUND_OBJECTS", "DELTA"):
        card["budget"] = _budget("sync", row.name, row.source_instance, row.testing_max_actions if row.budget_contract_version == 2 else LEGACY_IDENTITY_LIMIT)
    return card


def cards(watched=()):
    """Return active runs plus the last observation of runs already on screen."""
    frappe.only_for("System Manager")
    rows = []
    # Restrict previously displayed names to this same permission-checked projection.
    for dt, state_field, states, filters, fields in (
        (RUN, "state", ACTIVE_STATES, {}, RUN_FIELDS),
        (SYNC, "state", ("RUNNING",), {"site_scope": frappe.local.site},
         ["name","creation","modified","state","automatic","source_instance","skipped_count",
          "execution_scope","confirmed_position","rows_read","budget_contract_version","testing_max_actions"]),
        (MONITOR, "status", ("Started",), {"mode":"MONITOR"},
         ["name","creation","modified","mode","status","rows_read"])):
        conditions = [{state_field: ["in", states]}]
        if watched:
            conditions.append({"name": ["in", watched]})
        found = frappe.get_all(dt, filters=filters, or_filters=conditions, fields=fields,
                               order_by="creation desc", limit_page_length=0)
        for row in found:
            if dt == RUN:
                rows.append(migration(row))
            elif dt == SYNC:
                rows.append(synchronization(row))
            else:
                card = _card(row, "monitor", "Monitor only", row.status)
                card["counts"] = {"rows_read": row.rows_read if row.status == "Incomplete" else None,
                                  "created": 0, "updated": 0}
                rows.append(card)
    return rows


def relationship_status(rows):
    """Project current relationship counts for the requested runs and their budget children."""
    names = tuple(row['name'] for row in rows)
    if not names:
        return
    counts = {name: {} for name in names}
    for origin, root, status, count in frappe.db.sql("""SELECT r.name, r.execution_budget_run, p.status, COUNT(*)
        FROM `tabHERO Integration Run` r JOIN `tabHERO Pending Reference` p ON r.name=p.origin_run
        WHERE (r.name IN %s OR r.execution_budget_run IN %s) AND p.status IN ('Waiting','Conflict')
        GROUP BY r.name, r.execution_budget_run, p.status""", (names, names)):
        for name in {origin, root} & counts.keys():
            counts[name][status] = counts[name].get(status, 0) + count
    for row in rows:
        status = counts[row['name']]
        row['relationships'] = status
        if row['state'] == 'Completed':
            if status.get('Conflict'):
                row['display_state'] = 'Completed With Conflicts'
            elif status.get('Waiting'):
                row['display_state'] = 'Waiting for dependencies'
