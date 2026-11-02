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

import frappe

SALUTATIONS = ("Herr", "Frau", "Familie", "Eheleute", "Dr")


def _site_connected() -> bool:
    """False in offline test doubles; install, migrate and adoption always run on a connected site."""
    try:
        return bool(frappe.local.site) and frappe.db is not None
    except Exception:
        return False


def ensure_app_records() -> None:
    if not _site_connected():
        return
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
