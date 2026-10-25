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
                            row.base_active_hash, row.base_schema_fingerprint)

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

    def canonicalize(self, content):
        self._scope()
        if self._definition.direction != "FRAPPE_TO_MUTATION":
            return content
        from hero_integration.services.frappe_writeback import (
            WritebackContractError, normalise_draft_mapping, normalise_mapping,
        )
        snapshot = self.frappe.get_doc("HERO GraphQL Schema Snapshot", self._definition.schema_snapshot)
        schema = json.loads(snapshot.snapshot_json)
        for normalize in (normalise_mapping, normalise_draft_mapping):
            try:
                return normalize(content, schema)
            except WritebackContractError:
                continue
        # Structural storage is deliberately weaker than activation validation.
        # Incomplete or invalid business choices remain non-executable drafts.
        return content
