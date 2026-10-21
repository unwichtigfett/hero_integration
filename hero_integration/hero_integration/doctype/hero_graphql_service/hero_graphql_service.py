from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mutation_draft import operation_kind, validate as validate_mutation_draft


class HEROGraphQLService(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", self.schema_snapshot)
        if snapshot.analysis_status != "Validated":
            frappe.throw("A validated GraphQL schema snapshot is required.")
        if analyze_snapshot(json.loads(snapshot.snapshot_json)).fingerprint != snapshot.schema_fingerprint:
            frappe.throw("The selected GraphQL schema snapshot is stale.")
        self.schema_fingerprint = snapshot.schema_fingerprint
        contract_fields = ("query_document", "schema_snapshot", "operation_name")
        if not self.is_new() and self.has_value_changed("query_document"):
            self.service_revision = int(self.service_revision or 0) + 1
        if not self.is_new() and any(self.has_value_changed(field) for field in contract_fields):
            self.execution_status = "Pending Approval"
            self.approved_service_revision = 0
            self.approved_mapping_revision = 0
        if operation_kind(self.query_document) == "mutation":
            detail = validate_mutation_draft(json.loads(snapshot.snapshot_json), self.query_document)
            # A mutation is local-only metadata.  Never preserve or derive an
            # executable/validated state from a direct DocType save.
            self.status = "Draft"
            self.validation_summary = (
                f"Local-only mutation draft for {detail['operation']} bound to the selected schema fingerprint. "
                "HERO mutation execution is disabled."
            )
            return
        from hero_integration.client.graphql import assert_read_only

        assert_read_only(self.query_document)
        self.validation_summary = "Read-only GraphQL document bound to the selected schema fingerprint."
