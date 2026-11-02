"""Remove salutations from imported Project titles without HERO I/O.

Rewrite old titles from stored HERO salutations and advance Link baselines.
Locally edited ERPNext titles remain untouched.
"""

from __future__ import annotations

import frappe


def candidates() -> list[dict]:
    from hero_integration.services.change_detection import decode as decode_hashes, field_hashes

    rows = frappe.db.sql("""
        SELECT p.name, p.project_name, p.custom_hero_customer_title AS salutation, l.name AS link,
               l.source_field_hashes, l.target_field_hashes
        FROM `tabProject` p JOIN `tabHERO Integration Link` l
          ON l.target_doctype='Project' AND l.target_name=p.name AND l.source_type='project'
        WHERE IFNULL(p.custom_hero_customer_title, '') != ''""", as_dict=True)
    found = []
    for row in rows:
        salutation = " ".join(row.salutation.split())
        head, sep, rest = row.project_name.partition(" | ")
        trade, comma, tail = rest.partition(", ")
        if not sep or not comma or not tail.startswith(salutation + " "):
            continue
        source, target = decode_hashes(row.source_field_hashes) or {}, decode_hashes(row.target_field_hashes) or {}
        current = field_hashes({"project_name": row.project_name})["project_name"]
        if target.get("project_name") != current or source.get("project_name") != current:
            continue  # edited in ERPNext or not imported in the old format: leave it
        new = " ".join(f"{head} | {trade}, {tail[len(salutation) + 1:]}".split())
        found.append(frappe._dict({**row, "new_title": new}))
    return found


def run(dry_run: int = 1) -> dict:
    frappe.only_for("System Manager")
    from hero_integration.services.change_detection import decode as decode_hashes, encode as encode_hashes, field_hashes

    found = candidates()
    if int(dry_run):
        return {"would_change": len(found)}
    for row in found:
        frappe.db.set_value("Project", row.name, "project_name", row.new_title, update_modified=False)
        hashes = field_hashes({"project_name": row.new_title})
        source, target = decode_hashes(row.source_field_hashes), decode_hashes(row.target_field_hashes)
        source.update(hashes); target.update(hashes)
        frappe.db.set_value("HERO Integration Link", row.link, {"source_field_hashes": encode_hashes(source),
                                                              "target_field_hashes": encode_hashes(target)},
                            update_modified=False)
    frappe.db.commit()
    return {"changed": len(found)}
