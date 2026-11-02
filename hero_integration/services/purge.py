"""Remove every ERPNext record the HERO import created (user request 2026-11-01).

Only documents bound by a ``HERO Integration Link`` are touched.  A document
that another record still references (for example a manually created Sales
Order for a HERO Customer) is kept and reported, never force-deleted.  The
default is a dry run; the real run needs an explicit confirmation phrase.

The real run also requires HERO Integration Settings → "Allow deletion".

Usage (bench):
    bench --site <site> execute hero_integration.services.purge.purge_hero_data
    bench --site <site> execute hero_integration.services.purge.purge_hero_data \
        --kwargs "{'dry_run': 0, 'confirm': 'DELETE HERO DATA'}"
"""

from __future__ import annotations

import frappe

CONFIRMATION = "DELETE HERO DATA"
# Dependents first: Tasks and Events reference Projects, Projects reference
# Customers.  ERPNext deletes a Customer's own Contacts and Addresses itself.
ORDER = ("Task", "Event", "Project", "Address", "Customer")
# References that disappear together with the target and never block it.
OWN_REFERENCES = {
    "Customer": {"HERO Integration Link", "Contact", "Address"},
}
COMMIT_EVERY = 200


def purge_hero_data(dry_run: int | bool = 1, confirm: str = "", include_history: int | bool = 0) -> dict:
    """Delete all HERO-imported ERPNext records; returns counts per DocType.

    ``include_history`` also removes run history (runs, run records, polling
    events).  Links of kept documents remain, so a later import still finds them.
    """
    frappe.only_for("System Manager")
    dry_run = bool(int(dry_run))
    if not dry_run and confirm != CONFIRMATION:
        frappe.throw(frappe._('Deleting requires confirm="{0}".').format(CONFIRMATION))
    if not dry_run:
        # Second lock: HERO Integration Settings → "Allow deletion".
        from hero_integration.services.guards import assert_deletion_allowed
        assert_deletion_allowed(action="the HERO data purge")
    active = frappe.get_all("HERO Integration Run", filters={"state": ["in", ["Queued", "Running"]]}, pluck="name")
    if active:
        frappe.throw(frappe._("A HERO run is active ({0}); wait for it to finish.").format(", ".join(active)))

    links = frappe.get_all(
        "HERO Integration Link", filters={"source_system": "HERO"},
        fields=["name", "target_doctype", "target_name"], limit_page_length=0,
    )
    by_doctype: dict[str, list] = {}
    for link in links:
        by_doctype.setdefault(link.target_doctype, []).append(link)
    doctypes = [d for d in ORDER if d in by_doctype] + sorted(set(by_doctype) - set(ORDER))

    report = {"dry_run": dry_run, "deleted": {}, "kept": {}, "missing": {}, "links_removed": 0, "examples_kept": []}
    if dry_run:
        report["would_delete"] = {doctype: len(by_doctype[doctype]) for doctype in doctypes}
        report["links"] = len(links)
        return report

    done = 0
    for doctype in doctypes:
        for link in by_doctype[doctype]:
            outcome = _delete_target(doctype, link.target_name)
            report[outcome][doctype] = report[outcome].get(doctype, 0) + 1
            if outcome == "kept":
                if len(report["examples_kept"]) < 20:
                    report["examples_kept"].append(f"{doctype} {link.target_name}: {_last_reference}")
                continue
            frappe.delete_doc("HERO Integration Link", link.name, force=True, ignore_permissions=True)
            report["links_removed"] += 1
            done += 1
            if done % COMMIT_EVERY == 0:
                frappe.db.commit()
    if include_history:
        report["history_removed"] = _delete_history()
    frappe.db.commit()
    return report


_last_reference = ""


def _delete_target(doctype: str, name: str) -> str:
    """Delete one bound target unless something else references it."""
    global _last_reference
    if not name or not frappe.db.exists(doctype, name):
        return "missing"
    from hero_integration.services.migration import _references_besides_hero

    other = _references_besides_hero(doctype, name, ignore=OWN_REFERENCES.get(doctype, set()))
    if other:
        _last_reference = other
        return "kept"
    frappe.db.savepoint("hero_purge")
    try:
        # Only our own binding (and, for a Customer, its own Contacts and
        # Addresses) still points here; Frappe's link check would refuse.
        from hero_integration.services.guards import bound_target_deletion

        with bound_target_deletion():
            frappe.delete_doc(doctype, name, force=True)
    except Exception as error:  # a validation hook refused; keep and report
        frappe.db.rollback(save_point="hero_purge")
        _last_reference = f"{type(error).__name__}: {error}"[:200]
        return "kept"
    return "deleted"


def _delete_history() -> dict:
    removed = {}
    for doctype in ("HERO Integration Record", "HERO Polling Event", "HERO Integration Run"):
        if frappe.db.exists("DocType", doctype):
            removed[doctype] = frappe.db.count(doctype)
            frappe.db.delete(doctype)
    return removed
