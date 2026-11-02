from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

import frappe
from frappe.model.document import Document

from hero_integration.services.direct_mapping import MappingSpec, PURPOSE_FIELD, PURPOSE_RECORD_NAME, validate_spec
from hero_integration.services.frappe_mapping_catalog import FrappePickerMetadata, validate_draft_spec
from hero_integration.services.graphql_graph import analyze_snapshot


class HEROFrappeMapping(Document):
    def on_trash(self):
        if self.personal_graphql_mapping:
            frappe.throw('Published rule versions must be retained.', frappe.PermissionError)
        # GraphQL mapping JSON and personal Draft snapshots reference rule IDs
        # without Frappe Link fields. Until those writers share a deletion
        # serialization boundary, normal Link checks cannot prove safety.
        frappe.throw('Field-rule deletion is unavailable until all mapping references are reconciled.', frappe.PermissionError)

    def before_rename(self, *_args, **_kwargs):
        if self.personal_graphql_mapping:
            frappe.throw('Published rule versions cannot be renamed.', frappe.PermissionError)

    def validate(self) -> None:
        frappe.only_for("System Manager")
        from hero_integration.services.frappe_mapping_draft_storage import personal_publication_parent, first_publication_admits
        scope = getattr(self, 'personal_graphql_mapping', None) or ''
        previous = self.get_doc_before_save() if not self.is_new() else None
        old_scope = (previous.personal_graphql_mapping or '') if previous else ''
        if previous and self.has_value_changed('mapping_revision'):
            frappe.throw('Field-rule revision is managed by the mapping controller.')
        active_fields = ('status', 'operation', 'schema_snapshot', 'source_path', 'source_type', 'target_doctype',
                         'target_field', 'purpose', 'transformation', 'relation', 'priority',
                         'schema_dependencies', 'schema_manifest', 'definition_id', 'owner')
        if (previous and previous.status == 'Draft' and previous.owner != frappe.session.user
                and any(self.has_value_changed(field) for field in active_fields)):
            frappe.throw('Only the owner may change a shared field-rule Draft.', frappe.PermissionError)
        if previous and previous.status == 'Active' and any(self.has_value_changed(field) for field in active_fields):
            frappe.throw('Active field rules cannot be changed directly; use a personal draft and publication.')
        if scope != old_scope and (not self.is_new() or not scope or personal_publication_parent() != scope):
            frappe.throw('Published mapping scope is immutable and service-managed.', frappe.PermissionError)
        if scope and personal_publication_parent() != scope:
            frappe.throw('Published rule versions are immutable.', frappe.PermissionError)
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", self.schema_snapshot)
        if snapshot.analysis_status != "Validated":
            frappe.throw("A validated GraphQL schema snapshot is required.")
        if analyze_snapshot(json.loads(snapshot.snapshot_json)).fingerprint != snapshot.schema_fingerprint:
            frappe.throw("The selected GraphQL schema snapshot is stale.")
        self.schema_fingerprint = snapshot.schema_fingerprint
        self.definition_id = self.definition_id or uuid4().hex
        if previous and previous.definition_id and self.definition_id != previous.definition_id:
            frappe.throw('Field-rule definition identity is immutable.')
        portable = self.get('portable_definition_id') or ''
        lineage = self.get('portable_lineage_json') or ''
        if previous:
            if portable != (previous.get('portable_definition_id') or '') or lineage != (previous.get('portable_lineage_json') or ''):
                frappe.throw('Portable rule identity and lineage are immutable.')
        elif portable and not lineage and portable == self.definition_id and scope and first_publication_admits('rule', portable):
            pass  # A first publication creates a root without inventing a predecessor.
        elif portable or lineage:
            if not scope or personal_publication_parent() != scope or not portable or not lineage:
                frappe.throw('Portable rule identity and lineage are service-managed.')
            from hero_integration.services.mapping_rule_lineage import next_lineage, parse_lineage
            from hero_integration.services.personal_mapping_content import RULE_FIELDS
            try:
                declared = parse_lineage(lineage)
                rows = frappe.get_all('HERO Frappe Mapping', filters={'portable_definition_id':declared['predecessor_id']},
                                      fields=['name'], limit=2)
                if len(rows) != 1 or portable != self.definition_id:
                    raise ValueError
                original = frappe.get_doc('HERO Frappe Mapping', rows[0].name, for_update=True)
                original.check_permission('read')
                if (original.status != 'Active' or original.schema_snapshot != self.schema_snapshot
                        or original.schema_fingerprint != self.schema_fingerprint
                        or original.operation != self.operation
                        or original.schema_manifest != self.schema_manifest
                        or original.schema_dependencies != self.schema_dependencies
                        or (original.personal_graphql_mapping and original.personal_graphql_mapping != scope)):
                    raise ValueError
                expected = next_lineage(original, portable, {field:self.get(field) for field in RULE_FIELDS})
                if json.loads(expected) != declared:
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                frappe.throw('Portable rule lineage does not match the authorized preceding revision.')
        if self.is_new():
            if self.status == 'Active' and self.purpose != PURPOSE_RECORD_NAME:
                _lock_active_target((f'personal:{scope}:' if scope else '') + self.operation,
                                    self.target_doctype, self.target_field)
            try:
                portable_candidate = str(UUID(self.definition_id))
            except (ValueError, TypeError, AttributeError):
                portable_candidate = ''
            if portable_candidate:
                _lock_active_target('portable-field-rule', frappe.local.site, portable_candidate)
                if frappe.db.sql('select name from `tabHERO Frappe Mapping` where portable_definition_id=%s '
                                 'or definition_id in (%s,%s) for update',
                                 (portable_candidate, portable_candidate, UUID(portable_candidate).hex)):
                    frappe.throw('Field-rule identity is already enrolled by another mapping.')
                from hero_integration.services.frappe_mapping_draft_storage import lock_identity_drafts
                for row in lock_identity_drafts(frappe, [portable_candidate], 'rule'):
                    if not ((scope and personal_publication_parent() == scope and row.owner_user == frappe.session.user
                             and row.base_active_mapping == scope) or first_publication_admits('rule', portable_candidate, row)):
                        frappe.throw('Field-rule identity is already used by a personal draft.')
        spec = _spec(self)
        if self.status == "Draft" and self.purpose == PURPOSE_FIELD:
            target = validate_draft_spec(spec, FrappePickerMetadata())
            self.validation_summary = (
                "Draft proposal only; activation requires an app-owned integration."
                if not target.activation_allowed else "Draft proposal validated against current Frappe metadata."
            )
        else:
            validate_spec(spec, _FrappeMetadata())
        if self.status == "Active" and self.purpose != PURPOSE_RECORD_NAME:
            lock_scope = (f'personal:{scope}:' if scope else '') + self.operation
            same_active_target = bool(previous and previous.status == 'Active'
                and previous.operation == self.operation and previous.target_doctype == self.target_doctype
                and previous.target_field == self.target_field and previous.purpose == self.purpose
                and old_scope == scope)
            if previous and not same_active_target:
                _require_active_target(lock_scope, self.target_doctype, self.target_field)
            elif not previous:
                _lock_active_target(lock_scope, self.target_doctype, self.target_field)
            # A locking read sees committed state or MariaDB aborts a stale
            # REPEATABLE READ transaction; neither may accept an old snapshot.
            if not scope and not same_active_target:
                conflicts = frappe.db.sql(
                    "SELECT name FROM `tabHERO Frappe Mapping` "
                    "WHERE operation = %s AND target_doctype = %s AND target_field = %s "
                    "AND status = %s AND name != %s "
                    "AND COALESCE(personal_graphql_mapping, '') = %s LIMIT 1 FOR UPDATE",
                    (self.operation, self.target_doctype, self.target_field, "Active", self.name or "", scope),
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


def _target_lock_name(operation, target_doctype, target_field):
    key = json.dumps([frappe.local.site, operation, target_doctype, target_field],
                     ensure_ascii=False, separators=(",", ":"))
    return "hero-map:" + hashlib.sha256(key.encode()).hexdigest()[:52]


def _require_active_target(operation, target_doctype, target_field):
    name = _target_lock_name(operation, target_doctype, target_field)
    if name not in (getattr(frappe.db, '_hero_mapping_target_locks', None) or set()):
        frappe.throw('Mapping target must be locked before activating or retargeting an existing rule.')
    _lock_active_target(operation, target_doctype, target_field)


def _lock_active_target(operation, target_doctype, target_field):
    # GET_LOCK survives transaction commits, so keep it only on the database
    # connection and release it through both transaction completion hooks.
    db = frappe.db
    lock_name = _target_lock_name(operation, target_doctype, target_field)
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
        from hero_integration.services.doctype_permissions import ensure_doctype_write
        ensure_doctype_write(doctype, frappe)

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
