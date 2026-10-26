from __future__ import annotations

import hashlib
import json

import frappe
from frappe.model.document import Document

from hero_integration.services.frappe_mapping_catalog import FrappePickerMetadata, validate_draft_writeback_source
from hero_integration.services.frappe_writeback import (
    WritebackContractError, draft_source_type_compatible, normalise_draft_mapping, normalise_mapping,
)
from hero_integration.services.schema_operations import catalog


class HEROGraphQLMapping(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        from hero_integration.services.mapping_draft_access import protect_managed_mapping
        protect_managed_mapping(self)
        previous = self.get_doc_before_save() if not self.is_new() else None
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", self.schema_snapshot)
        if snapshot.analysis_status != "Validated":
            frappe.throw("A validated schema snapshot is required.")
        try:
            mapping = json.loads(self.mapping_json)
            if not isinstance(mapping, dict):
                raise ValueError
            if self.direction == "FRAPPE_TO_MUTATION":
                schema = json.loads(snapshot.snapshot_json)
                if self.status == "Active":
                    mapping = normalise_mapping(mapping, schema)
                else:
                    try:
                        mapping = normalise_mapping(mapping, schema)
                    except WritebackContractError:
                        mapping = normalise_draft_mapping(mapping, schema)
                        source_field = validate_draft_writeback_source(
                            mapping["source"]["doctype"], mapping["source"]["field"], FrappePickerMetadata(),
                        )
                        if not draft_source_type_compatible(source_field.fieldtype, mapping["target"]["graphql_type"]):
                            frappe.throw("The selected Frappe source and HERO scalar or enum input types are incompatible.")
                source = mapping["source"]
                if not frappe.get_meta(source["doctype"]).get_field(source["field"]):
                    frappe.throw("The configured Frappe writeback field no longer exists.")
                if self.operation_kind != "MUTATION" or self.operation_name != mapping["target"]["operation"]:
                    frappe.throw("Writeback direction, operation kind and reviewed HERO target must agree.")
                self.mapping_json = json.dumps(mapping, ensure_ascii=False, sort_keys=True)
            available = {item["operation_name"] for item in catalog(json.loads(snapshot.snapshot_json), self.operation_kind)}
        except (ValueError, TypeError, WritebackContractError):
            frappe.throw("The GraphQL mapping must contain a valid JSON object and schema operation.")
        if self.operation_name not in available:
            frappe.throw("The selected operation is not present in the selected schema snapshot.")
        self.schema_fingerprint = snapshot.schema_fingerprint
        self.source_instance = getattr(snapshot, "source_instance", "") or ""
        if self.direction == "FRAPPE_TO_MUTATION" and self.status == "Active":
            contracts = frappe.get_all(
                "HERO Mutation Contract",
                filters={"schema_snapshot": snapshot.name, "operation_name": self.operation_name, "status": "Active"},
                pluck="name",
            )
            if len(contracts) != 1:
                frappe.throw("Exactly one active schema-bound HERO Mutation Contract is required for writeback activation.")
            source = mapping["source"]
            conflicts = frappe.get_all(
                "HERO GraphQL Mapping",
                filters={
                    "direction": "FRAPPE_TO_MUTATION", "status": "Active",
                    "schema_snapshot": snapshot.name, "name": ["!=", self.name or ""],
                },
                fields=["name", "mapping_json"],
            )
            for other in conflicts:
                try:
                    other_source = json.loads(other.mapping_json).get("source", {})
                except (TypeError, ValueError):
                    continue
                if other_source == source:
                    frappe.throw("An active writeback mapping already owns this Frappe field.")
        if not self.definition_id:
            self.definition_id = self.name or ""
        if previous and previous.definition_id and self.definition_id != previous.definition_id:
            frappe.throw("GraphQL mapping definition identity is immutable.")
        self.content_hash = hashlib.sha256(
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        semantic_fields = ("schema_snapshot", "operation_kind", "operation_name", "direction", "mapping_json")
        content_changed = bool(previous and any(self.has_value_changed(field) for field in semantic_fields))
        writeback_scope = bool(previous and (previous.direction == "FRAPPE_TO_MUTATION" or self.direction == "FRAPPE_TO_MUTATION"))
        review_changed = bool(writeback_scope and any(self.has_value_changed(field) for field in ("suggestion_reason", "confidence")))
        status_changed = bool(writeback_scope and previous.status != self.status)
        draft_editor_changed = bool(writeback_scope and (previous.status == "Draft" or self.status == "Draft")
                                    and previous.modified_by != self.modified_by)
        if content_changed or review_changed or status_changed or draft_editor_changed:
            self.mapping_revision = int(previous.mapping_revision or 0) + 1
        if content_changed:
            self.status = "Draft"
        if self.status == "Active" and previous and previous.status == "Draft":
            if previous.modified_by != frappe.session.user:
                frappe.throw("Only the administrator who last changed this Draft may activate it.")
