"""One per-user Desk design package; business fields and permissions are invariant."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import frappe


FIELD = "custom_hero_design_mode"
MODES = frozenset({"Native", "Custom"})
PRESENTATION_FIELDS = {
    "custom_project_documents_tab": "Tab Break",
    "custom_project_images_tab": "Tab Break",
    "custom_project_documents_gallery": "Attachment Gallery",
    "custom_project_images_gallery": "Attachment Gallery",
    "custom_project_settings_section": "Section Break",
    "custom_project_progress_section": "Section Break",
    "custom_project_source_section": "Section Break",
}
LABELS = {
    "customer_details": {"", "Project and Customer"},
    "connections_tab": {"Project Tasks and Records"},
    "more_info_tab": {"Project Schedule"},
    "monitor_progress_tab": {"Project Assignments"},
    "costing_tab": {"Project Advanced Settings"},
    "section_break_18": {"Project Dates"},
    "users_section": {"Project Team"},
    "users": {"Project Team"},
}
COLLAPSE = {"customer_details": 0, "section_break0": 0, "section_break_18": 0,
    "users_section": 0, "project_details": 1, "margin": 1}


def _current_user():
    local = getattr(frappe, "local", None)
    session = getattr(local, "session", None) if local is not None else getattr(frappe, "session", None)
    return getattr(session, "user", None)


def get_design_mode(user=None):
    user = user or _current_user()
    if not user or user == "Guest":
        return "Native"
    if not frappe.db.has_column("User", FIELD):
        return "Custom"  # Preserve the deployed appearance until schema setup.
    mode = frappe.db.get_value("User", user, FIELD) or "Custom"
    return mode if mode in MODES else "Native"


def ensure_field():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    field = frappe.get_meta("User").get_field(FIELD)
    if field:
        if (field.fieldtype != "Select" or set((field.options or "").splitlines()) != MODES
                or field.permlevel):
            frappe.throw(frappe._("The Design preference field has an incompatible definition."))
        return
    create_custom_fields({"User": [{
        "fieldname": FIELD, "label": "Design", "fieldtype": "Select", "options": "Native\nCustom",
        "default": "Custom", "hidden": 1, "no_copy": 1, "insert_after": "desk_theme",
    }]}, update=False)


def extend_bootinfo(bootinfo):
    mode = get_design_mode()
    bootinfo["hero_design_mode"] = mode
    # Navy is part of Custom. Retain the stored color preference so returning to
    # Custom restores it; Native renders the closest built-in dark palette.
    if mode == "Native" and bootinfo.get("desk_theme") == "Navy":
        bootinfo["desk_theme"] = "Dark"


@frappe.whitelist(methods=["POST"])
def set_design_mode(mode: str):
    user = _current_user()
    if not user or user == "Guest":
        frappe.throw(frappe._("Log in to change your Design preference."), frappe.PermissionError)
    if not isinstance(mode, str) or mode not in MODES:
        frappe.throw(frappe._("Unsupported Design preference."))
    if not frappe.db.has_column("User", FIELD):
        frappe.throw(frappe._("The Design preference is not installed. Ask an administrator."))
    # Same own-user preference boundary as native theme changes. No caller-
    # supplied target, role, Project metadata or business record is updated.
    frappe.db.set_value("User", user, FIELD, mode)
    frappe.clear_document_cache("User", user)
    frappe.clear_cache(user=user)
    return {"mode": mode}


def native_project_meta(meta, original_fields):
    """Copy presentation only; never mutate shared cache or security attributes."""
    result = deepcopy(meta)
    fields = [field for field in result["fields"]
        if not (field["fieldname"] in PRESENTATION_FIELDS
            and PRESENTATION_FIELDS[field["fieldname"]] == field["fieldtype"])]
    by_name = {field["fieldname"]: field for field in fields}
    originals = {field["fieldname"]: field for field in original_fields}
    order = [field["fieldname"] for field in original_fields if field["fieldname"] in by_name]
    pending = [field for field in fields if field["fieldname"] not in order]
    while pending:
        progressed = False
        for field in list(pending):
            after = field.get("insert_after")
            if after in order:
                order.insert(order.index(after) + 1, field["fieldname"])
                pending.remove(field); progressed = True
        if not progressed:
            order.extend(field["fieldname"] for field in pending)
            break
    for name, values in LABELS.items():
        if name in by_name and name in originals and (by_name[name].get("label") or "") in values:
            by_name[name]["label"] = originals[name].get("label", "")
    for name, expected in COLLAPSE.items():
        if name in by_name and name in originals and (by_name[name].get("collapsible") or 0) == expected:
            by_name[name]["collapsible"] = originals[name].get("collapsible", 0)
    result["fields"] = [by_name[name] for name in order]
    for index, field in enumerate(result["fields"], 1):
        field["idx"] = index
    result["field_order"] = json.dumps(order)
    return result


@frappe.whitelist(methods=["GET", "POST"])
def getdoctype(doctype: str, with_parent: int | bool = False):
    """Use native loading, then project a private Native layout for this caller."""
    from frappe.desk.form.load import getdoctype as native_getdoctype

    native_getdoctype(doctype, with_parent)
    if get_design_mode() != "Native":
        return
    documents = frappe.response.get("docs") or []
    if not any(doc.get("name") == "Project" and doc.get("doctype") == "DocType" for doc in documents):
        return
    path = Path(frappe.get_app_path("erpnext", "projects", "doctype", "project", "project.json"))
    original = json.loads(path.read_text())["fields"]
    frappe.response["docs"] = [
        native_project_meta(doc, original) if doc.get("name") == "Project" and doc.get("doctype") == "DocType" else doc
        for doc in documents
    ]
