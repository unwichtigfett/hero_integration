"""Read-only preview: could existing HERO Customers and Projects take their business number as name?

Nothing is renamed or written to the database. Renaming an existing record needs
a separate, explicit approval and a fresh database backup; ``rename_doc`` would
then also move every reference counted here and the HERO Integration Link.

    bench --site <site> execute hero_integration.services.business_number_preview.write_csv \
        --kwargs "{'path': '/path/to/preview.csv'}"
"""
from __future__ import annotations

import csv
from collections import Counter

import frappe

from hero_integration.services.business_numbers import BusinessNumberError, business_number
from hero_integration.services.project_naming import TRADE_CATALOG

COLUMNS = (
    "doctype", "current_name", "source_instance", "hero_source_id", "hero_number", "proposed_name",
    "link_status", "links_to_target", "collision", "references", "safe_rename", "reason",
)


def preview_rows() -> list[dict[str, object]]:
    rows = _rows("Customer", "customer", "custom_hero_customer_number", _customer_proposal)
    rows += _rows("Project", "project", "custom_hero_display_id", _project_proposal)
    _judge(rows)
    return rows


def write_csv(path: str) -> dict[str, object]:
    """Write the preview to ``path`` and return a short summary."""
    rows = preview_rows()
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return summarize(rows) | {"path": path}


def summarize(rows: list[dict[str, object]]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for doctype in ("Customer", "Project"):
        own = [row for row in rows if row["doctype"] == doctype]
        summary[doctype] = {
            "linked": len(own),
            "safe_rename": sum(bool(row["safe_rename"]) for row in own),
            "already_named": sum(row["current_name"] == row["proposed_name"] for row in own),
            "reasons": dict(Counter(row["reason"] for row in own if not row["safe_rename"])),
        }
    return summary


def _rows(doctype, source_type, number_field, propose) -> list[dict[str, object]]:
    links = frappe.get_all(
        "HERO Integration Link",
        filters={"source_type": source_type, "target_doctype": doctype},
        fields=["name", "source_id", "source_instance", "sync_status", "target_name"],
        limit_page_length=0,
    )
    fields = ["name", number_field] + (["project_name"] if doctype == "Project" else [])
    targets = {
        row.name: row for row in frappe.get_all(doctype, fields=fields, limit_page_length=0)
    }
    links_per_target = Counter(link.target_name for link in links)
    references = _reference_counts(doctype)
    rows = []
    for link in links:
        target = targets.get(link.target_name)
        number = target.get(number_field) if target else None
        proposed, reason = propose(target, number) if target else (None, "target document is missing")
        rows.append({
            "doctype": doctype, "current_name": link.target_name, "source_instance": link.source_instance,
            "hero_source_id": link.source_id, "hero_number": number or "", "proposed_name": proposed or "",
            "link_status": link.sync_status or "", "links_to_target": links_per_target[link.target_name],
            "collision": "", "references": references.get(link.target_name, 0), "safe_rename": False,
            "reason": reason,
        })
    return rows


def _customer_proposal(_target, number) -> tuple[str | None, str]:
    try:
        name = business_number(number)
    except BusinessNumberError as error:
        return None, str(error)
    return (name, "") if name else (None, "no HERO customer number")


def _project_proposal(target, display_id) -> tuple[str | None, str]:
    """The trade comes from the stored title, which the confirmed trade catalog built (``PV-101 | ...``)."""
    try:
        number = business_number(display_id)
    except BusinessNumberError as error:
        return None, str(error)
    if not number:
        return None, "no HERO display_id"
    prefix = (target.get("project_name") or "").split(" | ", 1)[0]
    trade = prefix[: -len(number) - 1] if prefix.endswith(f"-{number}") else ""
    if trade not in TRADE_CATALOG:
        return None, "trade cannot be read from the stored project title"
    return f"{trade}-{number}", ""


def _judge(rows) -> None:
    proposals = Counter((row["doctype"], row["proposed_name"]) for row in rows if row["proposed_name"])
    for row in rows:
        proposed = row["proposed_name"]
        if not proposed:
            continue
        if proposed == row["current_name"]:
            row["reason"] = "already named by its HERO number"
            continue
        if proposals[(row["doctype"], proposed)] > 1:
            row["collision"] = "proposed by several HERO records"
        elif frappe.db.exists(row["doctype"], proposed):
            row["collision"] = f"name used by {row['doctype']} {proposed}"
        if row["collision"]:
            row["reason"] = "collision"
        elif row["link_status"] != "Synchronized":
            row["reason"] = f"link status {row['link_status'] or 'unknown'}"
        elif row["links_to_target"] != 1:
            row["reason"] = "several HERO links point to this document"
        else:
            row["safe_rename"] = True
            row["reason"] = "rename possible after approval and backup"


def _reference_counts(doctype: str) -> Counter:
    """Static Link fields plus Contact/Address Dynamic Links. Other dynamic references
    (comments, versions, ToDos) are moved by ``rename_doc`` too but are not counted."""
    from frappe.model.rename_doc import get_link_fields

    counts: Counter = Counter()
    for field in get_link_fields(doctype):
        if field.get("issingle"):
            continue
        parent, fieldname = field["parent"], field["fieldname"]
        if not frappe.db.table_exists(parent):
            continue
        for row in frappe.db.sql(
            f"SELECT `{fieldname}` AS target, COUNT(*) AS n FROM `tab{parent}` "
            f"WHERE `{fieldname}` IS NOT NULL AND `{fieldname}` != '' GROUP BY `{fieldname}`",
            as_dict=True,
        ):
            counts[row.target] += row.n
    for row in frappe.db.sql(
        "SELECT link_name AS target, COUNT(*) AS n FROM `tabDynamic Link` WHERE link_doctype=%s GROUP BY link_name",
        (doctype,), as_dict=True,
    ):
        counts[row.target] += row.n
    return counts
