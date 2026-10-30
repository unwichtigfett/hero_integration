from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from hero_integration.client.graphql import HeroClientError, validate_endpoint
from hero_integration.client.settings import BINDING_FIELDS, next_source_binding
from hero_integration.integrations.erpnext.company import confirmed_project_company


class HEROIntegrationSettings(Document):
    def validate(self):
        frappe.only_for("System Manager")
        if self.has_value_changed("erpnext_company"):
            self.erpnext_company_confirmed = 0
        self._update_source_binding()
        if self.hero_api_url:
            hosts = {line.strip().lower() for line in (self.allowed_hero_hosts or "").splitlines() if line.strip()}
            try:
                validate_endpoint(self.hero_api_url, hosts)
            except HeroClientError as exc:
                raise ValueError(str(exc)) from exc
        self._validate_company_selection()

    def _update_source_binding(self):
        confirm = bool(self.get("confirm_source_binding"))
        revision, confirmed = next_source_binding(
            self.get("source_binding_revision"), self.get("source_binding_confirmed_revision"),
            binding_changed=any(self.has_value_changed(field) for field in BINDING_FIELDS), confirm=confirm,
        )
        self.source_binding_revision, self.source_binding_confirmed_revision = revision, confirmed
        if confirm:
            self.source_binding_confirmed_by = frappe.session.user
            self.source_binding_confirmed_on = now_datetime()
        self.confirm_source_binding = 0

    def _validate_company_selection(self):
        if not self.erpnext_company_confirmed:
            return
        selection = confirmed_project_company(settings=self)
        if not selection.is_valid:
            raise ValueError(selection.reason)
