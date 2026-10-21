from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from hero_integration.services.graphql_contract import GraphQLContractError, parse_mutation


class HEROMutationContract(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        service = frappe.get_doc("HERO GraphQL Service", self.graphql_service)
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", service.schema_snapshot)
        if snapshot.analysis_status != "Validated" or snapshot.execution_contract_status == "Blocked":
            frappe.throw("A compatible validated schema snapshot is required for a mutation contract.")
        try:
            mapping = json.loads(self.mapping_json)
            if not isinstance(mapping, dict):
                raise ValueError("mapping must be an object")
            parsed = parse_mutation(service.query_document)
        except (ValueError, GraphQLContractError) as error:
            frappe.throw(f"Invalid HERO mutation contract: {error}")
        self.schema_snapshot = snapshot.name
        self.schema_fingerprint = snapshot.schema_fingerprint
        self.operation_name = parsed.root_field
        self.contract_fingerprint = parsed.fingerprint
        if not self.is_new() and self.has_value_changed("mapping_json"):
            self.contract_revision = int(self.contract_revision or 0) + 1
            self.status = "Draft"
        self.validation_summary = "Schema-bound mutation contract; explicit execution approval remains required."
