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
    plan = runtime_plan(settings)
    if plan["unsupported_sources"]:
        # Visible instead of a silent hourly no-op: the configuration blocks every run.
        record_scheduler_outcome(mode, "Skipped", "Polling configuration is not supported ("
                                 + ", ".join(map(str, plan["unsupported_sources"])) + "); no HERO request was made.")
        return
    # Older existing sites do not necessarily carry the later status field.
    # Missing history means no successful poll is known yet, not an exception
    # that could accidentally bypass the scheduler gate.
    last_success = getattr(settings, "last_successful_sync", None)
    if not scheduler_due(settings, frappe.utils.now_datetime(), last_success, system_timezone=_system_timezone()):
        return
    try:
        with _polling_lock():
            _run(mode, record_scheduler_outcome)
    except _lock_timeout_error():
        record_scheduler_outcome(mode, "Skipped", "A previous polling run is still active; this run was skipped.")


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
