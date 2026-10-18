from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from hero_integration.client.graphql import assert_read_only
from hero_integration.services.graphql_graph import analyze_snapshot


class HEROGraphqlService(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        assert_read_only(self.query_document)
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", self.schema_snapshot)
        if snapshot.analysis_status != "Validated":
            frappe.throw("A validated GraphQL schema snapshot is required.")
        if analyze_snapshot(json.loads(snapshot.snapshot_json)).fingerprint != snapshot.schema_fingerprint:
            frappe.throw("The selected GraphQL schema snapshot is stale.")
        self.schema_fingerprint = snapshot.schema_fingerprint
        if not self.is_new() and self.has_value_changed("query_document"):
            self.service_revision = int(self.service_revision or 0) + 1
        self.validation_summary = "Read-only GraphQL document bound to the selected schema fingerprint."
