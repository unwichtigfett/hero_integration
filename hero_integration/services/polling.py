"""Bounded polling modes; OFF never enters this module from the scheduler."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re


VALID_MODES = {"OFF", "MONITOR", "AUTO_SYNC"}
SUPPORTED_SOURCE_OPERATIONS = {"HeroCustomers", "HeroProjectMatches"}
WINDOW_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class MonitorResult:
    customer_records: int
    project_records: int
    source_instance: str


def effective_mode(settings) -> str:
    mode = str(getattr(settings, "polling_mode", "") or "").upper()
    if mode in VALID_MODES:
        return mode
    # Legacy sites used a boolean. Preserve the safe historical meaning while
    # fresh sites default to OFF through the DocType field.
    return "AUTO_SYNC" if bool(getattr(settings, "scheduled_sync_enabled", 0)) else "OFF"


def runtime_plan(settings) -> dict[str, object]:
    """Describe the persisted contract without reaching HERO or scheduling work."""
    sources = tuple(line.strip() for line in str(getattr(settings, "polling_data_sources", "") or "").splitlines() if line.strip())
    sources = sources or ("HeroCustomers", "HeroProjectMatches")
    unavailable = [source for source in sources if source not in SUPPORTED_SOURCE_OPERATIONS]
    return {
        "mode": effective_mode(settings), "sources": list(sources), "unsupported_sources": unavailable,
        "can_auto_sync": effective_mode(settings) == "AUTO_SYNC" and not unavailable,
        "watermarks": "OFFSET pagination is resumable by stored offsets but does not claim a monotonic cursor watermark.",
        "reconciliation": "Report-only; HERO absence never deletes ERPNext data.",
    }


def scheduler_due(settings, now: datetime, last_success: datetime | None = None) -> bool:
    """Bound automatic work by mode, local window, and persisted interval.

    The Frappe scheduler invokes the wrapper frequently; this predicate is deliberately
    side-effect free and never manufactures a due time from an external cursor.
    """
    if effective_mode(settings) == "OFF":
        return False
    window = str(getattr(settings, "polling_execution_window", "") or "").strip()
    if window:
        if not WINDOW_PATTERN.fullmatch(window):
            return False
        start, end = window.split("-", 1)
        clock = now.strftime("%H:%M")
        in_window = start <= clock <= end if start <= end else clock >= start or clock <= end
        if not in_window:
            return False
    interval = max(1, int(getattr(settings, "polling_interval_minutes", 60) or 60))
    return last_success is None or (now - last_success).total_seconds() >= interval * 60


def monitor(source) -> MonitorResult:
    """Read all available pages without creating/updating business documents.

    HERO's current fixed V1 queries expose offset pagination, not a monotonic
    cursor.  Consequently this function deliberately does not manufacture or
    advance watermarks.
    """
    customers = _count_pages(source.customers)
    projects = _count_pages(source.projects)
    return MonitorResult(customers, projects, source.source_instance)


def run_monitor() -> None:
    """Scheduled MONITOR entrypoint: read-only transport plus metadata event."""
    import frappe

    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.client.settings import get_configuration
    from hero_integration.sources.hero import HeroSource

    settings = frappe.get_single("HERO Integration Settings")
    plan = runtime_plan(settings)
    if plan["mode"] != "MONITOR":
        return
    if plan["unsupported_sources"]:
        _record_event("MONITOR", "Skipped", "Schema-only polling source requires a supported runtime adapter; no HERO request was made.")
        return
    try:
        config = get_configuration()
        result = monitor(HeroSource(HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds), config.source_instance))
        _record_event("MONITOR", "Succeeded", "Read-only monitoring completed; no business records or watermarks were written.", result)
        settings.last_successful_sync = frappe.utils.now_datetime()
        settings.sync_status = "MONITOR succeeded"
        settings.sync_error = ""
        settings.save(ignore_permissions=True)
    except Exception:
        _record_event("MONITOR", "Failed", "Read-only monitoring failed; see the redacted server error log.")
        settings.sync_status = "MONITOR failed"
        settings.sync_error = "Read-only monitoring failed; see the redacted server error log."
        settings.save(ignore_permissions=True)
        frappe.log_error(title="HERO read-only monitoring failed")


def _record_event(mode: str, status: str, detail: str, result: MonitorResult | None = None) -> None:
    import frappe

    frappe.get_doc({
        "doctype": "HERO Polling Event", "mode": mode, "status": status,
        "source_instance": result.source_instance if result else "",
        "customer_records": result.customer_records if result else 0,
        "project_records": result.project_records if result else 0,
        "detail": detail,
    }).insert(ignore_permissions=True)


def _count_pages(reader) -> int:
    count = 0
    offset = 0
    while True:
        page = reader(offset)
        count += len(page.records)
        if page.next_offset is None:
            return count
        offset = page.next_offset
