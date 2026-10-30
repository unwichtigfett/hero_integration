"""Bounded polling modes; OFF never enters this module from the scheduler."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone as dt_timezone
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


VALID_MODES = {"OFF", "MONITOR", "AUTO_SYNC"}
# Polling reads the reviewed V1 source operations, unfiltered, of every area switched on
# on the Synchronization page.
POLLING_SOURCES = ("HeroCustomers", "HeroProjectMatches")
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
    from hero_integration.services.sync_page import AREAS, BY_OPERATION, area_enabled

    areas = {item.key: area_enabled(settings, item) for item in AREAS}
    sources = [operation for operation in POLLING_SOURCES if areas[BY_OPERATION[operation].key]]
    strategy = str(getattr(settings, "polling_pagination_strategy", "OFFSET") or "OFFSET").upper()
    timezone = str(getattr(settings, "polling_timezone", "UTC") or "UTC").strip()
    full_inventory = bool(getattr(settings, "polling_full_inventory", 0))
    unsupported = _invalid_numeric_settings(settings)
    if strategy != "OFFSET":
        unsupported.append("pagination strategy " + strategy)
    if not _timezone_is_valid(timezone):
        unsupported.append("timezone " + timezone)
    return {
        "mode": effective_mode(settings), "sources": sources, "timezone": timezone,
        "paused": bool(getattr(settings, "sync_service_paused", 0)), "areas": areas,
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
        "can_auto_sync": effective_mode(settings) == "AUTO_SYNC" and not unsupported
        and not getattr(settings, "sync_service_paused", 0) and any(areas.values()),
        "watermarks": "Offsets resume incomplete bounded scans only; this does not claim a monotonic HERO cursor.",
        "reconciliation_policy": "Report-only; HERO absence never deletes ERPNext data.",
    }


def scheduler_due(settings, now: datetime, last_success: object = None, *, system_timezone: str | None = None) -> bool:
    """Bound automatic work by mode, configured timezone, window and interval.

    Frappe passes naive datetimes in the *system* timezone and stores Single
    Datetime values as strings; both are normalized before comparing.
    """
    plan = runtime_plan(settings)
    if plan["mode"] == "OFF" or plan["unsupported_sources"] or plan["paused"] or not any(plan["areas"].values()):
        return False
    current = _aware(now, system_timezone)
    if current is None:
        return False
    local_now = current.astimezone(ZoneInfo(str(plan["timezone"])))
    window = str(plan["execution_window"])
    if window:
        if not WINDOW_PATTERN.fullmatch(window):
            return False
        start, end = window.split("-", 1)
        clock = local_now.strftime("%H:%M")
        if not (start <= clock <= end if start <= end else clock >= start or clock <= end):
            return False
    interval = int(plan["interval_minutes"])
    previous = _aware(_as_datetime(last_success), system_timezone)
    if previous is None:
        return True
    elapsed = (current - previous).total_seconds()
    # A success time in the future (clock or timezone change) must not block polling indefinitely.
    return elapsed < 0 or elapsed >= interval * 60


def monitor(source, plan: dict[str, object] | None = None, *, customer_offset: int = 0, project_offset: int = 0, record_observer=None) -> MonitorResult:
    """Read selected source pages with a finite, progress-checked policy."""
    plan = plan or {"sources": list(POLLING_SOURCES), "max_pages": MAX_FULL_INVENTORY_PAGES, "rate_limit": 10000}
    minimum_interval = 60 / max(int(plan.get("rate_limit", 10000)), 1)
    max_pages = max(int(plan.get("max_pages", MAX_FULL_INVENTORY_PAGES)), 1)
    page_size = plan.get("page_size")
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        page_size = None
    counts: dict[str, _PageCount] = {}
    if "HeroCustomers" in plan["sources"]:
        counts["HeroCustomers"] = _count_pages(source.customers, customer_offset, max_pages, minimum_interval, record_observer, "HeroCustomers", page_size)
    if "HeroProjectMatches" in plan["sources"]:
        counts["HeroProjectMatches"] = _count_pages(source.projects, project_offset, max_pages, minimum_interval, record_observer, "HeroProjectMatches", page_size)
    incomplete = tuple(name for name, item in counts.items() if item.incomplete)
    customers = counts.get("HeroCustomers", _PageCount(0, 0, False))
    projects = counts.get("HeroProjectMatches", _PageCount(0, 0, False))
    return MonitorResult(customers.records, projects.records, source.source_instance, incomplete, customers.next_offset, projects.next_offset)


def run_monitor() -> None:
    """Scheduled MONITOR entrypoint: read-only transport plus bounded metadata."""
    import frappe

    settings = frappe.get_single("HERO Integration Settings")
    plan = runtime_plan(settings)
    if plan["mode"] != "MONITOR":
        return
    if plan["unsupported_sources"]:
        _record_event("MONITOR", "Skipped", "Polling configuration is not supported by the approved runtime contract; no HERO request was made.")
        return
    from hero_integration.services.sync_runtime_adapter import RuntimeBridgeError, run_materialized_monitor
    try:
        result = run_materialized_monitor(frappe_module=frappe)
        detail = "Materialized monitoring observed one bounded read page; full coverage is not claimed and no business records were written."
        event = _record_event("MONITOR", "Incomplete", detail, result)
        settings.last_successful_sync = frappe.utils.now_datetime()
        settings.sync_status, settings.sync_error = "MONITOR incomplete", ""
        settings.save(ignore_permissions=True)
        if plan["notifications"]:
            _notify_polling_event(event, detail)
    except RuntimeBridgeError as error:
        record_scheduler_outcome("MONITOR", "Skipped", f"Materialized monitoring is blocked: {error}")
    except Exception:
        _record_event("MONITOR", "Failed", "Materialized monitoring failed; see the redacted server error log.")
        settings.sync_status = "MONITOR failed"
        settings.sync_error = "Materialized monitoring failed or is not approved; see the redacted server error log."
        settings.save(ignore_permissions=True)
        frappe.log_error(title="HERO materialized monitoring failed")


def record_scheduler_outcome(mode: str, status: str, detail: str) -> str | None:
    """Make a scheduler skip or failure visible once; repeats of the same state add no new event."""
    import frappe

    settings = frappe.get_single("HERO Integration Settings")
    label = f"{mode} {status.lower()}"
    if (getattr(settings, "sync_status", "") or "") == label and (getattr(settings, "sync_error", "") or "") == detail:
        return None
    event = _record_event(mode if mode in {"MONITOR", "AUTO_SYNC"} else "MONITOR", status, detail)
    settings.sync_status, settings.sync_error = label, detail
    settings.save(ignore_permissions=True)
    return event


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


def _count_pages(reader, offset: int, max_pages: int, minimum_interval: float, record_observer=None, operation: str = "", page_size: int | None = None) -> _PageCount:
    count = pages = 0
    seen_offsets: set[int] = set()
    seen_identities: set[str] = set()
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
        if page_size is not None and len(page.records) > page_size:
            raise PaginationProgressError("HERO response exceeds the configured page size.")
        if page.next_offset is not None and (type(page.next_offset) is not int or page.next_offset <= current_offset or not page.records):
            raise PaginationProgressError("HERO pagination did not advance; monitoring stopped safely.")
        identities = []
        for record in page.records:
            identity = record.get('id') if isinstance(record, dict) else None
            if not isinstance(identity, (int, str)) or isinstance(identity, bool) or not str(identity).strip():
                raise PaginationProgressError("HERO source identity is missing or invalid; monitoring stopped safely.")
            identities.append(str(identity))
        if len(set(identities)) != len(identities) or seen_identities.intersection(identities):
            raise PaginationProgressError("HERO source identity repeated across pages; monitoring stopped safely.")
        seen_identities.update(identities)
        count += len(page.records)
        if record_observer:
            for record in page.records:
                record_observer(operation, record)
        if page.next_offset is None:
            if page_size is not None and len(page.records) == page_size:
                return _PageCount(count, current_offset, True)
            return _PageCount(count, 0, False)
        current_offset = page.next_offset
    return _PageCount(count, current_offset, True)


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace(" ", "T", 1))
        except ValueError:
            return None
    return None


def _aware(value: datetime | None, system_timezone: str | None) -> datetime | None:
    """Naive values are in the Frappe system timezone (UTC when unknown)."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    zone = dt_timezone.utc
    if system_timezone:
        try:
            zone = ZoneInfo(system_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return None
    return value.replace(tzinfo=zone)


NUMERIC_LIMITS = {
    "polling_interval_minutes": (1, 1440), "polling_page_size": (1, 1000), "polling_retry_limit": (0, 3),
    "polling_retry_backoff_seconds": (0, 3600), "polling_timeout_seconds": (1, 120),
    "polling_rate_limit": (1, 10000), "polling_concurrency": (1, 1),
}


def _invalid_numeric_settings(settings) -> list[str]:
    """Out-of-range stored values fail closed instead of silently becoming defaults.

    An unset Int field reads as 0/None and keeps its documented default.
    """
    invalid = []
    for field, (minimum, maximum) in NUMERIC_LIMITS.items():
        value = getattr(settings, field, None)
        if value in (None, "", 0) and minimum > 0:
            continue
        try:
            number = int(value) if value not in (None, "") else minimum
        except (TypeError, ValueError):
            invalid.append("setting " + field)
            continue
        if isinstance(value, bool) or not minimum <= number <= maximum:
            invalid.append("setting " + field)
    return invalid


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

