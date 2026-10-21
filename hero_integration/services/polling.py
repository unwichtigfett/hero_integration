"""Bounded polling modes; OFF never enters this module from the scheduler."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


VALID_MODES = {"OFF", "MONITOR", "AUTO_SYNC"}
SUPPORTED_SOURCE_OPERATIONS = {"HeroCustomers", "HeroProjectMatches"}
WINDOW_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d$")
MAX_FULL_INVENTORY_PAGES = 100


class PaginationProgressError(ValueError):
    pass


@dataclass(frozen=True)
class MonitorResult:
    customer_records: int
    project_records: int
    source_instance: str
    incomplete_sources: tuple[str, ...] = ()
    customer_next_offset: int = 0
    project_next_offset: int = 0


@dataclass(frozen=True)
class _PageCount:
    records: int
    next_offset: int
    incomplete: bool


def effective_mode(settings) -> str:
    mode = str(getattr(settings, "polling_mode", "") or "").upper()
    if mode in VALID_MODES:
        return mode
    return "AUTO_SYNC" if bool(getattr(settings, "scheduled_sync_enabled", 0)) else "OFF"


def runtime_plan(settings) -> dict[str, object]:
    """Normalize every offered polling setting without reaching HERO."""
    sources = tuple(line.strip() for line in str(getattr(settings, "polling_data_sources", "") or "").splitlines() if line.strip())
    sources = sources or ("HeroCustomers", "HeroProjectMatches")
    filters = _filters(getattr(settings, "polling_filters_json", "{}"))
    strategy = str(getattr(settings, "polling_pagination_strategy", "OFFSET") or "OFFSET").upper()
    timezone = str(getattr(settings, "polling_timezone", "UTC") or "UTC").strip()
    full_inventory = bool(getattr(settings, "polling_full_inventory", 0))
    unsupported = [source for source in sources if source not in SUPPORTED_SOURCE_OPERATIONS]
    if strategy != "OFFSET":
        unsupported.append("pagination strategy " + strategy)
    if not _timezone_is_valid(timezone):
        unsupported.append("timezone " + timezone)
    return {
        "mode": effective_mode(settings), "sources": list(sources), "filters": filters, "timezone": timezone,
        "execution_window": str(getattr(settings, "polling_execution_window", "") or "").strip(),
        "interval_minutes": _bounded_value(getattr(settings, "polling_interval_minutes", 60), 1, 1440, 60),
        "bootstrap": bool(getattr(settings, "polling_bootstrap", 0)),
        "page_size": _bounded_value(getattr(settings, "polling_page_size", 100), 1, 1000, 100),
        "pagination_strategy": strategy, "allow_backfill": bool(getattr(settings, "polling_allow_backfill", 0)),
        "full_inventory": full_inventory, "max_pages": MAX_FULL_INVENTORY_PAGES if full_inventory else 1,
        "reconciliation": bool(getattr(settings, "polling_reconciliation", 0)),
        "retry_limit": _bounded_value(getattr(settings, "polling_retry_limit", 0), 0, 3, 0),
        "retry_backoff_seconds": _bounded_value(getattr(settings, "polling_retry_backoff_seconds", 30), 0, 3600, 30),
        "timeout_seconds": _bounded_value(getattr(settings, "polling_timeout_seconds", getattr(settings, "timeout_seconds", 30)), 1, 120, 30),
        "rate_limit": _bounded_value(getattr(settings, "polling_rate_limit", 30), 1, 10000, 30),
        "concurrency": _bounded_value(getattr(settings, "polling_concurrency", 1), 1, 1, 1),
        "notifications": bool(getattr(settings, "polling_notifications", 0)), "unsupported_sources": unsupported,
        "can_auto_sync": effective_mode(settings) == "AUTO_SYNC" and not unsupported,
        "watermarks": "Offsets resume incomplete bounded scans only; this does not claim a monotonic HERO cursor.",
        "reconciliation_policy": "Report-only; HERO absence never deletes ERPNext data.",
    }


def scheduler_due(settings, now: datetime, last_success: datetime | None = None) -> bool:
    """Bound automatic work by mode, configured timezone, window and interval."""
    plan = runtime_plan(settings)
    if plan["mode"] == "OFF" or plan["unsupported_sources"]:
        return False
    local_now = _in_timezone(now, str(plan["timezone"]))
    window = str(plan["execution_window"])
    if window:
        if not WINDOW_PATTERN.fullmatch(window):
            return False
        start, end = window.split("-", 1)
        clock = local_now.strftime("%H:%M")
        if not (start <= clock <= end if start <= end else clock >= start or clock <= end):
            return False
    interval = int(plan["interval_minutes"])
    return last_success is None or (now - last_success).total_seconds() >= interval * 60


def monitor(source, plan: dict[str, object] | None = None, *, customer_offset: int = 0, project_offset: int = 0, record_observer=None) -> MonitorResult:
    """Read selected source pages with a finite, progress-checked policy."""
    plan = plan or {"sources": ["HeroCustomers", "HeroProjectMatches"], "max_pages": MAX_FULL_INVENTORY_PAGES, "rate_limit": 10000}
    minimum_interval = 60 / max(int(plan.get("rate_limit", 10000)), 1)
    max_pages = max(int(plan.get("max_pages", MAX_FULL_INVENTORY_PAGES)), 1)
    counts: dict[str, _PageCount] = {}
    if "HeroCustomers" in plan["sources"]:
        counts["HeroCustomers"] = _count_pages(source.customers, customer_offset, max_pages, minimum_interval, record_observer, "HeroCustomers")
    if "HeroProjectMatches" in plan["sources"]:
        counts["HeroProjectMatches"] = _count_pages(source.projects, project_offset, max_pages, minimum_interval, record_observer, "HeroProjectMatches")
    incomplete = tuple(name for name, item in counts.items() if item.incomplete)
    customers = counts.get("HeroCustomers", _PageCount(0, 0, False))
    projects = counts.get("HeroProjectMatches", _PageCount(0, 0, False))
    return MonitorResult(customers.records, projects.records, source.source_instance, incomplete, customers.next_offset, projects.next_offset)


def run_monitor() -> None:
    """Scheduled MONITOR entrypoint: read-only transport plus bounded metadata."""
    import frappe

    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.client.settings import get_configuration
    from hero_integration.sources.hero import HeroSource

    settings = frappe.get_single("HERO Integration Settings")
    plan = runtime_plan(settings)
    if plan["mode"] != "MONITOR":
        return
    if plan["unsupported_sources"]:
        _record_event("MONITOR", "Skipped", "Polling configuration is not supported by the approved runtime contract; no HERO request was made.")
        return
    state = frappe.get_single("HERO Integration Sync State")
    customer_offset = 0 if plan["allow_backfill"] or plan["bootstrap"] else int(getattr(state, "customer_offset", 0) or 0)
    project_offset = 0 if plan["allow_backfill"] or plan["bootstrap"] else int(getattr(state, "project_offset", 0) or 0)
    try:
        config = get_configuration()
        client = HeroGraphQLClient(config.endpoint, lambda: config.api_token, timeout=int(plan["timeout_seconds"]), max_retries=int(plan["retry_limit"]), retry_backoff_seconds=int(plan["retry_backoff_seconds"]))
        source = HeroSource(client, config.source_instance, page_size=int(plan["page_size"]), filters=plan["filters"], pagination_strategy=str(plan["pagination_strategy"]))
        result = monitor(source, plan, customer_offset=customer_offset, project_offset=project_offset, record_observer=_monitor_observer if plan["reconciliation"] else None)
        state.customer_offset, state.project_offset = result.customer_next_offset, result.project_next_offset
        state.watermark_status = "Bounded incomplete monitor" if result.incomplete_sources else "Monitor scan complete; no cursor watermark claimed"
        state.save(ignore_permissions=True)
        if result.incomplete_sources:
            detail = "Read-only monitoring stopped at its configured page bound for: " + ", ".join(result.incomplete_sources) + "."
            event = _record_event("MONITOR", "Incomplete", detail, result)
            settings.sync_status = "MONITOR incomplete"
        else:
            detail = "Read-only monitoring completed; no business records were written."
            event = _record_event("MONITOR", "Succeeded", detail, result)
            settings.last_successful_sync, settings.sync_status = frappe.utils.now_datetime(), "MONITOR succeeded"
        settings.sync_error = ""
        settings.save(ignore_permissions=True)
        if plan["notifications"]:
            _notify_polling_event(event, detail)
    except Exception:
        _record_event("MONITOR", "Failed", "Read-only monitoring failed; see the redacted server error log.")
        settings.sync_status = "MONITOR failed"
        settings.sync_error = "Read-only monitoring failed; see the redacted server error log."
        settings.save(ignore_permissions=True)
        frappe.log_error(title="HERO read-only monitoring failed")


def _record_event(mode: str, status: str, detail: str, result: MonitorResult | None = None) -> str:
    import frappe

    event = frappe.get_doc({"doctype": "HERO Polling Event", "mode": mode, "status": status,
        "source_instance": result.source_instance if result else "", "customer_records": result.customer_records if result else 0,
        "project_records": result.project_records if result else 0, "detail": detail})
    event.insert(ignore_permissions=True)
    return event.name


def _notify_polling_event(event_name: str, detail: str) -> None:
    import frappe
    from hero_integration.services.conflict_notifications import configured_recipients

    for user in configured_recipients():
        frappe.get_doc({"doctype": "Notification Log", "for_user": user, "type": "Alert",
            "document_type": "HERO Polling Event", "document_name": event_name,
            "subject": "HERO polling status", "email_content": detail}).insert(ignore_permissions=True)


def _monitor_observer(operation: str, record: dict[str, object]) -> None:
    """Persist only meaningful field-comparison outcomes from MONITOR."""
    from hero_integration.services.integration_events import record_output
    from hero_integration.services.migration import observe_monitor_record

    result = observe_monitor_record(operation, record)
    if result is None:
        return
    source_id = str(record.get("id")) if isinstance(record.get("id"), int) else "unknown"
    record_output(
        source_type=operation,
        source_id=source_id,
        result=result.code,
        action="monitor_reconciliation",
        detail=result.detail,
        target_doctype=result.target_doctype,
        target_name=result.target_name,
    )


def _count_pages(reader, offset: int, max_pages: int, minimum_interval: float, record_observer=None, operation: str = "") -> _PageCount:
    count = pages = 0
    seen_offsets: set[int] = set()
    current_offset = max(int(offset or 0), 0)
    last_request = 0.0
    while pages < max_pages:
        if current_offset in seen_offsets:
            raise PaginationProgressError("HERO pagination repeated an offset; monitoring stopped safely.")
        seen_offsets.add(current_offset)
        delay = minimum_interval - (time.monotonic() - last_request)
        if pages and delay > 0:
            time.sleep(delay)
        page = reader(current_offset)
        last_request = time.monotonic()
        pages += 1
        count += len(page.records)
        if record_observer:
            for record in page.records:
                record_observer(operation, record)
        if page.next_offset is None:
            return _PageCount(count, 0, False)
        if not isinstance(page.next_offset, int) or page.next_offset <= current_offset:
            raise PaginationProgressError("HERO pagination did not advance; monitoring stopped safely.")
        current_offset = page.next_offset
    return _PageCount(count, current_offset, True)


def _filters(value: object) -> dict[str, dict[str, object]]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(name): item for name, item in parsed.items() if isinstance(item, dict)}


def _bounded_value(value: object, minimum: int, maximum: int, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if minimum <= result <= maximum else default


def _timezone_is_valid(value: str) -> bool:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    return True


def _in_timezone(now: datetime, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    return now.astimezone(zone) if now.tzinfo else now.replace(tzinfo=zone)
