from __future__ import annotations

from contextlib import nullcontext
import importlib

import frappe

POLLING_LOCK = "hero_integration_polling"


def _system_timezone() -> str | None:
    getter = getattr(getattr(frappe, "utils", None), "get_system_timezone", None)
    return getter() if callable(getter) else None


def _polling_lock():
    """One polling run per site at a time; a second scheduler tick skips instead of overlapping.

    The site lock needs an initialised site (always true for scheduler jobs); Frappe test
    doubles without a site context run without it.
    """
    if not getattr(getattr(frappe, "local", None), "site", None):
        return nullcontext()
    from frappe.utils.synchronization import filelock

    return filelock(POLLING_LOCK, timeout=1)


def _lock_timeout_error():
    try:
        from frappe.utils.file_lock import LockTimeoutError
    except ImportError:  # minimal Frappe test doubles only
        return ()
    return LockTimeoutError


def run_scheduled_sync() -> None:
    settings = frappe.get_single("HERO Integration Settings")
    from hero_integration.services.polling import effective_mode, record_scheduler_outcome, runtime_plan, scheduler_due

    mode = effective_mode(settings)
    if mode == "OFF":
        return
    if mode == "AUTO_SYNC":
        _auto_sync_tick(settings, record_scheduler_outcome)
        return
    plan = runtime_plan(settings)
    if plan["unsupported_sources"]:
        # Visible instead of a silent no-op on every tick: the configuration blocks every run.
        record_scheduler_outcome(mode, "Skipped", "Polling configuration is not supported ("
                                 + ", ".join(map(str, plan["unsupported_sources"])) + "); no HERO request was made.")
        return
    # Sites without this status field have no known successful poll.
    last_success = getattr(settings, "last_successful_sync", None)
    now = frappe.utils.now_datetime()
    if not scheduler_due(settings, now, last_success, system_timezone=_system_timezone()):
        return
    # The interval also counts from the last started poll, so a failing poll
    # is not retried on every one-minute tick.
    if not scheduler_due(settings, now, _sync_state("last_poll_started_on"), system_timezone=_system_timezone()):
        return
    if _active_run_blocks():
        record_scheduler_outcome(mode, "Skipped", "A HERO run is active; polling waits for the next interval.")
        return
    try:
        with _polling_lock():
            _record_poll_start(now)
            _run(mode, record_scheduler_outcome)
    except _lock_timeout_error():
        record_scheduler_outcome(mode, "Skipped", "A previous polling run is still active; this run was skipped.")


def run_scheduled_schema_refresh() -> None:
    """Reload the live HERO schema every configured interval; independent of the polling mode."""
    from hero_integration.services.schema_refresh import run_scheduled_schema_refresh as tick

    tick()


def _sync_state(field: str):
    db = getattr(frappe, "db", None)
    return db.get_single_value("HERO Integration Sync State", field) if db is not None else None


def _active_run_blocks() -> bool:
    """MONITOR and AUTO_SYNC never compete with a running migration for HERO."""
    active = _sync_state("active_run")
    if not active:
        return False
    from hero_integration.services.migration import run_lease_alive

    return run_lease_alive(active)


def _record_poll_start(now) -> None:
    db = getattr(frappe, "db", None)
    if db is not None:
        db.set_single_value("HERO Integration Sync State", "last_poll_started_on", now)
        db.commit()


def _run(mode: str, record_scheduler_outcome) -> None:
    if mode == "MONITOR":
        from hero_integration.services.polling import run_monitor

        run_monitor()
        return
    adapter = importlib.import_module("hero_integration.services.sync_runtime_adapter")
    bridge_error = getattr(adapter, "RuntimeBridgeError", ())
    try:
        result = adapter.run_scheduled_auto_sync()
    except bridge_error as error:
        # The bridge messages are fixed, reviewed texts without secrets or record data.
        record_scheduler_outcome("AUTO_SYNC", "Skipped", f"Automatic sync is blocked: {error}")
        return
    except Exception:
        frappe.log_error(title="HERO automatic sync failed")
        record_scheduler_outcome("AUTO_SYNC", "Failed", "Automatic sync failed; see the redacted server error log.")
        return
    if getattr(result, "status", None) == getattr(adapter, "BLOCKED_NO_B7_MATERIALIZED_RUNTIME", object()):
        record_scheduler_outcome("AUTO_SYNC", "Skipped",
                                 "Automatic sync is blocked: no materialized runtime is available; no HERO request was made.")


def _auto_sync_tick(settings, record_scheduler_outcome) -> str | None:
    """Run enabled areas in order, once per polling interval within the window.
    Queue one Execute migration at a time; each reads HERO without outbound writes."""
    from hero_integration import api
    from hero_integration.services.polling import runtime_plan, scheduler_due
    from hero_integration.services.sync_page import ACTIVE_STATES, AREAS

    plan = runtime_plan(settings)
    if plan["unsupported_sources"]:
        record_scheduler_outcome("AUTO_SYNC", "Skipped", "Polling configuration is not supported ("
                                 + ", ".join(map(str, plan["unsupported_sources"])) + "); no HERO request was made.")
        return None
    now = frappe.utils.now_datetime()
    timezone = _system_timezone()
    if not scheduler_due(settings, now, None, system_timezone=timezone):
        return None  # off, paused, no area enabled or outside the execution window
    if frappe.db.exists("HERO Integration Run", {"state": ["in", list(ACTIVE_STATES)]}):
        return None
    instance = str(getattr(settings, "hero_source_instance", "default") or "default").strip() or "default"
    for item in AREAS:
        if not plan["areas"].get(item.key):
            continue
        last = frappe.get_all("HERO Integration Run", filters={"action": item.run_action, "mode": "Execute"},
                              pluck="creation", order_by="creation desc", limit_page_length=1)
        if last and not scheduler_due(settings, now, last[0], system_timezone=timezone):
            continue
        services = api._sync_area_read_services(instance).get(item.operation) or []
        if not services:
            record_scheduler_outcome("AUTO_SYNC", "Skipped", api._sync_area_blocker(item, instance))
            continue
        try:
            with _polling_lock():
                result = api._start_migration(action=item.run_action, dry_run=0, scheduled=True,
                                              graphql_service=services[0]["service"], source_instance=instance)
        except _lock_timeout_error():
            return None
        except Exception:
            frappe.log_error(title="HERO automatic sync could not start a run")
            record_scheduler_outcome("AUTO_SYNC", "Failed", f"{item.label}: the automatic run could not be started; see the error log.")
            return None
        if result.get("run"):
            record_scheduler_outcome("AUTO_SYNC", "Started", f"{item.label}: run {result['run']} queued.",
                                     run=result["run"], source_instance=instance)
        # The run and its polling receipt must exist before enqueue_after_commit fires.
        frappe.db.commit()
        return result.get("run")
    return None
