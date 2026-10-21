from __future__ import annotations

import frappe
from frappe.model.document import Document

from hero_integration.client.graphql import HeroClientError, validate_endpoint
from hero_integration.integrations.erpnext.company import confirmed_project_company


class HEROIntegrationSettings(Document):
    def validate(self):
        frappe.only_for("System Manager")
        if self.has_value_changed("erpnext_company"):
            self.erpnext_company_confirmed = 0
        if self.hero_api_url:
            hosts = {line.strip().lower() for line in (self.allowed_hero_hosts or "").splitlines() if line.strip()}
            try:
                validate_endpoint(self.hero_api_url, hosts)
            except HeroClientError as exc:
                raise ValueError(str(exc)) from exc
        self._validate_company_selection()

    def _validate_company_selection(self):
        if not self.erpnext_company_confirmed:
            return
        selection = confirmed_project_company(settings=self)
        if not selection.is_valid:
            raise ValueError(selection.reason)
