from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from hero_integration.client.graphql import HeroClientError, validate_endpoint
from hero_integration.client.settings import BINDING_FIELDS, next_source_binding
from hero_integration.services.schema_refresh import INTERVAL_MAX, INTERVAL_MIN, refresh_interval


class HEROIntegrationSettings(Document):
    def validate(self):
        frappe.only_for("System Manager")
        self._update_source_binding()
        if self.hero_api_url:
            hosts = {line.strip().lower() for line in (self.allowed_hero_hosts or "").splitlines() if line.strip()}
            try:
                validate_endpoint(self.hero_api_url, hosts)
            except HeroClientError as exc:
                raise ValueError(str(exc)) from exc
        if not INTERVAL_MIN <= refresh_interval(self) <= INTERVAL_MAX:
            raise ValueError(f"Schema refresh interval must be between {INTERVAL_MIN} and {INTERVAL_MAX} minutes.")

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
