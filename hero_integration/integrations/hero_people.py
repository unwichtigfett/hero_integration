"""Source-bound employee lookups and the legacy administrative employee-import RPC."""
from __future__ import annotations

import frappe

PARTNER_FIELD, USER_ID_FIELD = "custom_hero_partner_id", "custom_hero_user_id"


def _id(value) -> str | None:
    return str(value) if type(value) is int and value > 0 else None


def _scoped_link(source_type, value, source_instance):
    """Legacy User ID fields cannot establish which HERO source owns a person."""
    key = _id(value)
    if not key or not isinstance(source_instance, str) or not source_instance.strip():
        return None
    rows = frappe.get_all("HERO Integration Link", filters={
        "source_system": "HERO", "source_instance": source_instance,
        "source_type": source_type, "source_id": key,
    }, fields=["target_doctype", "target_name", "sync_status"], limit_page_length=2)
    if len(rows) != 1 or rows[0].sync_status != "Synchronized":
        return None
    return rows[0]


def user_by_partner(partner_id, *, source_instance=None) -> str | None:
    """Resolve a partner through its source-bound Employee and User links."""
    link = _scoped_link("partner", partner_id, source_instance)
    if not link or link.target_doctype != "Employee":
        return None
    user = frappe.db.get_value("Employee", link.target_name, "user_id")
    if not user or not frappe.db.get_value("User", user, "enabled"):
        return None
    links = frappe.get_all("HERO Integration Link", filters={
        "source_system": "HERO", "source_instance": source_instance,
        "source_type": "User", "target_doctype": "User", "target_name": user,
    }, fields=["sync_status"], limit_page_length=2)
    return user if len(links) == 1 and links[0].sync_status == "Synchronized" else None


def user_by_hero_user(user_id, *, source_instance=None) -> str | None:
    """The enabled User with a unique synchronized binding in this source."""
    link = _scoped_link("User", user_id, source_instance)
    if not link or link.target_doctype != "User":
        return None
    return link.target_name if frappe.db.get_value("User", link.target_name, "enabled") else None


def _existing_user(email: str, partner_id: str | None = None, *, source_instance=None) -> str | None:
    if partner_id and str(partner_id).isdigit():
        user = user_by_partner(int(partner_id), source_instance=source_instance)
        if user:
            return user
    # An exact, unambiguous login remains usable for first adoption. The caller
    # checks existing bindings and privileges before assigning any ownership.
    rows = frappe.db.sql("SELECT name FROM `tabUser` WHERE LOWER(email)=%s OR LOWER(name)=%s LIMIT 2", (email, email))
    return rows[0][0] if len(rows) == 1 else None


@frappe.whitelist(methods=["POST"])
def sync_hero_people() -> dict:
    """Queue the approved Employee reader and return its durable migration run."""
    from hero_integration.api import sync_area_start
    return sync_area_start("employees")


def sync_daily() -> None:
    """Old Scheduled Job Type entries may still call this until Frappe syncs hooks."""
    return None
