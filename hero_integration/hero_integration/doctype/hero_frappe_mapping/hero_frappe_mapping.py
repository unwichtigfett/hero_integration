from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import frappe
from frappe.model.document import Document

from hero_integration.services.direct_mapping import MappingSpec, PURPOSE_FIELD, PURPOSE_RECORD_NAME, validate_spec
from hero_integration.services.frappe_mapping_catalog import validate_draft_spec
from hero_integration.services.graphql_graph import analyze_snapshot


class HEROFrappeMapping(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", self.schema_snapshot)
        if snapshot.analysis_status != "Validated":
            frappe.throw("A validated GraphQL schema snapshot is required.")
        if analyze_snapshot(json.loads(snapshot.snapshot_json)).fingerprint != snapshot.schema_fingerprint:
            frappe.throw("The selected GraphQL schema snapshot is stale.")
        self.schema_fingerprint = snapshot.schema_fingerprint
        self.definition_id = self.definition_id or uuid4().hex
        spec = _spec(self)
        if self.status == "Draft" and self.purpose == PURPOSE_FIELD:
            target = validate_draft_spec(spec, _FrappeMetadata())
            self.validation_summary = (
                "Draft proposal only; activation requires an app-owned integration."
                if not target.activation_allowed else "Draft proposal validated against current Frappe metadata."
            )
        else:
            validate_spec(spec, _FrappeMetadata())
        if self.status == "Active" and self.purpose != PURPOSE_RECORD_NAME:
            _lock_active_target(self.operation, self.target_doctype, self.target_field)
            # A locking read sees committed state or MariaDB aborts a stale
            # REPEATABLE READ transaction; neither may accept an old snapshot.
            conflicts = frappe.db.sql(
                "SELECT name FROM `tabHERO Frappe Mapping` "
                "WHERE operation = %s AND target_doctype = %s AND target_field = %s "
                "AND status = %s AND name != %s LIMIT 1 FOR UPDATE",
                (self.operation, self.target_doctype, self.target_field, "Active", self.name or ""),
            )
            if conflicts:
                frappe.throw("An active mapping already owns this target field.")
            self.validation_summary = "Validated against current Frappe metadata."
        revision_fields = (
            "operation", "source_path", "source_type", "target_doctype", "target_field", "purpose",
            "transformation", "relation", "priority", "status",
        )
        if not self.is_new() and any(self.has_value_changed(field) for field in revision_fields):
            self.mapping_revision = int(self.mapping_revision or 0) + 1


def _lock_active_target(operation, target_doctype, target_field):
    # GET_LOCK survives transaction commits, so keep it only on the database
    # connection and release it through both transaction completion hooks.
    db = frappe.db
    key = json.dumps([frappe.local.site, operation, target_doctype, target_field],
                     ensure_ascii=False, separators=(",", ":"))
    lock_name = "hero-map:" + hashlib.sha256(key.encode()).hexdigest()[:52]
    held = getattr(db, "_hero_mapping_target_locks", None)
    if held is not None and lock_name in held:
        if db.sql("SELECT IS_USED_LOCK(%s) = CONNECTION_ID()", (lock_name,))[0][0] != 1:
            frappe.throw("Mapping target lock was lost; restart the transaction.")
        return
    if not db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, 5))[0][0]:
        frappe.throw("Mapping target is busy; retry the transaction.")
    if held is None:
        held = set()
        db._hero_mapping_target_locks = held
        def release():
            try:
                for name in sorted(held):
                    db.sql("SELECT RELEASE_LOCK(%s)", (name,))
            finally:
                held.clear()
                db._hero_mapping_target_locks = None
        try:
            db.after_commit.add(release)
            db.after_rollback.add(release)
        except Exception:
            db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
            db._hero_mapping_target_locks = None
            raise
    held.add(lock_name)


def _spec(document) -> MappingSpec:
    return MappingSpec(
        operation=document.operation,
        source_path=document.source_path,
        source_type=document.source_type,
        target_doctype=document.target_doctype,
        target_field=document.target_field,
        purpose=document.purpose,
        transformation=document.transformation,
        relation=document.relation or "",
        priority=int(document.priority if document.priority is not None else 5),
        definition_id=document.definition_id,
    )


class _FrappeMetadata:
    def ensure_write(self, doctype):
        frappe.has_permission(doctype, "write", throw=True)

    def field(self, doctype, fieldname):
        return frappe.get_meta(doctype).get_field(fieldname)

    def value_is_valid(self, doctype, fieldname, value):
        field = self.field(doctype, fieldname)
        if field.fieldtype == "Check":
            return value in (True, False, 0, 1)
        if field.fieldtype in {"Int", "Float"}:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if field.fieldtype == "Link":
            return isinstance(value, str) and bool(frappe.db.exists(field.options, value))
        return isinstance(value, str)
