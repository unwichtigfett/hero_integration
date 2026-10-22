"""Field-level, redacted comparison state for a linked HERO target."""

from __future__ import annotations

import hashlib
import json
from typing import Mapping


def field_hashes(values: Mapping[str, object]) -> dict[str, str]:
    """Hash normalised field values without retaining unrestricted payloads."""

    return {
        str(field): hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
        ).hexdigest()
        for field, value in sorted(values.items())
    }


def encode(hashes: Mapping[str, str]) -> str:
    return json.dumps(dict(sorted(hashes.items())), sort_keys=True, separators=(",", ":"))


def decode(value: object) -> dict[str, str] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in decoded.items()):
        return None
    return decoded


def changed_fields(previous: Mapping[str, str], current: Mapping[str, str]) -> set[str]:
    return {
        field for field in set(previous) | set(current)
        if previous.get(field) != current.get(field)
    }


def observe_frappe_target_change(doc, method=None) -> None:
    """Record a mapped Frappe-side field change without scheduling an outbound action.

    The wildcard Frappe hook reaches every DocType, therefore the first query
    is the scope boundary: only targets already named by an app-owned Link are
    considered.  The synchronized baseline remains untouched so that a later
    HERO-side change to the same field still becomes a conflict rather than an
    automatic overwrite.
    """
    del method
    import frappe
    from frappe.utils import now_datetime

    # During install/migrate, DocType metadata can be written before the
    # corresponding database columns exist.  This global on_update hook runs
    # for those technical writes as well, so it must return before querying
    # the Link fields introduced by the same synchronization.
    if frappe.flags.in_migrate:
        return

    links = frappe.get_all(
        "HERO Integration Link",
        filters={"target_doctype": doc.doctype, "target_name": doc.name},
        fields=["name", "source_type", "source_id", "target_field_hashes", "observed_target_hash"],
    )
    for row in links:
        baseline = decode(row.get("target_field_hashes"))
        if not baseline:
            continue
        # ``hero_phone_home`` is a composite relation snapshot, not a field on
        # the target document.  Its lifecycle remains with the dedicated
        # relation resolver and must not produce a false Frappe field change.
        baseline = {field: value for field, value in baseline.items() if not field.startswith("hero_")}
        if not baseline:
            continue
        current_values = {field: doc.get(field) for field in baseline}
        current_hashes = field_hashes(current_values)
        fields = changed_fields(baseline, current_hashes)
        if not fields:
            continue
        observed_hash = hashlib.sha256(
            json.dumps(current_values, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
        ).hexdigest()
        if observed_hash == (row.get("observed_target_hash") or ""):
            continue
        link = frappe.get_doc("HERO Integration Link", row.name)
        link.observed_target_hash = observed_hash
        link.last_observed_on = now_datetime()
        link.save(ignore_permissions=True)
        from hero_integration.services.integration_events import record_output

        record_output(
            source_type=row.source_type,
            source_id=row.source_id,
            result="OBSERVED",
            action="frappe_change_observed",
            detail="Mapped Frappe fields changed locally; no outbound HERO action was prepared.",
            target_doctype=doc.doctype,
            target_name=doc.name,
            changed_fields=sorted(fields),
        )
