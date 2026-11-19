"""Dispatch persisted import work and bound sync work without changing their write authority.

Only the scheduler decides when to call the scheduled operations. Existing
queue paths remain callable for jobs created before an upgrade.
"""
import frappe


def execute(operation, *, run_name="", **context):
    if operation == "migration":
        from hero_integration.services import file_migration, migration

        action = frappe.db.get_value("HERO Integration Run", run_name, "action")
        if not action:
            raise frappe.DoesNotExistError("HERO migration run is missing.")
        if action in file_migration.RUN_ACTIONS:
            return file_migration._execute(run_name)
        return migration._execute_run(run_name)

    from hero_integration.services import sync_runtime_adapter as adapter

    if operation == "manual_sync":
        return adapter.run_pending_sync(context["expected_plan_generation"])
    if operation == "one_shot":
        return adapter.run_one_shot(**context)
    if operation == "scheduled_sync":
        return adapter.run_scheduled_auto_sync()
    if operation == "monitor":
        from hero_integration.services import polling, run_reservation

        # This named lock survives the monitor's DB rollbacks. Do not hold a
        # row lock across HTTP. Queued imports are already visible at admission.
        if not run_reservation.acquire(frappe):
            raise adapter.RuntimeBridgeError("Another HERO operation owns the execution slot.")
        try:
            active = run_reservation.locked_legacy_slot(frappe)
            if (run_reservation.legacy_slot_is_active(frappe, active)
                    or run_reservation.locked_pending_migrations(frappe)
                    or run_reservation.locked_b8_runs(frappe, frappe.local.site)):
                raise adapter.RuntimeBridgeError("Another HERO operation owns the execution slot.")
            frappe.db.rollback()
            return polling.run_monitor()
        finally:
            run_reservation.release(frappe)
    raise ValueError("Unknown HERO execution operation.")
