"""App-owned records and form layout, for fresh installs as well as upgrades.

Patches only run on existing sites; ``bench install-app`` marks them as done
without running them.  Everything the v0_2 patches created is therefore also
created here:

- ``ensure_app_records`` (after_install and after_migrate, create-only): role
  "HERO Writeback", the trade Project Types, the Project Phase and Volume
  fields, the Task HERO Assignee field and the German salutations.
- ``apply_form_layout`` (after_install and after a mapping package adoption):
  the Project form Property Setters (labels, read-only, hidden, order).  It is
  not run on every migrate, so an administrator's later form changes stay.
"""

from __future__ import annotations

SALUTATIONS = ("Herr", "Frau", "Familie", "Eheleute", "Dr")


def _site_connected() -> bool:
    """False in offline test doubles; install, migrate and adoption always run on a connected site.

    ``frappe`` is imported at call time so a test that replaces the module is seen here too.
    """
    import frappe

    try:
        return (bool(frappe.local.site) and frappe.db is not None
                and callable(getattr(frappe, "get_meta", None)) and hasattr(frappe, "_"))
    except Exception:
        return False


def ensure_app_records() -> None:
    if not _site_connected():
        return
    import frappe
    from hero_integration.integrations.project_form import ensure_phase_field
    from hero_integration.integrations.task_form import setup_task_form
    from hero_integration.services.project_naming import TRADE_CATALOG

    if not frappe.db.exists("Role", "HERO Writeback"):
        frappe.get_doc({"doctype": "Role", "role_name": "HERO Writeback", "desk_access": 1}).insert(ignore_permissions=True)
    for trade_name in TRADE_CATALOG.values():
        if not frappe.db.exists("Project Type", trade_name):
            frappe.get_doc({"doctype": "Project Type", "project_type": trade_name}).insert(ignore_permissions=True)
    for name in SALUTATIONS:
        if not frappe.db.exists("Salutation", name):
            frappe.get_doc({"doctype": "Salutation", "salutation": name}).insert(ignore_permissions=True)
    ensure_phase_field()
    setup_task_form()


def apply_form_layout() -> dict:
    if not _site_connected():
        return {}
    from hero_integration.integrations.project_form import setup_project_form

    return setup_project_form()


def verify_install() -> dict:
    """bench execute: check a fresh install (CI). Raises when something the app needs is missing."""
    import frappe
    from hero_integration.install import HERO_CUSTOM_FIELDS
    from hero_integration.integrations.project_form import PHASE_FIELD, VOLUME_FIELD
    from hero_integration.services.project_naming import TRADE_CATALOG
    from hero_integration.services.task_lifecycle import ASSIGNEE_FIELD

    missing = []
    fields = [(doctype, item["fieldname"]) for doctype, items in HERO_CUSTOM_FIELDS.items() for item in items]
    fields += [("Project", PHASE_FIELD), ("Project", VOLUME_FIELD), ("Task", ASSIGNEE_FIELD)]
    for doctype, fieldname in fields:
        if not frappe.get_meta(doctype).has_field(fieldname):
            missing.append(f"field {doctype}.{fieldname}")
    if not frappe.db.exists("Role", "HERO Writeback"):
        missing.append("role HERO Writeback")
    missing += [f"project type {name}" for name in TRADE_CATALOG.values() if not frappe.db.exists("Project Type", name)]
    missing += [f"salutation {name}" for name in SALUTATIONS if not frappe.db.exists("Salutation", name)]
    for doctype in ("HERO Integration Settings", "HERO Integration Link", "HERO Integration Run", "HERO Frappe Mapping",
                    "HERO GraphQL Service", "HERO GraphQL Mapping", "HERO GraphQL Schema Snapshot"):
        if not frappe.db.exists("DocType", doctype):
            missing.append(f"doctype {doctype}")
    settings = frappe.get_single("HERO Integration Settings")
    unsafe = []
    if int(getattr(settings, "allow_graphql_mutation_execution", 0) or 0):
        unsafe.append("mutation execution is enabled by default")
    result = {
        "missing": missing, "unsafe_defaults": unsafe,
        "mappings": frappe.db.count("HERO Frappe Mapping"),
        "active_mappings": frappe.db.count("HERO Frappe Mapping", {"status": "Active"}),
        "approved_read_services": frappe.db.count("HERO GraphQL Service", {"execution_status": "Approved"}),
        "snapshots": frappe.db.count("HERO GraphQL Schema Snapshot", {"analysis_status": "Validated"}),
    }
    if missing or unsafe:
        raise frappe.ValidationError("HERO Integration install check failed: " + "; ".join(missing + unsafe))
    return result
