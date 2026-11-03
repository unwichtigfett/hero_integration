"""Labels of the app's fields on ERPNext documents: plain names, no "HERO" prefix.

Where a value comes from is metadata, not part of the label: the fieldname keeps
its ``custom_hero_`` prefix and the Custom Field records the module
``HERO Integration``.
"""
from __future__ import annotations

import re

PROVENANCE_MODULE = "HERO Integration"
FIELD_PREFIX = "custom_hero_"
_PREFIX = re.compile(r"^\s*HERO(?:[\s\-_]+|$)", re.IGNORECASE)

# (DocType, fieldname) -> label. Labels are English; translations/de.csv has the German names.
LABELS = {
    ("Customer", "custom_hero_source_id"): "Source ID",
    ("Customer", "custom_hero_customer_number"): "Customer Number",
    ("Customer", "custom_hero_customer_category"): "Customer Category",
    ("Customer", "custom_hero_customer_type"): "Customer Type",
    ("Customer", "custom_hero_is_contact_person"): "Is Contact Person",
    ("Project", "custom_hero_source_id"): "Source ID",
    ("Project", "custom_hero_related_project_id"): "Project ID",
    ("Project", "custom_hero_display_id"): "Project Nr.",
    ("Project", "custom_hero_source_created"): "Creation",
    ("Project", "custom_hero_source_modified"): "Modified",
    ("Project", "custom_hero_address_full_address"): "Project Address",
    ("Project", "custom_hero_partner_notes"): "Notes",
    ("Project", "custom_hero_project_number"): "Project Number",
    ("Project", "custom_hero_project_type"): "Project Kind",
    ("Project", "custom_hero_customer_title"): "Customer Title",
    ("Project", "custom_hero_customer_full_name"): "Customer Full Name",
    ("Project", "custom_hero_observed_status_id"): "Status ID",
    ("Project", "custom_hero_observed_status_name"): "Status Name",
    ("Project", "custom_hero_observed_status_short_name"): "Status Short Name",
    ("Project", "custom_hero_observed_status_status_code"): "Status Code",
    ("Project", "custom_hero_observed_status_step_id"): "Status Step ID",
    ("Project", "custom_hero_observed_status_created"): "Status Created",
    ("Project", "custom_hero_observed_status_modified"): "Status Modified",
    ("Event", "custom_hero_source_id"): "Source ID",
    ("Event", "custom_hero_key"): "Source Key",
    ("Event", "custom_hero_source_instance"): "Source Instance",
    ("Event", "custom_hero_type"): "Appointment Type Code",
    ("Event", "custom_hero_localized_type"): "Appointment Type",
    ("Event", "custom_hero_color"): "Appointment Color",
    ("Event", "custom_hero_created"): "Source Created",
    ("Event", "custom_hero_modified"): "Source Modified",
    ("Event", "custom_hero_deleted"): "Deleted",
    ("Event", "custom_hero_is_done"): "Done",
    ("Event", "custom_hero_project"): "Project",
    ("Event", "custom_hero_project_match_id"): "Project Reference",
    ("Event", "custom_hero_partners"): "Partners",
    ("Event", "custom_hero_partner_id"): "Partner ID",
    ("Event", "custom_hero_resources"): "Resources",
    ("Event", "custom_hero_provider"): "Provider",
    ("Event", "custom_hero_readonly"): "Read Only",
    ("Event", "custom_hero_category_id"): "Category ID",
    ("Event", "custom_hero_is_recurring"): "Recurring",
    ("Event", "custom_hero_original_start"): "Original Start",
    ("Event", "custom_hero_original_end"): "Original End",
    ("Task", "custom_hero_due_date"): "External Deadline",
    ("Task", "custom_hero_assignee"): "Responsible",
    ("Item", "custom_hero_manufacturer_nr"): "Manufacturer Part No",
    ("Item", "custom_hero_category"): "Category",
    ("Item", "custom_hero_supplier_id"): "Supplier Part No",
}


# Technical sync fields that duplicate a visible native value or only identify the source record.
# They stay (the sync compares against them) but are hidden on the form.
HIDDEN = {
    "Event": ("custom_hero_source_id", "custom_hero_key", "custom_hero_source_instance", "custom_hero_deleted",
              "custom_hero_type", "custom_hero_project_match_id", "custom_hero_category_id",
              "custom_hero_partner_id", "custom_hero_readonly"),
    # The HERO due date is also the native Expected End Date; this copy is the sync baseline.
    "Task": ("custom_hero_due_date",),
}


def strip_prefix(label: str | None) -> str:
    """``HERO Source ID`` -> ``Source ID``; a label that is only the prefix stays."""
    text = (label or "").strip()
    stripped = _PREFIX.sub("", text, count=1).strip()
    return stripped or text


def display_label(doctype: str, fieldname: str, label: str | None = None) -> str:
    """The label an app field gets on ``doctype``."""
    return LABELS.get((doctype, fieldname)) or strip_prefix(label) or fieldname


def relabel_fields() -> dict:
    """Give every app field on ERPNext documents and HERO child tables its plain label (idempotent)."""
    import frappe

    changed, doctypes = 0, set()
    for row in frappe.get_all("Custom Field", filters={"fieldname": ["like", FIELD_PREFIX + "%"]},
                              fields=["name", "dt", "fieldname", "label", "module"], limit_page_length=0):
        label = display_label(row.dt, row.fieldname, row.label)
        values = {key: value for key, value in (("label", label), ("module", PROVENANCE_MODULE))
                  if (row.get(key) or "") != value}
        if values:
            frappe.db.set_value("Custom Field", row.name, values, update_modified=False)
            changed += 1
            doctypes.add(row.dt)
    # Fields of the app's own child tables (e.g. Event partners and resources).
    owned = frappe.get_all("DocType", filters={"module": PROVENANCE_MODULE, "custom": 1, "istable": 1},
                           pluck="name", limit_page_length=0)
    for row in frappe.get_all("DocField", filters={"parent": ["in", owned or [""]], "parenttype": "DocType"},
                              fields=["name", "parent", "fieldname", "label"], limit_page_length=0):
        label = strip_prefix(row.label)
        if label != (row.label or ""):
            frappe.db.set_value("DocField", row.name, "label", label, update_modified=False)
            changed += 1
            doctypes.add(row.parent)
    hidden = hide_technical_fields()
    for doctype in sorted(doctypes):
        frappe.clear_cache(doctype=doctype)
    return {"fields_relabelled": changed, "doctypes": sorted(doctypes), "fields_hidden": hidden}


def hide_technical_fields() -> int:
    """Hide the technical sync fields of ``HIDDEN`` that exist (idempotent)."""
    import frappe
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    hidden = 0
    for doctype, fieldnames in HIDDEN.items():
        present = set(frappe.get_all("Custom Field", filters={"dt": doctype, "fieldname": ["in", fieldnames]},
                                     pluck="fieldname", limit_page_length=0))
        for fieldname in fieldnames:
            if fieldname in present:
                make_property_setter(doctype, fieldname, "hidden", "1", "Check", validate_fields_for_doctype=False)
                hidden += 1
        if present:
            frappe.clear_cache(doctype=doctype)
    return hidden
