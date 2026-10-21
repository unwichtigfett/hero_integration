from __future__ import annotations

import frappe


def run_scheduled_sync() -> None:
    settings = frappe.get_single("HERO Integration Settings")
    from hero_integration.services.polling import effective_mode, runtime_plan, scheduler_due

    mode = effective_mode(settings)
    # Older existing sites do not necessarily carry the later status field.
    # Missing history means no successful poll is known yet, not an exception
    # that could accidentally bypass the scheduler gate.
    last_success = getattr(settings, "last_successful_sync", None)
    if mode == "OFF" or not scheduler_due(settings, frappe.utils.now_datetime(), last_success):
        return
    if mode == "MONITOR":
        from hero_integration.services.polling import run_monitor

        run_monitor()
        return
    if runtime_plan(settings)["unsupported_sources"]:
        from hero_integration.services.polling import _record_event

        _record_event("AUTO_SYNC", "Skipped", "Schema-only polling source requires a supported runtime adapter; no HERO request was made.")
        return
    from hero_integration.api import start_scheduled_sync

    start_scheduled_sync()
