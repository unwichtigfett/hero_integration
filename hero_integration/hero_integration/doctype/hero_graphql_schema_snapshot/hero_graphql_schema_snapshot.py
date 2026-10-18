from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from hero_integration.services.graphql_graph import analyze_snapshot


class HEROGraphqlSchemaSnapshot(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        try:
            graph = analyze_snapshot(json.loads(self.snapshot_json))
        except (TypeError, ValueError) as error:
            self.analysis_status = "Rejected"
            self.analysis_detail = str(error)
            raise
        self.schema_fingerprint = graph.fingerprint
        self.analysis_status = "Validated"
        self.analysis_detail = f"{len(graph.nodes)} graph nodes and {len(graph.edges)} graph edges."
