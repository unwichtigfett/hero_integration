from __future__ import annotations

import frappe


def run_scheduled_sync() -> None:
    settings = frappe.get_single("HERO Integration Settings")
    from hero_integration.services.polling import effective_mode

    mode = effective_mode(settings)
    if mode == "OFF":
        return
    if mode == "MONITOR":
        from hero_integration.services.polling import run_monitor

        run_monitor()
        return
    from hero_integration.api import start_scheduled_sync

    start_scheduled_sync()
