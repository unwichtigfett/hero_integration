from __future__ import annotations

from frappe.model.document import Document

from hero_integration.client.graphql import HeroClientError, validate_endpoint
from hero_integration.integrations.erpnext.company import confirmed_project_company


class HEROIntegrationSettings(Document):
    def validate(self):
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
