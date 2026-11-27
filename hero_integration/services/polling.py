"""Bounded polling modes; OFF never enters this module from the scheduler."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone as dt_timezone
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


VALID_MODES = {"OFF", "MONITOR", "AUTO_SYNC"}
# Polling reads the reviewed V1 source operations, unfiltered, of every area switched on
# on the Synchronization page.
POLLING_SOURCES = ("HeroCustomers", "HeroProjectMatches")
WINDOW_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d$")
MAX_FULL_INVENTORY_PAGES = 100


class PaginationProgressError(ValueError):
    pass


@dataclass(frozen=True)
class MonitorResult:
    customer_records: int
    project_records: int
    source_instance: str
    incomplete_sources: tuple[str, ...] = ()
    customer_next_offset: int = 0
    project_next_offset: int = 0
    offset_checkpoints: dict = field(default_factory=dict)
    rows_read: int | None = None
    observed_ids: tuple[str, ...] = ()
    conflicting_ids: tuple[str, ...] = ()
    reader_digest: str = ""
    page_size: int = 0
    fingerprints: dict = field(default_factory=dict)
    minimum_interval: float = 0


@dataclass(frozen=True)
class _PageCount:
    records: int
    next_offset: int
    incomplete: bool
    checkpoint: dict | None = None


def effective_mode(settings) -> str:
    mode = str(getattr(settings, "polling_mode", "") or "").upper()
    if mode in VALID_MODES:
        return mode
    return "AUTO_SYNC" if bool(getattr(settings, "scheduled_sync_enabled", 0)) else "OFF"


def runtime_plan(settings) -> dict[str, object]:
    """Normalize every offered polling setting without reaching HERO."""
    from hero_integration.services.sync_page import AREAS, BY_OPERATION, area_enabled

    areas = {item.key: area_enabled(settings, item) for item in AREAS}
    sources = [operation for operation in POLLING_SOURCES if areas[BY_OPERATION[operation].key]]
    strategy = str(getattr(settings, "polling_pagination_strategy", "OFFSET") or "OFFSET").upper()
    timezone = str(getattr(settings, "polling_timezone", "UTC") or "UTC").strip()
    full_inventory = bool(getattr(settings, "polling_full_inventory", 0))
    unsupported = _invalid_numeric_settings(settings)
    from hero_integration.services.offset_pagination import validate_overlap
    page_size = _bounded_value(getattr(settings, "polling_page_size", 100), 1, 1000, 100)
    read_mode = getattr(settings, "polling_read_mode", "") or "NORMAL"
    try:
        overlap = validate_overlap(getattr(settings, "polling_page_overlap", 0), page_size)
    except ValueError:
        overlap = 0
        unsupported.append("setting polling_page_overlap")
    if read_mode != "NORMAL":
        unsupported.append("Fast Mode requires a verified HERO change-discovery contract.")
    if strategy != "OFFSET":
        unsupported.append("pagination strategy " + strategy)
    if not _timezone_is_valid(timezone):
        unsupported.append("timezone " + timezone)
    return {
        "mode": effective_mode(settings), "sources": sources, "timezone": timezone,
        "paused": bool(getattr(settings, "sync_service_paused", 0)), "areas": areas,
        "execution_window": str(getattr(settings, "polling_execution_window", "") or "").strip(),
        "interval_minutes": _bounded_value(getattr(settings, "polling_interval_minutes", 60), 1, 1440, 60),
        "bootstrap": bool(getattr(settings, "polling_bootstrap", 0)),
        "page_size": page_size, "page_overlap": overlap, "read_mode": read_mode, "fast_available": False,
        "pagination_strategy": strategy, "allow_backfill": bool(getattr(settings, "polling_allow_backfill", 0)),
        "full_inventory": full_inventory, "max_pages": MAX_FULL_INVENTORY_PAGES if full_inventory else 1,
        "reconciliation": bool(getattr(settings, "polling_reconciliation", 0)),
        "retry_limit": _bounded_value(getattr(settings, "polling_retry_limit", 0), 0, 3, 0),
        "retry_backoff_seconds": _bounded_value(getattr(settings, "polling_retry_backoff_seconds", 30), 0, 3600, 30),
        "timeout_seconds": _bounded_value(getattr(settings, "polling_timeout_seconds", getattr(settings, "timeout_seconds", 30)), 1, 120, 30),
        "rate_limit": _bounded_value(getattr(settings, "polling_rate_limit", 30), 1, 10000, 30),
        "concurrency": _bounded_value(getattr(settings, "polling_concurrency", 1), 1, 10, 1),
        "notifications": bool(getattr(settings, "polling_notifications", 0)), "unsupported_sources": unsupported,
        "can_auto_sync": effective_mode(settings) == "AUTO_SYNC" and not unsupported
        and not getattr(settings, "sync_service_paused", 0),
        "watermarks": "Offsets resume incomplete bounded scans only; this does not claim a monotonic HERO cursor.",
        "reconciliation_policy": "Report-only; HERO absence never deletes ERPNext data.",
    }


def scheduler_due(settings, now: datetime, last_success: object = None, *, system_timezone: str | None = None) -> bool:
    """Bound automatic work by mode, configured timezone, window and interval.

    Frappe passes naive datetimes in the *system* timezone and stores Single
    Datetime values as strings; both are normalized before comparing.
    """
    plan = runtime_plan(settings)
    # Monitor reads only the Desk areas. Automatic plans may also contain
    # direct readers (e.g. employees) when every core Desk area is off.
    if plan["mode"] == "OFF" or plan["unsupported_sources"] or plan["paused"]:
        return False
    if plan["mode"] == "MONITOR" and not any(plan["areas"].values()):
        return False
    current = _aware(now, system_timezone)
    if current is None:
        return False
    local_now = current.astimezone(ZoneInfo(str(plan["timezone"])))
    window = str(plan["execution_window"])
    if window:
        if not WINDOW_PATTERN.fullmatch(window):
            return False
        start, end = window.split("-", 1)
        clock = local_now.strftime("%H:%M")
        if not (start <= clock <= end if start <= end else clock >= start or clock <= end):
            return False
    interval = int(plan["interval_minutes"])
    previous = _aware(_as_datetime(last_success), system_timezone)
    if previous is None:
        return True
    elapsed = (current - previous).total_seconds()
    # A success time in the future (clock or timezone change) must not block polling indefinitely.
    return elapsed < 0 or elapsed >= interval * 60


def monitor(source, plan: dict[str, object] | None = None, *, customer_offset: int = 0, project_offset: int = 0, record_observer=None, offset_checkpoints=None) -> MonitorResult:
    """Read selected source pages with a finite, progress-checked policy."""
    plan = plan or {"sources": list(POLLING_SOURCES), "max_pages": MAX_FULL_INVENTORY_PAGES, "rate_limit": 10000}
    minimum_interval = 60 / max(int(plan.get("rate_limit", 10000)), 1)
    max_pages = max(int(plan.get("max_pages", MAX_FULL_INVENTORY_PAGES)), 1)
    page_size = plan.get("page_size")
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        page_size = None
    overlap = plan.get("page_overlap", 0)
    offset_checkpoints = offset_checkpoints or {}
    counts: dict[str, _PageCount] = {}
    if "HeroCustomers" in plan["sources"]:
        counts["HeroCustomers"] = _count_pages(source.customers, customer_offset, max_pages, minimum_interval, record_observer, "HeroCustomers", page_size, overlap, offset_checkpoints.get("HeroCustomers"))
    if "HeroProjectMatches" in plan["sources"]:
        counts["HeroProjectMatches"] = _count_pages(source.projects, project_offset, max_pages, minimum_interval, record_observer, "HeroProjectMatches", page_size, overlap, offset_checkpoints.get("HeroProjectMatches"))
    incomplete = tuple(name for name, item in counts.items() if item.incomplete)
    customers = counts.get("HeroCustomers", _PageCount(0, 0, False))
    projects = counts.get("HeroProjectMatches", _PageCount(0, 0, False))
    return MonitorResult(customers.records, projects.records, source.source_instance, incomplete,
                         customers.next_offset, projects.next_offset,
                         {name: item.checkpoint for name, item in counts.items() if item.checkpoint})


def run_monitor() -> None:
    """Scheduled MONITOR entrypoint: read-only transport plus bounded metadata."""
    import frappe

    settings = frappe.get_single("HERO Integration Settings")
    plan = runtime_plan(settings)
    if plan["mode"] != "MONITOR":
        return
    if plan["unsupported_sources"]:
        _record_event("MONITOR", "Skipped", "Polling configuration is not supported by the approved runtime contract; no HERO request was made.")
        return
    from hero_integration.services.sync_runtime_adapter import RuntimeBridgeError, run_materialized_monitor
    event = _record_event("MONITOR", "Started", "Reading approved page.")
    frappe.db.commit()  # Visible while the existing reader waits for its response.
    try:
        result = run_materialized_monitor(frappe_module=frappe)
        detail = "Materialized monitoring observed one bounded read page; full coverage is not claimed and no business records were written."
        frappe.db.set_value("HERO Polling Event", event, {"status": "Incomplete", "detail": detail,
            "customer_records": result.customer_records, "project_records": result.project_records,
            "rows_read": result.rows_read if result.rows_read is not None else result.customer_records + result.project_records})
        settings = frappe.get_single("HERO Integration Settings")
        settings.sync_status, settings.sync_error = "MONITOR incomplete", ""
        settings.save(ignore_permissions=True)
        if plan["notifications"]:
            _notify_polling_event(event, detail)
    except RuntimeBridgeError as error:
        frappe.db.rollback()
        frappe.db.set_value("HERO Polling Event", event, {"status": "Skipped", "detail": "Monitoring plan unavailable."})
        frappe.db.set_single_value("HERO Integration Settings", {"sync_status": "MONITOR skipped",
            "sync_error": str(error)})
    except Exception:
        frappe.db.rollback()
        settings = frappe.get_single("HERO Integration Settings")
        frappe.db.set_value("HERO Polling Event", event, {"status": "Failed", "detail": "Materialized monitoring failed."})
        settings.sync_status = "MONITOR failed"
        settings.sync_error = "Materialized monitoring failed or is not approved; see the redacted server error log."
        settings.save(ignore_permissions=True)
        frappe.log_error(title="HERO materialized monitoring failed")


def _incremental_monitor_selection(frappe, budget, sample, read_page, approval):
    from dataclasses import replace
    from hero_integration.services.test_environment import require_environment
    from hero_integration.services.sync_runtime_adapter import RuntimeBridgeError

    stream = sample.operation
    group = budget.monitor_checkpoint(stream)
    if group is None:
        selected, conflicts, offsets = [], set(), []
        previous_id, previous_fingerprint = None, None
        reader, rows_read, offset = None, 0, 0
        for _ in range(MAX_FULL_INVENTORY_PAGES):
            require_environment(frappe)
            started = time.monotonic()
            result = read_page(frappe_module=frappe, sample=replace(sample, offset=offset))
            # The adapter checked and locked the plan and binding after transport.
            # Release those locks before using the independently fenced budget connection.
            frappe.db.rollback()
            if result.source_instance != budget.source_instance or (reader and reader != result.reader_digest):
                raise RuntimeBridgeError("The approved MONITOR reader changed during selection.")
            reader = result.reader_digest
            ids = result.observed_ids
            if previous_id is not None and ids:
                repeated_page = int(ids[-1]) <= int(previous_id) and result.rows_read == result.page_size
                if int(ids[0]) < int(previous_id) or repeated_page:
                    raise RuntimeBridgeError("The approved MONITOR identity order did not advance.")
            rows_read += result.rows_read
            offsets.append(offset)
            if ids:
                if ids[0] == previous_id and result.fingerprints[ids[0]] != previous_fingerprint:
                    conflicts.add(ids[0])
                previous_id = ids[-1]
                previous_fingerprint = result.fingerprints[previous_id]
            conflicts.update(result.conflicting_ids)
            fresh = [key for key in budget.unobserved(stream, ids) if key not in selected]
            selected.extend(fresh[:budget.limit - len(selected)])
            if len(selected) == budget.limit or result.rows_read < result.page_size:
                break
            offset += result.rows_read
            if delay := max(0.0, result.minimum_interval - (time.monotonic() - started)):
                time.sleep(delay)
        else:
            raise RuntimeBridgeError("The bounded MONITOR scan reached its page ceiling before finding the next sample.")
        evidence = {"approval": approval, "reader": reader, "rows_read": rows_read, "offsets": offsets,
                    "conflicting_ids": sorted(conflicts.intersection(selected))}
        group = budget.incremental_group(stream, stream, [selected], observation=evidence)
    return budget.freeze(stream, group["ids"], evidence=group["observation"])


def run_manual_monitor(frappe, *, request_id, plan_key, expected_plan_generation, expected_source_revision,
                       stream, confirmed, expected_limit=None):
    """Observe a bounded selection under the shared execution slot. Replay reuses its receipts."""
    import hashlib
    import json
    from uuid import UUID
    from hero_integration.services.execution_budget import ExecutionBudget
    from hero_integration.services.migration_testing import configuration, validate_limit
    from hero_integration.services.sync_runtime_adapter import ManualMonitorSample, RuntimeBridgeError, run_materialized_monitor

    frappe.only_for("System Manager")
    if str(confirmed) != "1":
        raise RuntimeBridgeError("Manual MONITOR requires explicit approval.")
    from hero_integration.services.test_environment import require_environment
    require_environment(frappe)
    run_id = UUID(str(request_id)).hex
    generation, revision = int(expected_plan_generation), int(expected_source_revision)
    settings = frappe.get_single("HERO Integration Settings")
    source = str(settings.hero_source_instance or "default").strip()
    event_type = "HERO Polling Event"
    prior = frappe.db.get_value(event_type, run_id,
        ["manual_approval", "testing_max_actions", "testing_contract_version", "status"], as_dict=True)
    version = int(prior.testing_contract_version or 2) if prior else 3
    if version not in (2, 3):
        raise RuntimeBridgeError("Unsupported manual MONITOR contract version.")
    approval = hashlib.sha256(json.dumps([version, frappe.local.site, frappe.session.user, source, plan_key,
                                         generation, revision, stream]).encode()).hexdigest()
    if prior:
        if prior.manual_approval != approval:
            raise RuntimeBridgeError("This MONITOR request belongs to a different approval.")
        limit = prior.testing_max_actions
        validate_limit(limit)
    else:
        testing = configuration(settings, frappe_module=frappe)
        if not testing["testing_enabled"]:
            raise RuntimeBridgeError("Enable migration testing to start a bounded manual MONITOR test.")
        limit = testing["testing_max_actions"]
        if stream not in POLLING_SOURCES:
            raise RuntimeBridgeError("Select an approved MONITOR collection.")
        if expected_limit is not None and int(expected_limit) != limit:
            raise RuntimeBridgeError("The reviewed MONITOR test limit changed.")
        if effective_mode(settings) != "MONITOR" or revision != int(settings.source_binding_revision or 0):
            raise RuntimeBridgeError("A MONITOR test requires the reviewed MONITOR source revision.")
        event = frappe.get_doc({"doctype": event_type, "mode": "MONITOR", "status": "Started",
                                "source_instance": source, "manual_approval": approval, "testing_max_actions": limit,
                                "testing_contract_version": version,
                                "detail": "Reading the approved manual MONITOR sample."})
        event.insert(ignore_permissions=True, set_name=run_id)
        frappe.db.commit()  # Freeze the test setting before the first transport, including interrupted attempts.

    budget = ExecutionBudget("monitor", run_id, source, limit, version=version,
                             source_revision=revision if version == 3 else None)
    try:
        selection = budget.selection()
        if selection is None:
            if prior and prior.status in {"Incomplete", "Conflicts"}:
                raise RuntimeBridgeError("The recorded MONITOR sample has lost its frozen identities.")
            sample = ManualMonitorSample(plan_key, generation, revision, stream, limit)
            if version == 3:
                selection = _incremental_monitor_selection(frappe, budget, sample, run_materialized_monitor, approval)
            else:
                result = run_materialized_monitor(frappe_module=frappe, sample=sample)
                if result.source_instance != source:
                    raise RuntimeBridgeError("The approved MONITOR source changed before cohort selection.")
                evidence = {"approval": approval, "reader": result.reader_digest, "rows_read": result.rows_read,
                            "conflicting_ids": list(result.conflicting_ids)}
                selection = budget.freeze(stream, result.observed_ids, evidence=evidence)
        if selection["main_type"] != stream or selection["evidence"]["approval"] != approval:
            raise RuntimeBridgeError("The frozen MONITOR cohort belongs to another stream or approval.")
        evidence = selection["evidence"]
        ids, conflicts = selection["ids"], set(evidence["conflicting_ids"])
        if not prior or prior.status not in {"Incomplete", "Conflicts"}:
            for identity in ids:
                budget.reserve_record(stream, identity)
            if version == 3:
                from hero_integration.services.execution_budget import _check_source_revision
                if require_environment(frappe) != selection["environment"]:
                    raise RuntimeBridgeError("The frozen MONITOR environment changed.")
                _check_source_revision(frappe.db, source, revision)
            for identity in ids:
                budget.outcome(stream, identity, "conflict" if identity in conflicts else "processed")
            frappe.db.set_value(event_type, run_id, {
                "status": "Conflicts" if conflicts else "Incomplete", "rows_read": evidence["rows_read"],
                "customer_records": len(ids) if stream == "HeroCustomers" else 0,
                "project_records": len(ids) if stream == "HeroProjectMatches" else 0,
                "detail": "Manual MONITOR observed its frozen sample. Full change-stream coverage is unproven."})
            frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        # A terminal receipt with damaged evidence must never become reusable for a fresh read.
        if not prior or prior.status not in {"Incomplete", "Conflicts"}:
            frappe.db.set_value(event_type, run_id, {"status": "Failed", "detail": "Manual MONITOR sample could not be confirmed."})
            frappe.db.commit()
        raise
    return {"run": run_id, "coverage": "APPROVED_PAGE_ONLY" if version == 2 else "BOUNDED_ORDERED_SAMPLE", "operation": stream, "rows_read": evidence["rows_read"],
            "status": "Conflicts" if conflicts else "Observed", **budget.report()}


def record_scheduler_outcome(mode: str, status: str, detail: str, *, run: str = "", sync_run: str = "", source_instance: str = "") -> str | None:
    """Record each queued run; deduplicate repeated scheduler skips or failures."""
    import frappe

    settings = frappe.get_single("HERO Integration Settings")
    source_instance = source_instance or str(getattr(settings, "hero_source_instance", "") or "default")
    label = f"{mode} {status.lower()}"
    if run or sync_run:
        event = _record_event(mode, status, detail, run=run, sync_run=sync_run, source_instance=source_instance)
        # A started run ends the previous skip/failure state: the next identical failure is new again
        # and the page no longer shows the outdated error. Written directly; no Settings validation.
        frappe.db.set_single_value("HERO Integration Settings", {"sync_status": label, "sync_error": detail if status in {"Failed", "Incomplete"} else ""})
        return event
    if (getattr(settings, "sync_status", "") or "") == label and (getattr(settings, "sync_error", "") or "") == detail:
        return None
    event = _record_event(mode if mode in {"MONITOR", "AUTO_SYNC"} else "MONITOR", status, detail, source_instance=source_instance)
    settings.sync_status, settings.sync_error = label, detail
    settings.save(ignore_permissions=True)
    return event


def _record_event(mode: str, status: str, detail: str, result: MonitorResult | None = None, *,
                  run: str = "", sync_run: str = "", source_instance: str = "") -> str:
    import frappe

    if not source_instance and result is None:
        settings = frappe.get_single("HERO Integration Settings")
        source_instance = str(getattr(settings, "hero_source_instance", "") or "default")
    event = frappe.get_doc({"doctype": "HERO Polling Event", "mode": mode, "status": status, "run": run or None, "sync_run": sync_run or None,
        "source_instance": result.source_instance if result else source_instance, "customer_records": result.customer_records if result else 0,
        "project_records": result.project_records if result else 0, "detail": detail})
    event.insert(ignore_permissions=True)
    return event.name


def _version_confirms_inventory(data, reader_key):
    import json
    from hero_integration.services.sync_engine import inventory_slice_completed

    try:
        for field, old, new in json.loads(data)["changed"]:
            if field != "reader_states_json":
                continue
            previous = json.loads(old or "{}").get(reader_key) or {}
            current = json.loads(new)[reader_key]
            if current["status"] != "PARTIAL" or previous == current:
                return False
            checkpoint = json.loads(current.get("inventory_checkpoint_json") or "null")
            if not inventory_slice_completed(current["diagnostic"], checkpoint):
                return False
            if checkpoint is None:
                return True
            prior = json.loads(previous.get("inventory_checkpoint_json") or "null") or {}
            return checkpoint["next_offset"] > (prior.get("next_offset") or 0)
    except (KeyError, TypeError, ValueError, AttributeError):
        # Audit retention and older versions can leave the execution unprovable.
        pass
    return False


def _archived_inventory_progress(frappe_api, instance, since):
    """Recover older PARTIAL receipts from the final committed reader version of each run."""
    rows = frappe_api.db.sql("""
        SELECT event.name AS receipt, reader.read_service, reader.execution_scope, reader.state,
               version.data AS checkpoint_change
        FROM `tabHERO Polling Event` event
        JOIN `tabHERO Sync Run Ledger` anchor ON anchor.name = event.sync_run
            AND anchor.site_scope = %(site)s AND anchor.source_instance = event.source_instance
            AND anchor.automatic = 1
        JOIN `tabHERO Sync Run Ledger` reader ON reader.site_scope = anchor.site_scope
            AND reader.source_instance = anchor.source_instance AND reader.automatic = 1
            AND (reader.scheduler_cycle = NULLIF(anchor.scheduler_cycle, '') OR reader.name = anchor.name)
        LEFT JOIN tabVersion version ON version.name = (
            SELECT v.name FROM tabVersion v
            WHERE reader.state = 'PARTIAL' AND reader.execution_scope = 'INVENTORY'
                AND v.ref_doctype = 'HERO Sync State Ledger' AND v.docname = reader.site_scope
                AND v.creation BETWEEN reader.creation AND reader.modified
            ORDER BY v.creation DESC, v.name DESC LIMIT 1)
        WHERE event.source_instance = %(instance)s AND event.creation >= %(since)s
            AND event.mode = 'AUTO_SYNC' AND event.status = 'Incomplete' AND event.detail = 'PARTIAL'
            AND COALESCE(event.run, '') = '' AND COALESCE(event.manual_approval, '') = ''
    """, {"site": frappe_api.local.site, "instance": instance, "since": since}, as_dict=True)
    by_receipt = {}
    partials = set()
    for row in rows:
        receipt = row["receipt"]
        completed = row["state"] in ("SUCCEEDED", "BOUNDED_COMPLETE")
        if row["state"] == "PARTIAL" and row["execution_scope"] == "INVENTORY" and row["checkpoint_change"]:
            key = instance + "|" + (row["read_service"] or "")
            completed = _version_confirms_inventory(row["checkpoint_change"], key)
            if completed:
                partials.add(receipt)
        by_receipt[receipt] = completed and by_receipt.get(receipt, True)
    return sorted(receipt for receipt in partials if by_receipt[receipt])


def execution_summary(frappe_api, instance: str, since) -> dict[str, object]:
    """Count automatic executions once, using the linked run's current state.

    Manual MONITOR samples carry their own approval. Only scheduler receipts
    contribute here; skipped ticks are shown separately.
    """
    archived = _archived_inventory_progress(frappe_api, instance, since)
    params = {"instance": instance, "since": since}
    params.update({f"receipt_{i}": name for i, name in enumerate(archived)})
    placeholders = ", ".join(f"%(receipt_{i})s" for i in range(len(archived))) or "NULL"
    rows = frappe_api.db.sql(f"""
        SELECT outcome, COUNT(*) AS count, MAX(creation) AS latest,
               MAX(CASE WHEN outcome = 'InventoryProgress' THEN creation ELSE success_on END) AS success_on
        FROM (
            SELECT COALESCE(NULLIF(event.run, ''), event.name) AS identity,
                CASE
                    WHEN event.name IN ({placeholders}) THEN 'InventoryProgress'
                    WHEN COALESCE(event.run, '') = '' AND event.mode = 'AUTO_SYNC'
                        AND event.status = 'Succeeded' AND event.detail = 'PARTIAL'
                        AND COALESCE(event.sync_run, '') <> '' THEN 'InventoryProgress'
                    WHEN COALESCE(event.run, '') = '' THEN event.status
                    WHEN run.state IN ('Queued', 'Running') THEN 'Running'
                    WHEN run.state = 'Completed' THEN 'Succeeded'
                    WHEN run.state = 'Completed With Conflicts' THEN 'Conflicts'
                    WHEN run.state = 'Failed' THEN 'Failed'
                    WHEN run.state = 'Cancelled' THEN 'Cancelled'
                    ELSE 'Incomplete'
                END AS outcome,
                MAX(event.creation) AS creation,
                MAX(CASE WHEN COALESCE(event.run, '') = '' AND event.status = 'Succeeded'
                         THEN event.creation
                         WHEN run.state IN ('Completed', 'Completed With Conflicts')
                         THEN run.finished_on END) AS success_on
            FROM `tabHERO Polling Event` event
            LEFT JOIN `tabHERO Integration Run` run
                ON run.name = event.run AND run.source_instance = event.source_instance
                   AND run.mode = 'Execute'
            WHERE event.source_instance = %(instance)s AND event.creation >= %(since)s
                AND COALESCE(event.manual_approval, '') = ''
            GROUP BY identity, outcome
        ) executions
        GROUP BY outcome
    """, params, as_dict=True)
    counts = {row["outcome"]: row["count"] for row in rows}
    latest = max(rows, key=lambda row: str(row["latest"]), default=None)
    last_status = latest["outcome"] if latest else "Not started"
    return {
        "runs": sum(count for status, count in counts.items() if status != "Skipped"),
        "pass": counts.get("Succeeded", 0) + counts.get("InventoryProgress", 0),
        "partial_inventory": counts.get("InventoryProgress", 0), "fail": counts.get("Failed", 0),
        "incomplete": counts.get("Incomplete", 0) + counts.get("Started", 0),
        "skipped": counts.get("Skipped", 0), "running": counts.get("Running", 0),
        "conflicts": counts.get("Conflicts", 0), "cancelled": counts.get("Cancelled", 0),
        "last_status": "Succeeded" if last_status == "InventoryProgress" else last_status,
        "last_successful": max((row["success_on"] for row in rows if row["success_on"]), default=None),
    }


def _notify_polling_event(event_name: str, detail: str) -> None:
    import frappe
    from hero_integration.services.conflict_notifications import configured_recipients, recipient_diagnostic, recipient_text

    for user in configured_recipients():
        frappe.get_doc({"doctype": "Notification Log", "for_user": user, "type": "Alert",
            "document_type": "HERO Polling Event", "document_name": event_name,
            "subject": recipient_text(user, "HERO polling status"),
            "email_content": recipient_diagnostic(user, detail)}).insert(ignore_permissions=True)


def _count_pages(reader, offset: int, max_pages: int, minimum_interval: float, record_observer=None, operation: str = "", page_size: int | None = None, overlap: int = 0, offset_window=None) -> _PageCount:
    from hero_integration.services.offset_pagination import advance, validate_overlap
    from hero_integration.sources.hero import SourcePage
    if overlap:
        if offset and not offset_window:
            raise PaginationProgressError("Overlapping offset scans require their previous page checkpoint.")
        if page_size is None:
            raise PaginationProgressError("Page Overlap requires an approved offset page size.")
        actual = getattr(getattr(reader, "__self__", None), "contract", None)
        page_size = min(page_size, getattr(actual, "page_size", page_size))
        validate_overlap(overlap, page_size)
    count = pages = 0
    seen_offsets: set[int] = set()
    seen_identities: set[str] = set()
    current_offset = max(int(offset or 0), 0)
    last_request = 0.0
    while pages < max_pages:
        if current_offset in seen_offsets:
            raise PaginationProgressError("HERO pagination repeated an offset; monitoring stopped safely.")
        seen_offsets.add(current_offset)
        delay = minimum_interval - (time.monotonic() - last_request)
        if pages and delay > 0:
            time.sleep(delay)
        page = reader(current_offset)
        last_request = time.monotonic()
        pages += 1
        if page_size is not None and len(page.records) > page_size:
            raise PaginationProgressError("HERO response exceeds the configured page size.")
        if page.next_offset is not None and (type(page.next_offset) is not int or page.next_offset <= current_offset or not page.records):
            raise PaginationProgressError("HERO pagination did not advance; monitoring stopped safely.")
        physical_count = len(page.records)
        if overlap and (offset_window or page.next_offset is not None):
            if page.next_offset is not None and page.next_offset != current_offset + physical_count:
                raise PaginationProgressError("The reader does not expose contiguous offset positions.")
            try:
                window = advance(page.records, offset=current_offset, size=page_size, previous=offset_window, overlap=overlap)
            except ValueError as exc:
                raise PaginationProgressError(str(exc)) from exc
            next_offset = window.next_offset if page.next_offset is not None else None
            page = SourcePage(window.rows, next_offset, window.checkpoint, window.changed)
            offset_window = window.checkpoint
        identities = []
        for record in page.records:
            identity = record.get('id') if isinstance(record, dict) else None
            if not isinstance(identity, (int, str)) or isinstance(identity, bool) or not str(identity).strip():
                raise PaginationProgressError("HERO source identity is missing or invalid; monitoring stopped safely.")
            identities.append(str(identity))
        if len(set(identities)) != len(identities) or seen_identities.intersection(identities).difference(getattr(page, "changed_overlap_ids", ())):
            raise PaginationProgressError("HERO source identity repeated across pages; monitoring stopped safely.")
        seen_identities.update(identities)
        count += len(page.records)
        if record_observer:
            for record in page.records:
                record_observer(operation, record)
        if page.next_offset is None:
            if page_size is not None and physical_count == page_size:
                return _PageCount(count, current_offset, True)
            return _PageCount(count, 0, False)
        current_offset = page.next_offset
    return _PageCount(count, current_offset, True, offset_window if overlap else None)


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace(" ", "T", 1))
        except ValueError:
            return None
    return None


def _aware(value: datetime | None, system_timezone: str | None) -> datetime | None:
    """Naive values are in the Frappe system timezone (UTC when unknown)."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    zone = dt_timezone.utc
    if system_timezone:
        try:
            zone = ZoneInfo(system_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return None
    return value.replace(tzinfo=zone)


NUMERIC_LIMITS = {
    "polling_interval_minutes": (1, 1440), "polling_page_size": (1, 1000), "polling_retry_limit": (0, 3),
    "polling_retry_backoff_seconds": (0, 3600), "polling_timeout_seconds": (1, 120),
    "polling_rate_limit": (1, 10000), "polling_concurrency": (1, 10),
}


def _invalid_numeric_settings(settings) -> list[str]:
    """Out-of-range stored values fail closed instead of silently becoming defaults.

    An unset Int field reads as 0/None and keeps its documented default.
    """
    invalid = []
    for field, (minimum, maximum) in NUMERIC_LIMITS.items():
        value = getattr(settings, field, None)
        if value in (None, "", 0) and minimum > 0:
            continue
        try:
            number = int(value) if value not in (None, "") else minimum
        except (TypeError, ValueError):
            invalid.append("setting " + field)
            continue
        if isinstance(value, bool) or not minimum <= number <= maximum:
            invalid.append("setting " + field)
    return invalid


def _bounded_value(value: object, minimum: int, maximum: int, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if minimum <= result <= maximum else default


def _timezone_is_valid(value: str) -> bool:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    return True

