"""Bounded polling modes; OFF never enters this module from the scheduler."""
from __future__ import annotations

from dataclasses import dataclass


VALID_MODES = {"OFF", "MONITOR", "AUTO_SYNC"}


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
    if effective_mode(settings) != "MONITOR":
        return
    try:
        config = get_configuration()
        result = monitor(HeroSource(HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds), config.source_instance))
        frappe.get_doc({
            "doctype": "HERO Polling Event", "mode": "MONITOR", "status": "Succeeded",
            "source_instance": result.source_instance, "customer_records": result.customer_records,
            "project_records": result.project_records,
            "detail": "Read-only monitoring completed; no business records or watermarks were written.",
        }).insert(ignore_permissions=True)
    except Exception:
        frappe.get_doc({
            "doctype": "HERO Polling Event", "mode": "MONITOR", "status": "Failed",
            "detail": "Read-only monitoring failed; see the redacted server error log.",
        }).insert(ignore_permissions=True)
        frappe.log_error(title="HERO read-only monitoring failed")


def _count_pages(reader) -> int:
    count = 0
    offset = 0
    while True:
        page = reader(offset)
        count += len(page.records)
        if page.next_offset is None:
            return count
        offset = page.next_offset
