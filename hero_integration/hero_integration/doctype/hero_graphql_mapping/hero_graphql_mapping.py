from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from hero_integration.services.schema_operations import catalog


class HEROGraphQLMapping(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", self.schema_snapshot)
        if snapshot.analysis_status != "Validated":
            frappe.throw("A validated schema snapshot is required.")
        try:
            mapping = json.loads(self.mapping_json)
            if not isinstance(mapping, dict):
                raise ValueError
            available = {item["operation_name"] for item in catalog(json.loads(snapshot.snapshot_json), self.operation_kind)}
        except (ValueError, TypeError):
            frappe.throw("The GraphQL mapping must contain a valid JSON object and schema operation.")
        if self.operation_name not in available:
            frappe.throw("The selected operation is not present in the selected schema snapshot.")
        self.schema_fingerprint = snapshot.schema_fingerprint
        self.source_instance = getattr(snapshot, "source_instance", "") or ""
        if not self.is_new() and self.has_value_changed("mapping_json"):
            self.mapping_revision = int(self.mapping_revision or 0) + 1
            self.status = "Draft"
