"""Read-only preview: could existing HERO Customers and Projects take their business number as name?

Nothing is renamed or written to the database. Renaming an existing record needs
a separate, explicit approval and a fresh database backup. Reference counts are
point-in-time evidence, not a guarantee that every reference can be renamed.

    bench --site <site> execute hero_integration.services.business_number_preview.write_csv \
        --kwargs "{'path': '/path/to/preview.csv'}"
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict

import frappe

from hero_integration.services.business_numbers import BusinessNumberError, business_number, project_reference
from hero_integration.services.project_naming import ProjectTitleValidationError

COLUMNS = (
    "doctype", "current_name", "source_instance", "hero_source_id", "hero_number", "proposed_name",
    "link_status", "links_to_target", "collision", "references", "referencing_records", "safe_rename", "reason",
)


def preview_rows(project_trades: dict | None = None) -> list[dict[str, object]]:
    """Optional reviewed trades are {source_instance: {technical_source_id: trade_code}}."""
    rows = _rows("Customer", "customer", "custom_hero_customer_number", _customer_proposal)
    rows += _rows("Project", "project", "custom_hero_display_id", _project_proposal, project_trades=project_trades)
    _judge(rows)
    return rows


def write_csv(path: str, project_trades: dict | None = None) -> dict[str, object]:
    """Write the preview to ``path`` and return a short summary."""
    rows = preview_rows(project_trades)
    with open(path, "x", newline="", encoding="utf-8") as handle:
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


def _rows(doctype, source_type, number_field, propose, *, project_trades=None) -> list[dict[str, object]]:
    links = frappe.get_all(
        "HERO Integration Link",
        filters={"source_system": "HERO", "source_type": source_type, "target_doctype": doctype},
        fields=["name", "source_id", "source_instance", "sync_status", "target_name"],
        limit_page_length=0,
    )
    if not links:
        return []
    fields = ["name", number_field]
    if doctype == "Project" and frappe.get_meta("Project").has_field("custom_hero_project_type"):
        fields.append("custom_hero_project_type")
    targets = {
        row.name: row for row in frappe.get_all(
            doctype, filters={"name": ["in", sorted({link.target_name for link in links})]},
            fields=fields, limit_page_length=0,
        )
    }
    links_per_target = Counter(link.target_name for link in links)
    references = _reference_records(doctype, set(targets))
    rows = []
    for link in links:
        target = targets.get(link.target_name)
        number = target.get(number_field) if target else None
        if not target:
            proposed, reason = None, "target document is missing"
        elif doctype == "Project":
            trade = (project_trades or {}).get(link.source_instance or "default", {}).get(str(link.source_id))
            proposed, reason = propose(target, number, trade=trade)
        else:
            proposed, reason = propose(target, number)
        rows.append({
            "doctype": doctype, "current_name": link.target_name, "source_instance": link.source_instance,
            "hero_source_id": link.source_id, "hero_number": number or "", "proposed_name": proposed or "",
            "link_status": link.sync_status or "", "links_to_target": links_per_target[link.target_name],
            "collision": "", "references": len(references.get(link.target_name, [])),
            "referencing_records": json.dumps(references.get(link.target_name, []), sort_keys=True),
            "safe_rename": False,
            "reason": reason,
        })
    return rows


def _customer_proposal(_target, number) -> tuple[str | None, str]:
    try:
        name = business_number(number)
    except BusinessNumberError as error:
        return None, str(error)
    return (name, "") if name else (None, "no HERO customer number")


def _project_proposal(target, display_id, *, trade=None) -> tuple[str | None, str]:
    """Only explicitly reviewed trade configuration can supply a preview prefix."""
    trade = trade or target.get("custom_hero_project_type")
    if not trade:
        return None, "reviewed source-scoped trade configuration is required; project titles are not evidence"
    try:
        reference = project_reference({"display_id": display_id, "confirmed_project_type": trade})
    except (BusinessNumberError, ProjectTitleValidationError) as error:
        return None, str(error)
    return (reference, "") if reference else (None, "no HERO display_id")


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
        elif not frappe.get_meta(row["doctype"]).allow_rename:
            row["reason"] = "native DocType does not allow rename"
        else:
            row["safe_rename"] = True
            row["reason"] = "rename possible after approval and backup"


def _reference_records(doctype: str, target_names: set[str]) -> dict[str, list[dict[str, str]]]:
    """List native static/dynamic references, including child rows and Single fields.

    This is a point-in-time preview. Custom string references and external systems
    cannot be proven by metadata; approval, backup and a fresh rename preflight
    remain mandatory.
    """
    from frappe.model.dynamic_links import get_dynamic_links
    from frappe.model.rename_doc import get_link_fields

    references = defaultdict(list)
    if not target_names:
        return references
    fields = [(field, None) for field in get_link_fields(doctype)]
    fields += [(field, field.options) for field in get_dynamic_links()]
    seen = set()
    for field, dynamic_type_field in fields:
        parent, fieldname = field["parent"], field["fieldname"]
        key = (parent, fieldname, dynamic_type_field)
        if key in seen:
            continue
        seen.add(key)
        meta = frappe.get_meta(parent)
        if meta.is_virtual:
            continue
        if meta.issingle:
            document = frappe.get_doc(parent)
            target = document.get(fieldname)
            if target in target_names and (not dynamic_type_field or document.get(dynamic_type_field) == doctype):
                references[target].append({"doctype": parent, "name": parent, "field": fieldname})
            continue
        if not frappe.db.table_exists(parent):
            continue
        filters = {fieldname: ["in", sorted(target_names)]}
        if dynamic_type_field:
            filters[dynamic_type_field] = doctype
        columns = ["name", fieldname] + (["parent", "parenttype"] if meta.istable else [])
        for row in frappe.get_all(parent, filters=filters, fields=columns, limit_page_length=0):
            reference = {"doctype": parent, "name": row.name, "field": fieldname}
            if meta.istable:
                reference.update(parent=row.parent, parenttype=row.parenttype)
            references[row.get(fieldname)].append(reference)
    return references
