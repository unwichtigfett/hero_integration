"""Display HERO data through Project Property Setters, preserving field definitions.
The native Project Type holds the trade."""

from __future__ import annotations

import json

import frappe

# fieldname -> label shown in the form (HERO-owned, therefore read-only).
SHOWN = {
    "custom_hero_related_project_id": "Project ID",
    "custom_hero_display_id": "Project Nr.",
    "custom_hero_source_created": "Creation",
    "custom_hero_source_modified": "Modified",
    "custom_hero_address_full_address": "Project Address",
}
# HERO notes live in the native Notes field; it stays editable and is sent with "Send to HERO".
EDITABLE: set[str] = set()
PHASE_FIELD = "custom_hero_phase"  # app-owned display field: the HERO status step name
VOLUME_FIELD = "custom_hero_volume"  # HERO volume: project volume in euros (read-only)
HIDDEN = (
    "custom_hero_observed_status_name",
    "custom_hero_source_id", "custom_hero_project_number", "custom_hero_customer_title",
    "custom_hero_customer_full_name", "custom_hero_project_type",
    # Superseded by the native Notes (older mapping revisions only).
    "custom_hero_partner_notes", "custom_hero_project_type_copy",
    "custom_hero_observed_status_id", "custom_hero_observed_status_status_code",
    "custom_hero_observed_status_short_name", "custom_hero_observed_status_step_id",
    "custom_hero_observed_status_created", "custom_hero_observed_status_modified",
)
# Placement: (field, after) applied in this order to the current field order.
PLACEMENT = (
    ("custom_hero_related_project_id", "project_name"),
    ("custom_hero_display_id", "custom_hero_related_project_id"),
    (PHASE_FIELD, "project_type"),
    ("custom_hero_source_created", "column_break_5"),
    ("custom_hero_source_modified", "custom_hero_source_created"),
    ("custom_hero_address_full_address", "custom_hero_source_modified"),
    (VOLUME_FIELD, "custom_hero_address_full_address"),
    ("custom_hero_lead_source", VOLUME_FIELD),
    ("custom_hero_designation", "custom_hero_display_id"),
    ("custom_hero_project_manager", PHASE_FIELD),
)


def setup_project_form() -> dict:
    """Create the trade Project Types, the HERO Writeback role and the Project form Property Setters (idempotent)."""
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    from hero_integration.services.project_naming import TRADE_CATALOG

    if not frappe.db.exists("Role", "HERO Writeback"):
        frappe.get_doc({"doctype": "Role", "role_name": "HERO Writeback", "desk_access": 1}).insert(ignore_permissions=True)
    created_types = []
    for trade_name in TRADE_CATALOG.values():
        if not frappe.db.exists("Project Type", trade_name):
            frappe.get_doc({"doctype": "Project Type", "project_type": trade_name}).insert(ignore_permissions=True)
            created_types.append(trade_name)
    ensure_phase_field()
    meta = frappe.get_meta("Project")
    present = {field.fieldname for field in meta.fields}
    for fieldname, label in SHOWN.items():
        if fieldname in present:
            make_property_setter("Project", fieldname, "label", label, "Data", validate_fields_for_doctype=False)
            read_only = "0" if fieldname in EDITABLE else "1"
            make_property_setter("Project", fieldname, "read_only", read_only, "Check", validate_fields_for_doctype=False)
            make_property_setter("Project", fieldname, "hidden", "0", "Check", validate_fields_for_doctype=False)
    for fieldname in HIDDEN:
        if fieldname in present:
            make_property_setter("Project", fieldname, "hidden", "1", "Check", validate_fields_for_doctype=False)
    order = field_order([field.fieldname for field in meta.fields])
    make_property_setter("Project", None, "field_order", json.dumps(order), "Data", for_doctype=True,
                         validate_fields_for_doctype=False)
    frappe.clear_cache(doctype="Project")
    return {"project_types_created": created_types, "fields_shown": len(SHOWN), "fields_hidden": len(HIDDEN)}


def field_order(current: list[str]) -> list[str]:
    """Move the shown HERO fields to their place; every other field keeps its relative order."""
    order = list(current)
    for fieldname, after in PLACEMENT:
        if fieldname in order and after in order:
            order.remove(fieldname)
            order.insert(order.index(after) + 1, fieldname)
    return order


def project_type_for_trade(trade_code: str | None) -> str | None:
    """The native Project Type (Gewerk) of a HERO trade, if that Project Type exists."""
    from hero_integration.services.project_naming import TRADE_CATALOG

    name = TRADE_CATALOG.get((trade_code or "").upper())
    return name if name and frappe.db.exists("Project Type", name) else None


def ensure_phase_field() -> None:
    """Create-only: the read-only Phase field shows the HERO status step, as HERO does."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    if not frappe.db.exists("Custom Field", {"dt": "Project", "fieldname": VOLUME_FIELD}):
        create_custom_fields({"Project": [{
            "fieldname": VOLUME_FIELD, "label": "Project Volume", "fieldtype": "Currency", "read_only": 1,
            "insert_after": "custom_hero_address_full_address",
            "description": "Project volume in euros from HERO.",
        }]}, update=False)
    if not frappe.db.exists("Custom Field", {"dt": "Project", "fieldname": PHASE_FIELD}):
        create_custom_fields({"Project": [{
            "fieldname": PHASE_FIELD, "label": "Phase", "fieldtype": "Data", "read_only": 1,
            "insert_after": "project_type", "in_standard_filter": 1,
            "description": "Current HERO status step; change it with Send to HERO.",
        }]}, update=False)


def refresh_project_phases(step_names: dict) -> int:
    """Set Phase of HERO projects from their stored status step ID; returns how many changed."""
    if not step_names or not frappe.db.has_column("Project", PHASE_FIELD):
        return 0
    bound = set(frappe.get_all("HERO Integration Link", filters={"source_system": "HERO", "target_doctype": "Project"},
                               pluck="target_name", limit_page_length=0))
    changed = 0
    for project in frappe.get_all("Project", fields=["name", "custom_hero_observed_status_step_id", PHASE_FIELD],
                                  limit_page_length=0):
        step = str(project.get("custom_hero_observed_status_step_id") or "")
        name = step_names.get(int(step)) if step.isdigit() else None
        if project.name in bound and name and project.get(PHASE_FIELD) != name:
            frappe.db.set_value("Project", project.name, PHASE_FIELD, name, update_modified=False)
            changed += 1
    return changed


def refresh_project_phases_from_hero() -> dict:
    """One-time/backfill: read the step names from HERO and fill Phase (bench execute)."""
    frappe.only_for("System Manager")
    from types import SimpleNamespace

    from hero_integration.services.project_send import _read_client
    from hero_integration.sources.hero import GraphQLServiceSource

    source = GraphQLServiceSource(_read_client(), SimpleNamespace(), SimpleNamespace(), "default")
    changed = refresh_project_phases(source.project_status_steps())
    frappe.db.commit()
    return {"projects_updated": changed}
