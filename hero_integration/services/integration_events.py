"""Durable, bounded integration output built on ``HERO Integration Record``.

This is deliberately not a general event bus.  It is the app's small, versioned
read model for a completed observation or action and is safe to consume without
requiring another HERO- or Core-specific application.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import now_datetime


OUTPUT_SCHEMA_VERSION = "hero-integration-result/v1"
_whitelist = getattr(frappe, "whitelist", lambda: lambda function: function)


def redacted_changed_fields(fields: list[str] | None) -> str:
    """Return field names only; values must stay in the protected source record."""

    return ", ".join(sorted({str(field) for field in fields or [] if field}))


def record_output(
    *,
    source_type: str,
    source_id: str,
    result: str,
    action: str,
    detail: str = "",
    run: str | None = None,
    target_doctype: str | None = None,
    target_name: str | None = None,
    correlation_id: str | None = None,
    origin: str = "hero_integration",
    changed_fields: list[str] | None = None,
) -> str:
    """Persist one technical outcome after the caller's business write is known.

    The caller controls its database transaction.  No realtime notification is
    emitted here, so consumers never receive an event that later rolls back.
    """

    record = frappe.get_doc(
        {
            "doctype": "HERO Integration Record",
            "run": run,
            "source_type": source_type,
            "source_id": str(source_id),
            "result": result,
            "detail": detail,
            "target_doctype": target_doctype,
            "target_name": target_name,
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "event_type": "integration.result",
            "technical_action": action,
            "correlation_id": correlation_id or "",
            "origin": origin,
            "observed_at": now_datetime(),
            "changed_fields": redacted_changed_fields(changed_fields),
        }
    )
    record.insert(ignore_permissions=True)
    return record.name


@_whitelist()
def list_outputs(limit_start: int = 0, limit_page_length: int = 20) -> dict[str, Any]:
    """Return a paginated, System-Manager-only projection of durable results."""

    if "System Manager" not in frappe.get_roles():
        frappe.throw("System Manager role is required", frappe.PermissionError)
    start = max(int(limit_start or 0), 0)
    page_length = min(max(int(limit_page_length or 20), 1), 100)
    fields = [
        "name", "creation", "observed_at", "output_schema_version", "event_type",
        "source_type", "source_id", "target_doctype", "target_name",
        "technical_action", "result", "correlation_id", "origin", "changed_fields",
    ]
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "records": frappe.get_all(
            "HERO Integration Record", fields=fields, order_by="creation desc",
            limit_start=start, limit_page_length=page_length,
        ),
        "limit_start": start,
        "limit_page_length": page_length,
    }
