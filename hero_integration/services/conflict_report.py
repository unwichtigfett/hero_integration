"""Readable report for one Synchronization conflict (read-only).

A conflict link stores an English diagnostic and hash baselines, never the
synchronized values. The report therefore names what the diagnostic proves:
the HERO record, the affected Doctype fields with the HERO variables that
feed them (from the rules of the run that processed the record; unknown when
that run cannot be reconstructed), the current ERPNext value and, where
the diagnostic carries them, the old and new HERO values. Texts stay English;
the Synchronization page translates them at display time.
"""

from __future__ import annotations

import ast
import re

import frappe

RECHECK_PREFIX = "Recheck requested: "
MAX_VALUE = 300
MAX_FIELDS = 20

_FIELD_LIST = re.compile(r"^(?:Field conflict requires review|Task fields require review): (?P<fields>[\w, ]+?)\.?$")
_NUMBER_DRIFT = re.compile(
    r"^HERO (?P<label>.+?) changed from (?P<old>'[^']*'|\"[^\"]*\") to (?P<new>'[^']*'|\"[^\"]*\")\. "
    r".* set (?P<field>\w+) to ")
_PRICE = re.compile(r"^The (?:imported )?(?P<list>Standard (?:Buying|Selling)) price (?P<name>\S+) ")

# Review reasons without a field list: the field they concern.
_REASON_FIELDS = (
    ("the Project has no customer", "customer"),
    ("The project address is missing or incomplete", "project_name"),
)

# What the two decisions do, per kind of report (English, translated in the page).
_EXPLANATIONS = {
    "field": "The field was changed in HERO and in ERPNext since the last synchronization, "
             "so neither value was overwritten.",
    "number": "HERO changed the business number. The ERPNext record keeps its name and number until you decide.",
    "review": "HERO delivered this record with a value ERPNext cannot use as it is. "
              "It was imported and waits for your confirmation.",
    "product": "The ERPNext Item or price was changed after the last import, so the newer HERO article was not applied.",
    "record": "HERO delivered this record, but it could not be imported. It has no ERPNext record yet.",
    "other": "The synchronization stopped for this record and needs a decision.",
}
_DECISIONS = {
    "Conflict": ("The next synchronization checks the record again and applies HERO changes that no longer conflict.",
                 "ERPNext keeps its current values; later HERO changes to this record are not imported."),
    "Pending Review": ("The record is released as it was imported.",
                       "ERPNext keeps the record, but later HERO changes to it are not imported."),
}


def report(link_name: str, source_instance: str) -> dict:
    """Report for one open conflict link of ``source_instance``."""
    link = frappe.get_doc("HERO Integration Link", link_name)
    instance = link.source_instance or "default"
    if instance != source_instance:
        raise frappe.PermissionError("The conflict belongs to a different HERO source.")
    if link.sync_status not in ("Conflict", "Pending Review"):
        raise frappe.ValidationError("The record has no open conflict.")
    detail = link.conflict_detail or ""
    recheck = detail.startswith(RECHECK_PREFIX)
    problem = detail[len(RECHECK_PREFIX):] if recheck else detail
    rules = (_product_rules(link, instance) if link.source_type == "supply_product"
             else _run_rules(link.source_type, link.source_id, instance))
    kind, fields = _fields(link, problem, rules)
    approve, decline = _DECISIONS[link.sync_status]
    return {
        "name": link.name, "kind": kind, "hero_id": link.source_id, "source_type": link.source_type,
        "area": _area_label(link.source_type), "doctype": link.target_doctype or "", "record": link.target_name or "",
        "record_exists": bool(link.target_doctype and link.target_name
                              and frappe.db.exists(link.target_doctype, link.target_name)),
        "status": "Recheck requested" if recheck else
                  ("Confirmation pending" if link.sync_status == "Pending Review" else "Conflict"),
        "problem": problem or "Conflict requires review.", "explanation": _EXPLANATIONS[kind],
        "fields": fields, "decidable": True, "approve_effect": approve, "decline_effect": decline,
    }


def record_report(record_name: str, source_instance: str) -> dict:
    """Report for a HERO record the latest run could not import (nothing to decide)."""
    record = frappe.get_doc("HERO Integration Record", record_name)
    run_instance = frappe.db.get_value("HERO Integration Run", record.run, "source_instance") if record.run else None
    if (run_instance or "default") != source_instance:
        raise frappe.PermissionError("The conflict belongs to a different HERO source.")
    detail = record.detail or record.result or ""
    names = _FIELD_LIST.match(detail) or re.match(r"^HERO response omits the source of: (?P<fields>[\w, ]+)\.$", detail)
    doctype = _target_doctype(record.source_type)
    rules = _run_rules(record.source_type, record.source_id, source_instance, run=record.run)
    fields = [_field(record.source_type, doctype, None, name, rules) for name in _split(names)] if names else []
    return {
        "name": record.name, "kind": "record", "hero_id": record.source_id, "source_type": record.source_type,
        "area": _area_label(record.source_type), "doctype": doctype, "record": "", "record_exists": False,
        "status": "Not imported", "problem": detail or "Conflict requires review.",
        "explanation": _EXPLANATIONS["record"], "fields": fields, "decidable": False, "run": record.run,
        "approve_effect": "", "decline_effect": "Fix the record in HERO; the next run imports it.",
    }


# ---------------------------------------------------------------- field resolution

def _fields(link, problem: str, rules) -> tuple[str, list[dict]]:
    doctype, name = link.target_doctype, link.target_name
    if match := _FIELD_LIST.match(problem):
        return "field", [_field(link.source_type, doctype, name, field, rules) for field in _split(match)]
    if match := _NUMBER_DRIFT.match(problem):
        entry = _field(link.source_type, doctype, name, match["field"], rules)
        entry["hero_value"] = _literal(match["new"])
        entry["previous_hero_value"] = _literal(match["old"])
        return "number", [entry]
    if link.source_type == "supply_product":
        if match := _PRICE.match(problem):
            entry = _field(link.source_type, "Item Price", match["name"], "price_list_rate", rules)
            entry["hero_variable"] = dict(rules or ()).get(("Item Price", match["list"]))
            return "product", [entry]
        return "product", [_field(link.source_type, doctype, name, field, rules) for field in _changed_item_fields(link)]
    for reason, field in _REASON_FIELDS:
        if reason in problem:
            return "review", [_field(link.source_type, doctype, name, field, rules)]
    return ("review" if link.sync_status == "Pending Review" else "other"), []


def _split(match) -> list[str]:
    return [item.strip() for item in match["fields"].split(",") if item.strip()][:MAX_FIELDS]


def _field(source_type: str, doctype: str, name: str | None, field: str, rules) -> dict:
    entry = {"doctype": doctype or "", "field": field, "label": field,
             "hero_variable": _hero_variable(source_type, doctype, field, rules), "value": None}
    if doctype and frappe.db.exists("DocType", doctype):
        meta = frappe.get_meta(doctype)
        docfield = meta.get_field(field)
        if docfield is not None:
            entry["label"] = docfield.label or field
        if name and docfield is not None and frappe.db.exists(doctype, name):
            entry["value"] = _display(frappe.db.get_value(doctype, name, field))
    return entry


def _hero_variable(source_type: str, doctype: str, field: str, rules) -> str | None:
    """HERO path of the rule that writes ``doctype.field``; None when it cannot be proven."""
    if not rules:
        return None
    exact = [path for (rule_doctype, rule_field), path in rules if rule_field == field and rule_doctype == doctype]
    # Customer contact fields are written to the Contact of the Customer.
    paths = exact or [path for (rule_doctype, rule_field), path in rules if rule_field == field]
    return ", ".join(dict.fromkeys(paths)) or None


def _product_rules(link, source_instance: str) -> tuple | None:
    """(doctype, field) → HERO path of the HeroProducts mapping the article was last imported under.

    Item Price rules are keyed by their price list. None when the mapping in
    force today is not the one recorded on the link (rules changed since)."""
    from hero_integration.services import direct_runtime

    try:
        read = direct_runtime.resolve_read("HeroProducts", source_instance)
    except Exception:  # an unreadable mapping names no variable
        return None
    recorded = (getattr(link, "mapping_version", "") or "").split(":", 1)
    if len(recorded) != 2 or recorded[1] != read.mapping_version[:len(recorded[1])]:
        return None
    return tuple((((spec.target_doctype, spec.relation) if spec.target_doctype == "Item Price"
                   else (spec.target_doctype, spec.target_field)), spec.source_path)
                 for spec in read.plan.definitions if spec.purpose != "Source Identity")


def _run_rules(source_type: str, source_id: str, source_instance: str, run: str | None = None) -> tuple | None:
    """(doctype, field) → HERO path of exactly the rules frozen onto the run of
    ``source_instance`` that processed this record. HERO ids repeat across
    sources, so a run of another source never counts. None when no such run
    exists or its rules changed."""
    operation = _operation(source_type)
    if not operation or source_type == "supply_product":
        return None
    if not run:
        rows = frappe.db.sql("""SELECT x.run FROM `tabHERO Integration Record` x
            JOIN `tabHERO Integration Run` r ON r.name = x.run
            WHERE x.source_type=%s AND x.source_id=%s AND r.mode='Execute' AND IFNULL(r.graphql_mapping, '') != ''
              AND COALESCE(NULLIF(r.source_instance, ''), 'default')=%s
            ORDER BY x.creation DESC LIMIT 1""", (source_type, str(source_id), source_instance))
        run = rows[0][0] if rows else None
    if not run or not frappe.db.exists("HERO Integration Run", run):
        return None
    document = frappe.get_doc("HERO Integration Run", run)
    if (getattr(document, "source_instance", "") or "default") != source_instance:
        return None
    if not getattr(document, "graphql_mapping", ""):
        return None  # without a frozen mapping every active rule would qualify
    from hero_integration.services.migration import _mapping_plan_for_run

    try:
        plan = _mapping_plan_for_run(document, operation)
    except Exception:  # the run's service, mapping or rule revisions are no longer reconstructable
        return None
    return tuple(((spec.target_doctype, spec.target_field), spec.source_path)
                 for spec in plan.definitions if spec.source_path and spec.target_field)


def _changed_item_fields(link) -> list[str]:
    """Item fields whose current value differs from the per-field import baseline."""
    import json

    from hero_integration.services.product_migration import owned_field_digests

    try:
        baseline = (json.loads(link.target_field_hashes or "{}") or {}).get("fields") or {}
    except (TypeError, ValueError, AttributeError):
        baseline = {}
    if not isinstance(baseline, dict) or not baseline or not frappe.db.exists("Item", link.target_name):
        return []  # imported before per-field baselines existed
    fields = tuple(baseline)  # the fields mapped when the baseline was taken
    current = owned_field_digests(frappe.get_doc("Item", link.target_name), fields)
    return [field for field in fields if baseline[field] != current[field]]


def _display(value) -> str | None:
    if value is None or value == "":
        return None
    text = frappe.utils.strip_html_tags(str(value)).strip() if isinstance(value, str) else str(value)
    return text if len(text) <= MAX_VALUE else text[:MAX_VALUE - 1] + "…"


def _literal(text: str) -> str:
    try:
        return str(ast.literal_eval(text))
    except (ValueError, SyntaxError):
        return text.strip("'\"")


def _areas():
    from hero_integration.services.sync_page import AREAS

    return {item.source_type: item for item in AREAS}


def _area_label(source_type: str) -> str:
    item = _areas().get(source_type)
    if item:
        return item.label
    return {"supply_product": "Products", "file_upload": "Documents", "customer_address": "Contacts"}.get(source_type, source_type)


def _operation(source_type: str) -> str:
    item = _areas().get(source_type)
    return item.operation if item else ("HeroCustomers" if source_type == "customer_address" else "")


def _target_doctype(source_type: str) -> str:
    return {"customer": "Customer", "project": "Project", "Task": "Task", "CalendarEvent": "Event",
            "supply_product": "Item", "customer_address": "Address"}.get(source_type, "")
