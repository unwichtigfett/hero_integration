"""One transaction reservation boundary for legacy migrations and B8 runs.

The named lock serializes first-use row creation. The Singles row lock remains
held until the caller commits/rolls back; releasing the named lock is not a
transaction commit. All run checks below are current reads, not snapshot reads.
"""
from __future__ import annotations

LOCK_NAME = "hero_integration:active_run"
SLOT_DOCTYPE = "HERO Integration Sync State"
SLOT_FIELD = "active_run"
LEGACY_TERMINAL_STATES = frozenset({"Completed", "Completed With Conflicts", "Failed", "Cancelled", "Blocked"})


def acquire(frappe) -> bool:
    rows = frappe.db.sql("SELECT GET_LOCK(%s, %s) AS acquired", (LOCK_NAME, 10), as_dict=True)
    return bool(rows and int(rows[0].acquired or 0) == 1)


def release(frappe) -> None:
    frappe.db.sql("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))


def locked_legacy_slot(frappe) -> str:
    rows = frappe.db.sql(
        "SELECT `value` FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s FOR UPDATE",
        (SLOT_DOCTYPE, SLOT_FIELD), as_dict=True,
    )
    if not rows:
        frappe.db.sql(
            "INSERT INTO `tabSingles` (`doctype`, `field`, `value`) VALUES (%s, %s, '')",
            (SLOT_DOCTYPE, SLOT_FIELD),
        )
        return ""
    return str(rows[0].value or "")


def legacy_slot_is_active(frappe, run_name: str) -> bool:
    if not run_name:
        return False
    rows = frappe.db.sql("SELECT state FROM `tabHERO Integration Run` WHERE name=%s FOR UPDATE", (run_name,), as_dict=True)
    # An orphan slot is uncertain: only the existing migration recovery path
    # may reclaim it; B8 must not infer that a missing row authorizes writing.
    return not rows or rows[0].state not in LEGACY_TERMINAL_STATES


def locked_b8_runs(frappe, site: str):
    if not frappe.db.table_exists("HERO Sync Run Ledger"):
        return []
    return frappe.db.sql(
        "SELECT name, sync_run_id FROM `tabHERO Sync Run Ledger` "
        "WHERE site_scope=%s AND state='RUNNING' FOR UPDATE", (site,), as_dict=True,
    )
