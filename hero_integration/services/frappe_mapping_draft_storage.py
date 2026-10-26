"""Frappe persistence for personal drafts; the caller owns the outer transaction."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
import json
from uuid import uuid4

from hero_integration.services.mapping_draft_service import (
    ActiveMapping, DraftConflict, MappingDraft, MappingDraftError,
    MappingDraftRevision, MappingDraftService, _object, draft_key,
)


_write_scope = ContextVar("hero_mapping_draft_write_scope", default=None)


def assert_storage_write(document) -> None:
    """Reject generic document writes, even for a System Manager."""
    import frappe

    scope = _write_scope.get()
    if scope != (frappe.local.site, frappe.session.user):
        raise PermissionError("Personal mapping documents must be written through their storage service.")
    if "System Manager" not in frappe.get_roles():
        raise PermissionError("System Manager authority is required.")
    if document.doctype == "HERO Mapping Draft" and document.owner_user != frappe.session.user:
        raise PermissionError("Only the personal draft owner may write it.")
    if document.doctype == "HERO Mapping Revision" and document.actor != frappe.session.user:
        raise PermissionError("Revision actor must be the current administrator.")


class FrappeMappingDraftStorage:
    requires_generation = True
    DRAFT = "HERO Mapping Draft"
    REVISION = "HERO Mapping Revision"

    def __init__(self, frappe_module=None):
        if frappe_module is None:
            import frappe as frappe_module
        self.frappe = frappe_module
        self._definition = None

    def service(self) -> MappingDraftService:
        return MappingDraftService(self, canonicalize=self.canonicalize)

    @contextmanager
    def transaction(self, definition_id: str, actor: str):
        f = self.frappe
        self._authorize(actor)
        if self._definition is not None:
            raise MappingDraftError("Nested personal draft operations are not supported.")
        savepoint = "hero_draft_" + uuid4().hex
        f.db.savepoint(savepoint)
        token = _write_scope.set((f.local.site, f.session.user))
        try:
            # Lock the EXISTING parent. This serializes first inserts as well as
            # CAS updates, unlike a lock on a not-yet-existing personal draft.
            rows = f.db.sql(
                "select * from `tabHERO GraphQL Mapping` where definition_id=%s for update",
                (definition_id,), as_dict=True,
            )
            if len(rows) != 1:
                raise MappingDraftError("Exactly one mapping definition is required.")
            self._definition = f.get_doc(dict(rows[0], doctype="HERO GraphQL Mapping"))
            self._definition.check_permission("read")
            if self._definition.status != "Active":
                raise MappingDraftError("A current active mapping definition is required.")
            yield
        except Exception:
            f.db.rollback(save_point=savepoint)
            raise
        finally:
            self._definition = None
            _write_scope.reset(token)
        # No commit: outer request/job/test transaction retains the parent lock.

    def _authorize(self, actor):
        f = self.frappe
        if actor != f.session.user or actor in ("Guest", None):
            raise PermissionError("Draft actor must equal the authenticated session user.")
        if "System Manager" not in f.get_roles():
            raise PermissionError("System Manager authority is required.")

    def _scope(self):
        self._authorize(self.frappe.session.user)
        if self._definition is None:
            raise PermissionError("A locked draft transaction is required.")

    def active(self, definition_id):
        self._scope()
        row = self._definition
        if row.definition_id != definition_id:
            raise PermissionError("Mapping definition scope mismatch.")
        return ActiveMapping(self.frappe.local.site, definition_id, row.name,
                             int(row.mapping_revision), row.schema_fingerprint,
                             _object(json.loads(row.mapping_json)))

    def draft(self, key):
        self._scope()
        if key != draft_key(self.frappe.local.site, self._definition.definition_id, self.frappe.session.user):
            raise PermissionError("A foreign personal draft cannot be accessed.")
        rows = self.frappe.db.sql(
            "select * from `tabHERO Mapping Draft` where draft_key=%s for update", (key,), as_dict=True,
        )
        if not rows:
            return None
        row = rows[0]
        if row.owner_user != self.frappe.session.user or row.site_scope != self.frappe.local.site:
            raise PermissionError("Personal draft ownership or site mismatch.")
        return MappingDraft(row.draft_key, row.site_scope, row.definition_id, row.owner_user,
                            int(row.generation), row.state, json.loads(row.content_json), row.content_hash,
                            int(row.content_revision), row.base_active_mapping, int(row.base_active_revision),
                            row.base_active_hash, row.base_schema_fingerprint,
                            json.loads(row.base_content_json) if row.base_content_json else None,
                            int(row.reconciliation_revision or 0),
                            json.loads(row.reconciliation_json) if row.reconciliation_json else None)

    def save_draft(self, value: MappingDraft):
        self._scope()
        f = self.frappe
        expected_key = draft_key(f.local.site, self._definition.definition_id, f.session.user)
        if (value.key != expected_key or value.owner != f.session.user or value.site_scope != f.local.site
                or value.definition_id != self._definition.definition_id):
            raise PermissionError("Personal draft scope mismatch.")
        rows = f.db.sql("select * from `tabHERO Mapping Draft` where name=%s for update", (value.key,), as_dict=True)
        row = f.get_doc(dict(rows[0], doctype=self.DRAFT)) if rows else f.new_doc(self.DRAFT)
        row.update({
            "draft_key": value.key, "site_scope": value.site_scope, "definition_id": value.definition_id,
            "owner_user": value.owner, "generation": value.generation, "state": value.state,
            "content_json": json.dumps(value.content, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "content_hash": value.content_hash, "content_revision": value.content_revision,
            "base_active_mapping": value.base_active_name, "base_active_revision": value.base_active_revision,
            "base_active_hash": value.base_active_hash, "base_schema_fingerprint": value.base_schema_fingerprint,
            "base_content_json": json.dumps(value.base_content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if value.base_content is not None else "",
            "reconciliation_revision": value.reconciliation_revision,
            "reconciliation_json": json.dumps(value.reconciliation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if value.reconciliation is not None else "",
        })
        row.save()

    def append_revision(self, value: MappingDraftRevision):
        self._scope()
        if value.actor != self.frappe.session.user:
            raise PermissionError("Revision actor mismatch.")
        expected_key = draft_key(self.frappe.local.site, self._definition.definition_id, self.frappe.session.user)
        if value.draft_key != expected_key:
            raise PermissionError("A foreign personal draft revision cannot be created.")
        current = self.draft(expected_key)
        if (current is None or current.generation != value.generation
                or current.content_revision != value.revision or current.content_hash != value.content_hash):
            raise DraftConflict("Revision does not describe the current saved personal draft.")
        values = asdict(value)
        values.pop("content")
        values["content_json"] = json.dumps(value.content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        values["base_active_mapping"] = values.pop("base_active_name")
        values["doctype"] = self.REVISION
        # A deterministic primary key protects immutable generation/revision identity.
        values["name"] = f"{value.draft_key}-{value.generation}-{value.revision}"
        self.frappe.get_doc(values).insert(set_name=values["name"])

    def _rule_snapshot(self, identity):
        from hero_integration.services.personal_mapping_content import RULE_FIELDS
        f, parent = self.frappe, self._definition
        config = json.loads(parent.mapping_json)
        rows = f.get_all("HERO Frappe Mapping", filters={"definition_id": identity, "status": "Active",
            "schema_snapshot": parent.schema_snapshot, "schema_fingerprint": parent.schema_fingerprint,
            "operation": config.get("target_operation", parent.operation_name)}, fields=["name"], limit=2)
        if len(rows) != 1:
            raise MappingDraftError("Exactly one visible active field-rule template in this definition scope is required.")
        rule = f.get_doc("HERO Frappe Mapping", rows[0].name)
        rule.check_permission("read")
        return ({field: rule.get(field) for field in RULE_FIELDS},
                {"name": rule.name, "revision": int(rule.mapping_revision), "operation": rule.operation,
                 "schema_snapshot": rule.schema_snapshot, "schema_fingerprint": rule.schema_fingerprint})

    def initial_content(self, content):
        from hero_integration.services.personal_mapping_content import bounded, references
        self._scope()
        value = _object(content)
        ids = references(value)
        if not ids:
            return bounded(value)
        if "field_rules" in value or "field_rule_bases" in value:
            raise MappingDraftError("Active content already uses reserved personal snapshot fields.")
        value["field_rules"], value["field_rule_bases"] = {}, {}
        for identity in ids:
            value["field_rules"][identity], value["field_rule_bases"][identity] = self._rule_snapshot(identity)
        return bounded(value)

    def canonicalize(self, content):
        from hero_integration.services.personal_mapping_content import bounded, references, same, validate_edits, validate_rule
        self._scope()
        key = draft_key(self.frappe.local.site, self._definition.definition_id, self.frappe.session.user)
        prior = self.draft(key).content
        value = validate_edits(prior, content)
        rules, bases = value.get("field_rules", {}), value.get("field_rule_bases", {})
        if not isinstance(rules, dict) or not isinstance(bases, dict) or not same(bases, prior.get("field_rule_bases", {})):
            raise MappingDraftError("Field-rule provenance is server-managed.")
        known = prior.get("field_rules", {})
        # Retain unlinked snapshots too: Undo/relink may restore the private edit.
        if set(known) - set(rules):
            raise MappingDraftError("Existing rule snapshots must be retained.")
        for identity in references(value):
            if identity not in known:
                snapshot, provenance = self._rule_snapshot(identity)
                rules.setdefault(identity, snapshot)
                bases[identity] = provenance
        if set(rules) != set(bases):
            raise MappingDraftError("Unknown field-rule snapshot.")
        for rule in rules.values():
            validate_rule(rule)
        if rules or "field_rules" in prior:
            value["field_rules"], value["field_rule_bases"] = rules, bases
        # Personal drafts store structural content losslessly. Executable
        # writeback normalization remains solely at its existing execution gate.
        return bounded(value)
