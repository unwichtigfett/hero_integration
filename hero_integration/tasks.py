from __future__ import annotations

import frappe


def run_scheduled_sync() -> None:
    settings = frappe.get_single("HERO Integration Settings")
    if not settings.scheduled_sync_enabled:
        return
    from hero_integration.api import start_migration

    start_migration(action="Sync", dry_run=0, scheduled=True)
