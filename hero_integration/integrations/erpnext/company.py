"""Validated per-site ERPNext Company selection for HERO projects."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompanySelection:
    name: str = ""
    reason: str = ""

    @property
    def is_valid(self) -> bool:
        return bool(self.name)


def confirmed_project_company(frappe_api=None, settings=None) -> CompanySelection:
    if frappe_api is None:
        import frappe as frappe_api

    settings = settings if settings is not None else frappe_api.get_single("HERO Integration Settings")
    company = (settings.erpnext_company or "").strip()
    if not company:
        return CompanySelection(reason="No ERPNext Company is selected in HERO Integration Settings.")
    if not settings.erpnext_company_confirmed:
        return CompanySelection(reason="The selected ERPNext Company is not confirmed for HERO projects.")
    if not frappe_api.db.exists("Company", company):
        return CompanySelection(reason="The selected ERPNext Company no longer exists.")
    if not frappe_api.has_permission("Company", "read", company):
        return CompanySelection(reason="The current user cannot read the selected ERPNext Company.")
    if frappe_api.get_meta("Company").has_field("disabled") and frappe_api.db.get_value("Company", company, "disabled"):
        return CompanySelection(reason="The selected ERPNext Company is disabled.")
    return CompanySelection(name=company)
