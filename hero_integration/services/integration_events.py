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


def migration_outcome(result: str) -> str:
    """Summarize existing durable result codes without replacing their detail."""
    if result in {"CREATED", "UPDATED", "CONVERGED", "OBSERVED", "BASELINE", "SKIPPED", "PREVIEW_OK", "SKIPPED_CONTACT_PERSON", "SKIPPED_SOURCE_DELETED"}:
        return "SUCCESS"
    if result in {"CONFLICT", "SKIPPED_AMBIGUOUS", "SKIPPED_REJECTED"}:
        return "CONFLICT"
    if result in {"PENDING_REVIEW", "UNAVAILABLE"}:
        return "SKIPPED_DEPENDENCY"
    return "FAILED"


def redacted_changed_fields(fields: list[str] | None) -> str:
    """Format supplied field names; callers must not pass values (no validation or redaction)."""

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
    event_id: str | None = None,
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
    if event_id:
        # Hash-autonamed Documents can regenerate their name after a duplicate
        # insert. Keep the content-derived primary key for every redelivery.
        now = record.observed_at
        values = record.as_dict()
        columns = ["source_type", "source_id", "result", "detail", "target_doctype", "target_name",
                   "output_schema_version", "event_type", "technical_action", "correlation_id",
                   "origin", "observed_at", "changed_fields"]
        actor = frappe.session.user
        frappe.db.bulk_insert("HERO Integration Record",
            ["name", "creation", "modified", "owner", "modified_by", *columns],
            [(event_id, now, now, actor, actor, *(values[key] for key in columns))], ignore_duplicates=True)
        return event_id
    else:
        record.insert(ignore_permissions=True)
    return record.name


def record_processed_page(run, source_type, identities):
    """Batch idempotent primary-identity observations with the existing page checkpoint."""
    from hashlib import sha256
    import json

    now = now_datetime()
    actor = frappe.session.user
    rows = []
    for identity in identities:
        key = sha256(json.dumps([run, source_type, str(identity)]).encode()).hexdigest()
        rows.append((key, now, now, actor, actor, run, source_type, str(identity), "OBSERVED",
                     "identity_checkpoint", OUTPUT_SCHEMA_VERSION, "integration.result", "hero_integration", now))
    frappe.db.bulk_insert("HERO Integration Record", ["name", "creation", "modified", "owner", "modified_by",
        "run", "source_type", "source_id", "result", "technical_action", "output_schema_version",
        "event_type", "origin", "observed_at"], rows, ignore_duplicates=True)


@_whitelist()
def list_outputs(limit_start: int = 0, limit_page_length: int = 20) -> dict[str, Any]:
    """Return a paginated, System-Manager-only projection of durable results."""

    if "System Manager" not in frappe.get_roles():
        frappe.throw(frappe._("System Manager role is required"), frappe.PermissionError)
    start = max(int(limit_start or 0), 0)
    page_length = min(max(int(limit_page_length or 20), 1), 100)
    fields = [
        "name", "creation", "observed_at", "output_schema_version", "event_type",
        "source_type", "source_id", "target_doctype", "target_name",
        "technical_action", "result", "correlation_id", "origin", "changed_fields",
    ]
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "records": [{**row, "outcome": migration_outcome(row.get("result", ""))} for row in frappe.get_all(
            "HERO Integration Record", fields=fields,
            or_filters=[["technical_action", "!=", "identity_checkpoint"], ["technical_action", "is", "not set"]],
            order_by="creation desc",
            limit_start=start, limit_page_length=page_length,
        )],
        "limit_start": start,
        "limit_page_length": page_length,
    }
