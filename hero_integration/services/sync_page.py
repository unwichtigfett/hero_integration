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


def manual_prerequisite(instance: str, frappe_module) -> str:
    """Return the first unfinished onboarding area for this HERO source."""
    for key, action in (("employees", "Employee Migration"), ("contacts", "Customer Migration")):
        rows = frappe_module.get_all(
            "HERO Integration Run",
            filters={"action": action, "mode": "Execute", "source_instance": instance},
            fields=["state"], order_by="creation desc, name desc", limit_page_length=1,
        )
        if not rows or rows[0]["state"] not in SUCCESS_STATES:
            return key
    return ""


def manual_allowed(key: str, prerequisite: str) -> bool:
    return not prerequisite or key == prerequisite


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
