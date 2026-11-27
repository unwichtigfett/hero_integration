"""Data areas of the Synchronization page.

The page shows four business areas. Each area maps to one stored run action
(the run engine keeps its historic action keys), one HERO read operation and the
source type its records are linked under. Everything else, such as the GraphQL
service or the mapping rules, is resolved from the active mapping.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SyncArea:
    key: str
    label: str
    action_label: str
    run_action: str
    operation: str
    source_type: str
    setting: str
    description: str
    scope_note: str


AREAS: tuple[SyncArea, ...] = (
    SyncArea("contacts", "Contacts", "Contact Migration", "Customer Migration", "HeroCustomers", "customer",
             "sync_area_contacts", "Import contact information from HERO GraphQL.",
             "Contact and customer data with the objects, fields and relations of the active mapping."),
    SyncArea("projects", "Projects", "Project Migration", "Project Migration", "HeroProjectMatches", "project",
             "sync_area_projects", "Import project information from HERO GraphQL.",
             "Mapped project data. The referenced contacts are processed first in the same run."),
    SyncArea("events", "Events", "Event Migration", "Schedule Migration", "HeroCalendarEvents", "CalendarEvent",
             "sync_area_events", "Import calendar events from HERO GraphQL.",
             "Mapped appointment data with its project, participant and resource relations."),
    SyncArea("tasks", "Tasks", "Task Migration", "Projection Migration", "HeroTasks", "Task",
             "sync_area_tasks", "Import task information from HERO GraphQL.",
             "Mapped task fields with their approved relations and assignments."),
)
BY_KEY = {area.key: area for area in AREAS}
BY_RUN_ACTION = {area.run_action: area for area in AREAS}
BY_OPERATION = {area.operation: area for area in AREAS}
SUCCESS_STATES = ("Completed", "Completed With Conflicts")
ACTIVE_STATES = ("Queued", "Running")
CONFLICT_RESULTS = ("CONFLICT", "SKIPPED_AMBIGUOUS", "BLOCKED_RECORD")
POLLING_PERIOD_DAYS = 30


def manual_start_blockers(instance: str, frappe_module, *, area_key: str | None = None) -> dict[str, str]:
    """Return the remaining onboarding requirements for manual starts on this source."""
    blockers = {}
    for action, areas, message in (
        ("Employee Migration", ("projects", "tasks", "combined"), "Migrate Employees first"),
        ("Customer Migration", ("documents", "pictures", "combined"), "Migrate Contacts first"),
    ):
        if area_key is not None and area_key not in areas:
            continue
        rows = frappe_module.get_all(
            "HERO Integration Run",
            filters={"action": action, "mode": "Execute", "source_instance": instance, "inventory_sync_run": ["is", "not set"]},
            fields=["state"], order_by="creation desc, name desc", limit_page_length=1,
        )
        if not rows or rows[0]["state"] not in SUCCESS_STATES:
            for key in areas:
                if key == area_key or (area_key is None and key != "combined"):
                    blockers.setdefault(key, message)
    return blockers


def area(key: str) -> SyncArea:
    try:
        return BY_KEY[str(key or "").strip().lower()]
    except KeyError:
        raise ValueError("Unknown synchronization area.") from None


def area_enabled(settings, item: SyncArea) -> bool:
    """Area switches default to on; only an explicit 0 turns an area off."""
    value = getattr(settings, item.setting, None)
    return True if value in (None, "") else bool(int(value))


def action_label(run_action: str) -> str:
    item = BY_RUN_ACTION.get(run_action)
    return item.label if item else run_action


def mapping_scope(rows: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Objects and fields of the active mapping rules, counted without duplicates."""
    objects: dict[str, set[str]] = {}
    for row in rows:
        doctype = str(row.get("target_doctype") or "").strip()
        field = str(row.get("target_field") or "").strip()
        if doctype:
            fields = objects.setdefault(doctype, set())
            if field:
                fields.add(field)
    return {
        "object_count": len(objects),
        "field_count": sum(len(fields) for fields in objects.values()),
        "objects": [{"doctype": doctype, "fields": sorted(fields)} for doctype, fields in sorted(objects.items())],
    }

def scheduler_state(frappe, settings, plan, instance, scheduler_active):
    """Read committed configuration, a worker lease and the existing writeback queue."""
    from frappe.utils.background_jobs import get_job_status
    from redis.exceptions import RedisError
    from hero_integration.services.sync_engine import FrappeSyncLedger
    from hero_integration.services.sync_runtime_adapter import (
        _require_area_enabled, _require_confirmed_source_binding, _require_latest_validated_snapshot,
        _runtime_plan_service,
    )

    site = frappe.local.site
    ledger = FrappeSyncLedger(frappe)
    run = frappe.db.get_value(ledger.RUN, {"site_scope": site, "source_instance": instance,
                              "automatic": 1, "state": "RUNNING"}, "sync_run_id")
    processing = bool(run and ledger.lease_active(run))
    queue = None
    service = _runtime_plan_service(frappe)
    approved = service.current_approved(site, instance)
    if approved:
        try:
            queue = get_job_status("hero-auto-write-" + approved.plan_key)
        except RedisError:
            queue = "unavailable"
    ready = scheduler_active and plan["mode"] != "OFF" and not plan["paused"] and not plan["unsupported_sources"]
    reason = ""
    area = ""
    if ready:
        if not approved:
            reason = "No approved runtime plan exists for this site/source instance."
        else:
            try:
                _require_confirmed_source_binding(settings)
                # Validate existing references only. Rendering the page issues no HERO request.
                service.resolve_executable_plan(site, instance, approved.generation, False, frappe.session.user)
                import json
                from hero_integration.services.sync_runtime_adapter import ReaderDisabled
                operations = []
                for selected in approved.selections:
                    try:
                        _require_area_enabled(frappe, settings, selected)
                    except ReaderDisabled:
                        continue
                    service.resolve_executable_plan(site, instance, approved.generation, False, frappe.session.user,
                                                    reader=selected.graphql_service)
                    _require_latest_validated_snapshot(frappe, settings, instance, selected)
                    operations.append(json.loads(frappe.db.get_value("HERO GraphQL Mapping", selected.graphql_mapping,
                                                                    "mapping_json"))["target_operation"])
                area = ", ".join(operations)
                if not operations:
                    reason = "No approved reader is enabled."
            except (ValueError, KeyError, frappe.PermissionError, frappe.DoesNotExistError) as error:
                reason = str(error)
    return {"running": processing or queue == "started", "armed": bool(ready and not reason),
            "plan_blocker": reason, "scheduled_operation": area, "writeback_job": queue,
            "orphaned_run": run if run and not processing else None}
