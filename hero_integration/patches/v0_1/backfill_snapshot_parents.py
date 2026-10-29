"""Link every existing schema snapshot to its parent (previous snapshot of the same source and endpoint).

Only the link is written. Mappings are not copied by this patch; inheriting them stays an explicit
administrator action (Mapping Editor) or happens automatically for newly captured snapshots.
"""
from __future__ import annotations

import frappe

SNAPSHOT_DOCTYPE = "HERO GraphQL Schema Snapshot"


def execute() -> None:
    if not frappe.db.has_column(SNAPSHOT_DOCTYPE, "parent_snapshot"):
        return
    rows = frappe.get_all(SNAPSHOT_DOCTYPE, fields=["name", "source_instance", "endpoint_fingerprint", "parent_snapshot"],
                          order_by="creation asc, name asc")
    previous: dict[tuple[str, str], str] = {}
    for row in rows:
        lineage = (row.get("source_instance") or "", row.get("endpoint_fingerprint") or "")
        parent = previous.get(lineage)
        if parent and not row.get("parent_snapshot"):
            frappe.db.set_value(SNAPSHOT_DOCTYPE, row["name"], "parent_snapshot", parent, update_modified=False)
        previous[lineage] = row["name"]
