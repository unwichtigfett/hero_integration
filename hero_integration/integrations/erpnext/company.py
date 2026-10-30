"""The ERPNext Company that owns every HERO project.

One HERO account belongs to one business, so there is no per-integration
Company setting: HERO projects use the ERPNext Default Company (Global
Defaults). A site with exactly one Company uses that Company.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompanySelection:
    name: str = ""
    reason: str = ""

    @property
    def is_valid(self) -> bool:
        return bool(self.name)


def project_company(frappe_api=None) -> CompanySelection:
    if frappe_api is None:
        import frappe as frappe_api

    company = str(frappe_api.db.get_single_value("Global Defaults", "default_company") or "").strip()
    if not company:
        companies = frappe_api.get_all("Company", pluck="name", limit_page_length=2)
        if len(companies) != 1:
            return CompanySelection(reason="Set a Default Company in ERPNext Global Defaults; HERO projects use it.")
        company = companies[0]
    if not frappe_api.db.exists("Company", company):
        return CompanySelection(reason="The ERPNext Default Company no longer exists.")
    if not frappe_api.has_permission("Company", "read", company):
        return CompanySelection(reason="The current user cannot read the ERPNext Default Company.")
    if frappe_api.get_meta("Company").has_field("disabled") and frappe_api.db.get_value("Company", company, "disabled"):
        return CompanySelection(reason="The ERPNext Default Company is disabled.")
    return CompanySelection(name=company)
