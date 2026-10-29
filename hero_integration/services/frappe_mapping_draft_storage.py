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
_publication_scope = ContextVar("hero_mapping_publication_scope", default=None)
_publication_operation = ContextVar("hero_mapping_publication_operation", default=None)


def is_personal_publication(document) -> bool:
    import frappe
    return _publication_scope.get() == (frappe.local.site, frappe.session.user, document.name)


def personal_publication_active() -> bool:
    import frappe
    scope = _publication_operation.get()
    return bool(scope and scope[:2] == (frappe.local.site, frappe.session.user))


def personal_publication_parent():
    return _publication_operation.get()[2] if personal_publication_active() else None


@contextmanager
def _publication_operation_scope(site, actor, parent):
    token = _publication_operation.set((site, actor, parent))
    try:
        yield
    finally:
        _publication_operation.reset(token)


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

    def materialize_native_task(self, definition_id, actor, generation, revision, active_revision, reconciliation_revision,
                                *, snapshot_name):
        """Compile one saved personal Task draft without publishing or authorizing it."""
        from hero_integration.services.mapping_contract_v1 import MappingContractError, QueryAdapter, SnapshotAdapter, StoredSnapshot
        from hero_integration.services.mapping_draft_service import _hash
        from hero_integration.services.personal_mapping_content import references
        from hero_integration.services.direct_mapping import MappingSpec, validate_spec
        from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _FrappeMetadata
        from hero_integration.api import _validated_snapshot

        with self.transaction(definition_id, actor):
            parent, f = self._definition, self.frappe
            active = self.active(definition_id)
            draft = self.draft(draft_key(f.local.site, definition_id, actor))
            if draft is None or draft.state != 'OPEN' or draft.owner != actor:
                raise DraftConflict('The personal draft is not open.')
            if (type(generation) is not int or draft.generation != generation or type(revision) is not int
                    or revision < 1 or type(reconciliation_revision) is not int
                    or draft.reconciliation_revision != reconciliation_revision):
                raise DraftConflict('The personal draft revision is stale or unsaved.')
            self.service()._assert_current(active, draft, revision, active_revision)
            f.get_doc(self.DRAFT, draft.key, for_update=True).check_permission('read')
            if (parent.schema_snapshot != snapshot_name or parent.direction != 'QUERY_TO_FRAPPE'
                    or parent.operation_kind != 'QUERY' or parent.operation_name != 'tasks'):
                raise MappingDraftError('Only the explicit Task query mapping can be materialized here.')
            if _hash(draft.content) != draft.content_hash or self.canonicalize(draft.content) != draft.content:
                raise MappingDraftError('Saved personal content is not intact and canonical.')
            saved = f.db.sql(
                'select content_hash from `tabHERO Mapping Revision` where draft_key=%s and generation=%s '
                'and revision=%s and kind=%s for update', (draft.key, generation, revision, 'DRAFT_SAVE'), as_dict=True)
            if len(saved) != 1 or saved[0].content_hash != draft.content_hash:
                raise DraftConflict('Saved draft history does not match the requested revision.')
            self._assert_locked_rule_baseline(draft)
            rules = draft.content.get('field_rules', {})
            metadata = _FrappeMetadata()
            for identity in references(draft.content):
                try:
                    validate_spec(MappingSpec(operation='HeroTasks', definition_id=identity, **rules[identity]), metadata)
                except (KeyError, TypeError, ValueError) as error:
                    raise MappingDraftError('Native Task rule or target metadata is invalid.') from error
            snapshot, schema = _validated_snapshot(parent.schema_snapshot, for_update=True)
            if snapshot.schema_fingerprint != active.schema_fingerprint or snapshot.source_instance != parent.source_instance:
                raise DraftConflict('The Task schema or source context changed.')
            try:
                validated = SnapshotAdapter().adapt(StoredSnapshot(snapshot.name, schema, snapshot.schema_fingerprint,
                                                                   snapshot.source_instance, snapshot.analysis_status))
                return QueryAdapter().materialize_personal_task(validated, draft.content)
            except MappingContractError as error:
                raise MappingDraftError('The saved Task draft cannot be materialized from this schema.') from error

    def publish(self, definition_id, actor, generation, revision, active_revision, reconciliation_revision,
                *, confirmed=False, warning_fingerprint=''):
        """Publish exactly one saved personal revision under the parent lock."""
        from hero_integration.services.mapping_draft_service import _hash
        if confirmed is not True:
            raise MappingDraftError('Explicit publication confirmation is required.')
        self._authorize(actor)
        # Use the same lock order as directional exchange: context before
        # parent. The initial lookup identifies only the lock to acquire; the
        # current, locked parent is checked again below before publication.
        rows = self.frappe.db.sql(
            'select source_instance, schema_snapshot, direction from `tabHERO GraphQL Mapping` '
            'where definition_id=%s', (definition_id,), as_dict=True)
        if len(rows) != 1:
            raise MappingDraftError('Exactly one mapping definition is required.')
        scope = (rows[0].source_instance or '', rows[0].schema_snapshot, rows[0].direction)
        from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
        lock_exchange_context(*scope)
        with self.transaction(definition_id, actor), _publication_operation_scope(self.frappe.local.site, actor, self._definition.name):
            f = self.frappe
            if (self._definition.source_instance or '', self._definition.schema_snapshot, self._definition.direction) != scope:
                raise DraftConflict('Mapping context changed; reload before publication.')
            active = self.active(definition_id)
            key = draft_key(f.local.site, definition_id, actor)
            draft = self.draft(key)
            if draft is None or draft.state != 'OPEN' or draft.owner != actor:
                raise DraftConflict('The personal draft is not open.')
            if (type(generation) is not int or generation != draft.generation or type(revision) is not int
                    or revision != draft.content_revision or type(reconciliation_revision) is not int
                    or reconciliation_revision != draft.reconciliation_revision):
                raise DraftConflict('The personal draft revision is stale.')
            row = f.get_doc(self.DRAFT, key, for_update=True)
            row.check_permission('write')
            self._definition.check_permission('write')
            if row.modified_by != actor:
                raise PermissionError('Only the last author may publish this draft.')
            try:
                diagnostics = json.loads(row.diagnostics_json) if row.diagnostics_json else {}
            except (ValueError, TypeError) as error:
                raise MappingDraftError('Draft diagnostics are invalid.') from error
            if not isinstance(diagnostics, dict):
                raise MappingDraftError('Draft diagnostics are invalid.')
            if row.warning_fingerprint:
                warnings = diagnostics.get('warnings')
                if (warning_fingerprint != row.warning_fingerprint or not isinstance(warnings, list) or not warnings
                        or len(warnings) > 100 or any(not isinstance(w, str) or not w or len(w) > 1000 for w in warnings)):
                    raise MappingDraftError('Current warnings must be available and acknowledged.')
            receipt = diagnostics.get('personal_publication')
            if isinstance(receipt, dict) and (receipt.get('generation'), receipt.get('revision'),
                    receipt.get('draft_hash'), receipt.get('expected_active_revision')) == (
                    generation, revision, draft.content_hash, active_revision):
                if (receipt.get('active_revision') == active.revision and receipt.get('active_hash') == active.content_hash
                        and self._publication_receipt_current(receipt)):
                    return {'name': active.name, 'revision': active.revision, 'unchanged': True}
                raise DraftConflict('The published mapping changed after this publication.')
            self.service()._assert_current(active, draft, revision, active_revision)
            if revision < 1 or _hash(draft.content) != draft.content_hash:
                raise MappingDraftError('A saved, intact draft revision is required.')
            saved = f.db.sql(
                'select content_hash from `tabHERO Mapping Revision` where draft_key=%s and generation=%s '
                'and revision=%s and kind=%s for update', (key, generation, revision, 'DRAFT_SAVE'), as_dict=True)
            if len(saved) != 1 or saved[0].content_hash != draft.content_hash:
                raise DraftConflict('Saved draft history does not match the requested revision.')
            from hero_integration.api import _validated_snapshot
            snapshot, schema = _validated_snapshot(self._definition.schema_snapshot, for_update=True)
            if snapshot.schema_fingerprint != active.schema_fingerprint:
                raise DraftConflict('The schema snapshot changed.')
            self._publication_snapshot = (snapshot, schema)
            try:
                if self.canonicalize(draft.content) != draft.content:
                    raise MappingDraftError('Saved personal content is not canonical.')
            finally:
                self._publication_snapshot = None
            content, created = self._publication_content(draft, schema)
            parent = self._definition
            parent.mapping_json = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            parent.mapping_revision = active.revision + 1
            parent.status = 'Active'
            token = _publication_scope.set((f.local.site, actor, parent.name))
            try:
                parent.save()
            finally:
                _publication_scope.reset(token)
            if parent.status != 'Active' or int(parent.mapping_revision) != active.revision + 1:
                raise MappingDraftError('Publication did not produce the expected active revision.')
            current = self.active(definition_id)
            self.append_revision(MappingDraftRevision(key, generation, revision, 'ACTIVE_ACTIVATION', actor,
                draft.content, draft.content_hash, active.name, active.revision, active.content_hash, active.schema_fingerprint))
            receipt = {'generation': generation, 'revision': revision, 'draft_hash': draft.content_hash,
                'expected_active_revision': active_revision, 'active_revision': current.revision,
                'active_hash': current.content_hash, 'objects': self._publication_objects(created, content)}
            diagnostics['personal_publication'] = receipt
            row.diagnostics_json = json.dumps(diagnostics, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            row.save()
            return {'name': current.name, 'revision': current.revision, 'unchanged': False}

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
        except Exception as error:
            try:
                f.db.rollback(save_point=savepoint)
            except Exception as rollback_error:
                raise RuntimeError(
                    f'Personal draft savepoint rollback failed ({type(rollback_error).__name__}); abort the transaction.'
                ) from error
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
        if value.kind not in {"DRAFT_SAVE", "ACTIVE_ACTIVATION"}:
            raise MappingDraftError("Unsupported personal mapping revision kind.")
        # Frappe retries primary-key collisions on hash-autonamed DocTypes with
        # a random name, even when insert(set_name=...) specified an identity.
        # The parent definition and this personal draft are already locked.
        if self.frappe.db.sql(
            "select name from `tabHERO Mapping Revision` "
            "where draft_key=%s and generation=%s and revision=%s and kind=%s limit 1 for update",
            (value.draft_key, value.generation, value.revision, value.kind),
        ):
            raise DraftConflict("This personal mapping revision is already recorded.")
        values = asdict(value)
        values.pop("content")
        values["content_json"] = json.dumps(value.content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        values["base_active_mapping"] = values.pop("base_active_name")
        values["doctype"] = self.REVISION
        # Preserve existing draft revision names; activation has its own kind.
        suffix = "-activation" if value.kind == "ACTIVE_ACTIVATION" else ""
        values["name"] = f"{value.draft_key}-{value.generation}-{value.revision}{suffix}"
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
        if rule.personal_graphql_mapping and rule.personal_graphql_mapping != parent.name:
            raise MappingDraftError("The field-rule version belongs to another mapping.")
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
        scope = value.get('native_task_onboarding')
        if scope is not None and (self._definition.direction != 'QUERY_TO_FRAPPE'
                or self._definition.operation_name != 'tasks'
                or scope['source_instance'] != self._definition.source_instance):
            raise MappingDraftError('Native Task onboarding is outside the current source and mapping.')
        if value.get('additional_mappings'):
            self._validate_additional_mappings(value['additional_mappings'])
        if value.get('field_contexts') or (isinstance(value.get('source'), dict) and
                ({'object_type', 'schema_context'} & set(value['source']))):
            self._validate_object_contexts(value)
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

    def _validate_additional_mappings(self, mappings):
        from hero_integration.api import _validated_snapshot
        from hero_integration.services.frappe_mapping_catalog import (
            FrappePickerMetadata, fields as target_catalog_fields, validate_draft_target, validate_draft_writeback_source,
        )
        from hero_integration.services.frappe_writeback import (
            WritebackContractError, draft_source_type_compatible, normalise_draft_mapping,
        )
        from hero_integration.services.mapping_suggestions import source_fields

        parent = self._definition
        snapshot, schema = getattr(self, '_publication_snapshot', None) or _validated_snapshot(parent.schema_snapshot)
        if snapshot.schema_fingerprint != parent.schema_fingerprint:
            raise DraftConflict('Mapping schema changed.')
        metadata = FrappePickerMetadata()
        existing = json.loads(parent.mapping_json)
        operation = existing.get('target_operation') or parent.operation_name
        sources = {item.path: item for item in source_fields(operation, schema) if item.selectable} if parent.direction == 'QUERY_TO_FRAPPE' else {}
        for item in mappings:
            source, target = item['source'], item['target']
            try:
                if parent.direction == 'QUERY_TO_FRAPPE':
                    if set(target) != {'doctype', 'field'}:
                        raise MappingDraftError('Invalid query assignment shape.')
                    if set(source) == {'field', 'object_type', 'schema_context'}:
                        from hero_integration.services.object_mapping_catalog import field as schema_field
                        schema_field(schema, source['object_type'], source['field'], source['schema_context'])
                        permitted = next((candidate for candidate in target_catalog_fields(
                            metadata, doctype=target['doctype']) if candidate.path == target['field']), None)
                        if permitted is None or not permitted.selectable:
                            raise MappingDraftError('The Frappe target is unavailable or protected.')
                        continue
                    if set(source) != {'field'}:
                        raise MappingDraftError('Invalid query assignment shape.')
                    chosen = sources.get(source['field'])
                    if chosen is None or not chosen.mapping_type:
                        raise MappingDraftError('Source field is not approved for this query.')
                    validate_draft_target(operation, chosen.mapping_type, target.get('doctype', ''), target['field'], metadata)
                elif parent.direction == 'FRAPPE_TO_MUTATION':
                    if set(source) != {'doctype', 'field'} or set(target) != {'operation', 'argument', 'input_type', 'field', 'graphql_type'}:
                        raise MappingDraftError('Invalid mutation assignment shape.')
                    normalized = normalise_draft_mapping(item, schema)
                    actual = normalized['target']
                    if actual['operation'] != parent.operation_name or any(actual.get(k) != v for k, v in target.items()):
                        raise MappingDraftError('Mutation target does not match the snapshot.')
                    field = validate_draft_writeback_source(source.get('doctype', ''), source['field'], metadata)
                    if not draft_source_type_compatible(field.fieldtype, actual['graphql_type']):
                        raise MappingDraftError('Source and mutation field types are incompatible.')
                else:
                    raise MappingDraftError('Unsupported mapping direction.')
            except (ValueError, WritebackContractError) as error:
                raise MappingDraftError('Invalid additional mapping.') from error

    def _validate_object_contexts(self, content):
        from hero_integration.api import _validated_snapshot
        from hero_integration.services.object_mapping_catalog import field as schema_field
        if self._definition.direction != 'QUERY_TO_FRAPPE':
            raise MappingDraftError('Object field contexts require a Query mapping.')
        snapshot, schema = getattr(self, '_publication_snapshot', None) or _validated_snapshot(self._definition.schema_snapshot)
        if snapshot.schema_fingerprint != self._definition.schema_fingerprint:
            raise DraftConflict('Mapping schema changed.')
        contexts, rules = content.get('field_contexts', {}), content.get('field_rules', {})
        primary = content.get('source', {})
        if isinstance(primary, dict) and ({'object_type', 'schema_context'} & set(primary)):
            allowed = {'field', 'object_type', 'schema_context', 'doctype', 'type', 'path', 'source_path'}
            if (not {'field', 'object_type', 'schema_context'} <= set(primary)
                    or set(primary) - allowed
                    or any(primary[key] != primary['field'] for key in ('path', 'source_path') if key in primary)):
                raise MappingDraftError('Invalid primary object field context.')
            try:
                schema_field(schema, primary['object_type'], primary['field'], primary['schema_context'])
            except (KeyError, TypeError, ValueError) as error:
                raise MappingDraftError('Object field context does not match the saved schema.') from error
        for identity, context in contexts.items():
            if identity not in rules or identity not in content.get('mapping_definition_ids', []):
                raise MappingDraftError('Object context must identify a selected personal field rule.')
            try:
                schema_field(schema, context['object_type'], rules[identity]['source_path'], context['schema_context'])
            except (KeyError, TypeError, ValueError) as error:
                raise MappingDraftError('Object field context does not match the saved schema.') from error

    def _approved_relationship_contexts(self, content, schema):
        """Recognize only the existing Project-to-Customer identity resolver.

        This does not approve a service. Publication still checks its existing
        read contract, rule provenance, permissions and mapping plan.
        """
        from hero_integration.services.mapping_suggestions import source_fields
        from hero_integration.services.object_mapping_catalog import field as schema_field
        if content.get('target_operation') != 'HeroProjectMatches':
            return False
        source = next((item for item in source_fields('HeroProjectMatches', schema)
                       if item.path == 'customer_id' and item.selectable), None)
        contexts, rules = content.get('field_contexts') or {}, content.get('field_rules') or {}
        if not source or not isinstance(contexts, dict) or not contexts or not isinstance(rules, dict):
            return False
        for identity, context in contexts.items():
            rule = rules.get(identity, {})
            if (identity not in content.get('mapping_definition_ids', []) or not isinstance(context, dict)
                    or not isinstance(rule, dict)
                    or (rule.get('source_path'), rule.get('source_type'), rule.get('target_doctype'),
                 rule.get('target_field'), rule.get('purpose'), rule.get('transformation'), rule.get('relation')) !=
                    ('customer_id', 'int|null', 'Project', 'customer', 'Customer Relation',
                     'HERO Customer Link', 'HERO Customer Link') or context.get('object_type') != source.return_type):
                return False
            try:
                chosen = schema_field(schema, context['object_type'], 'customer_id', context['schema_context'])
            except (KeyError, TypeError, ValueError):
                return False
            if chosen['relations'] or chosen['arguments']:
                return False
        return True

    def _approved_task_contexts(self, content, schema):
        """Allow only flat schema contexts for the existing native Task rules."""
        from hero_integration.services.direct_mapping import MappingSpec, validate_native_task_rule
        from hero_integration.services.object_mapping_catalog import field as schema_field
        from hero_integration.services.graphql_graph import normalize_schema
        parent = self._definition
        scope = content.get('native_task_onboarding')
        if (content.get('target_operation') != 'HeroTasks' or parent.operation_name != 'tasks'
                or scope != {'confirmed': True, 'source_instance': parent.source_instance, 'source_type': 'Task'}):
            return False
        try:
            normalized = normalize_schema(schema)
            query_name = normalized['queryType']['name']
            query_type = next(item for item in normalized['types'] if item.get('name') == query_name)
            roots = [item for item in query_type.get('fields', []) if item.get('name') == 'tasks']
            ref = roots[0]['type'] if len(roots) == 1 else None
            while isinstance(ref, dict) and ref.get('kind') in {'NON_NULL', 'LIST'}:
                ref = ref.get('ofType')
            if not isinstance(ref, dict) or ref.get('kind') != 'OBJECT' or ref.get('name') != 'Task':
                return False
        except (KeyError, TypeError, ValueError, StopIteration):
            return False
        contexts, rules = content.get('field_contexts'), content.get('field_rules')
        if not isinstance(contexts, dict) or not contexts or not isinstance(rules, dict):
            return False
        for identity, context in contexts.items():
            if identity not in content.get('mapping_definition_ids', []) or not isinstance(context, dict):
                return False
            try:
                rule = rules[identity]
                spec = MappingSpec(operation='HeroTasks', definition_id=identity, **rule)
                validate_native_task_rule(spec)
                chosen = schema_field(schema, context['object_type'], spec.source_path, context['schema_context'])
            except (KeyError, TypeError, ValueError):
                return False
            if context['object_type'] != 'Task' or chosen['relations'] or chosen['arguments']:
                return False
        return True

    def _publication_content(self, draft, schema):
        from uuid import NAMESPACE_URL, uuid5
        from hero_integration.services.personal_mapping_content import RULE_FIELDS, same
        from hero_integration.services.mapping_suggestions import source_fields
        from hero_integration.services.frappe_writeback import normalise_mapping
        from hero_integration.api import _mapping_values_preserved

        f, parent = self.frappe, self._definition
        content = _object(draft.content)
        additions = content.pop('additional_mappings', [])
        created = []
        if parent.direction == 'FRAPPE_TO_MUTATION':
            if 'field_rules' in content or 'field_rule_bases' in content:
                raise MappingDraftError('Mutation mapping has unexpected personal rule snapshots.')
            normalized = normalise_mapping(content, schema)
            if not _mapping_values_preserved(content, normalized):
                raise MappingDraftError('Active writeback normalization would lose draft parameters.')
            for item in additions:
                candidate = {'source': item['source'], 'target': {k: item['target'][k] for k in
                    ('operation', 'argument', 'input_type', 'field')}, 'transformation': item['transformation']}
                normalized_child = normalise_mapping(candidate, schema)
                child = f.get_doc({'doctype': 'HERO GraphQL Mapping', 'definition_id': item['id'],
                    'schema_snapshot': parent.schema_snapshot, 'operation_kind': 'MUTATION',
                    'operation_name': parent.operation_name, 'direction': parent.direction,
                    'status': 'Active', 'mapping_json': json.dumps(normalized_child, sort_keys=True), 'mapping_revision': 1})
                child.insert()
                created.append(('HERO GraphQL Mapping', child.name))
            return normalized, created
        if parent.direction != 'QUERY_TO_FRAPPE':
            raise MappingDraftError('Unsupported publication direction.')
        if (isinstance(content.get('source'), dict) and
                ({'object_type', 'schema_context'} & set(content['source'])) or
                any('object_type' in item.get('source', {}) for item in additions)):
            raise MappingDraftError('Object-first Draft fields require an approved fetch and publication contract before activation.')
        if content.get('field_contexts'):
            if not (self._approved_relationship_contexts(content, schema) or self._approved_task_contexts(content, schema)):
                raise MappingDraftError('Object-first Draft fields require an approved fetch and publication contract before activation.')
            content.pop('field_contexts')
        ids = content.get('mapping_definition_ids')
        rules, bases = content.pop('field_rules', {}), content.pop('field_rule_bases', {})
        if not isinstance(ids, list) or not ids or not isinstance(rules, dict) or not isinstance(bases, dict):
            raise MappingDraftError('A read mapping requires saved field-rule references.')
        self._assert_locked_rule_baseline(draft)
        projected = []
        for identity in ids:
            rows = f.db.sql('select * from `tabHERO Frappe Mapping` where definition_id=%s for update', (identity,), as_dict=True)
            if len(rows) != 1 or identity not in rules or identity not in bases:
                raise DraftConflict('Referenced field-rule identity is unavailable.')
            original = f.get_doc(dict(rows[0], doctype='HERO Frappe Mapping'))
            original.check_permission('read')
            if (original.status != 'Active' or original.schema_snapshot != parent.schema_snapshot
                    or original.schema_fingerprint != parent.schema_fingerprint
                    or (original.personal_graphql_mapping and original.personal_graphql_mapping != parent.name)
                    or original.operation != (content.get('target_operation') or parent.operation_name)):
                raise DraftConflict('Referenced field rule is outside the current mapping scope.')
            snapshot = {field: original.get(field) for field in RULE_FIELDS}
            provenance = {'name': original.name, 'revision': int(original.mapping_revision), 'operation': original.operation,
                          'schema_snapshot': original.schema_snapshot, 'schema_fingerprint': original.schema_fingerprint}
            if not same(provenance, bases[identity]):
                raise DraftConflict('A referenced field rule changed after the draft was opened.')
            if same(snapshot, rules[identity]):
                projected.append(identity)
                continue
            values = {field: rules[identity].get(field) for field in RULE_FIELDS}
            new_identity = str(uuid5(NAMESPACE_URL, f'{draft.key}:{draft.generation}:{draft.content_revision}:{identity}'))
            child = f.get_doc({'doctype': 'HERO Frappe Mapping', 'definition_id': new_identity,
                'operation': original.operation, 'schema_snapshot': original.schema_snapshot, 'status': 'Active',
                'personal_graphql_mapping': parent.name,
                'mapping_revision': 1, 'schema_dependencies': original.schema_dependencies,
                'schema_manifest': original.schema_manifest, **values})
            child.insert()
            created.append(('HERO Frappe Mapping', child.name))
            projected.append(new_identity)
        operation = content.get('target_operation') or parent.operation_name
        sources = {item.path: item for item in source_fields(operation, schema) if item.selectable}
        for item in additions:
            source = sources.get(item['source']['field'])
            if source is None or not source.mapping_type:
                raise MappingDraftError('Additional source field is not approved.')
            child = f.get_doc({'doctype': 'HERO Frappe Mapping', 'definition_id': item['id'],
                'operation': operation, 'source_path': source.path, 'source_type': source.mapping_type,
                'personal_graphql_mapping': parent.name,
                'target_doctype': item['target']['doctype'], 'target_field': item['target']['field'],
                'purpose': 'Field', 'transformation': item['transformation'], 'priority': 5,
                'schema_snapshot': parent.schema_snapshot, 'status': 'Active', 'mapping_revision': 1})
            child.insert()
            created.append(('HERO Frappe Mapping', child.name))
            projected.append(item['id'])
        content['mapping_definition_ids'] = projected
        from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _FrappeMetadata, _spec
        from hero_integration.services.direct_mapping import validate_spec
        from hero_integration.services.mapping_runtime import plan_from_definitions
        specifications = []
        for identity in projected:
            rows = f.db.sql('select * from `tabHERO Frappe Mapping` where definition_id=%s for update',
                            (identity,), as_dict=True)
            if len(rows) != 1:
                raise DraftConflict('A selected field-rule version is unavailable.')
            rule = f.get_doc(dict(rows[0], doctype='HERO Frappe Mapping'))
            rule.check_permission('read')
            specification = _spec(rule)
            validate_spec(specification, _FrappeMetadata())
            specifications.append(specification)
        targets = [(spec.target_doctype, spec.target_field) for spec in specifications if spec.purpose != 'Record Name']
        if len(set(targets)) != len(targets):
            raise MappingDraftError('The published mapping contains duplicate target fields.')
        native_task = content.get('native_task_onboarding') is not None
        plan = plan_from_definitions(parent.operation_name, specifications, target_operation=operation, native_task=native_task)
        if native_task:
            from hero_integration.api import _validated_snapshot
            from hero_integration.services.read_contract import task_materialization
            snapshot, _ = _validated_snapshot(parent.schema_snapshot, for_update=True)
            try:
                _, content['generated_task_seal'] = task_materialization(snapshot, content, plan)
            except (ValueError, TypeError, KeyError) as error:
                raise MappingDraftError('Native Task mapping cannot be materialized for publication.') from error
        self._validate_publication_read_contract(content)
        return content, created

    def _assert_locked_rule_baseline(self, draft):
        from hero_integration.services.mapping_draft_service import _hash
        from hero_integration.services.personal_mapping_content import RULE_FIELDS, references
        f, parent = self.frappe, self._definition
        current = _object(json.loads(parent.mapping_json))
        identities = references(current)
        if identities:
            current['field_rules'], current['field_rule_bases'] = {}, {}
            for identity in sorted(identities):
                rows = f.db.sql('select * from `tabHERO Frappe Mapping` where definition_id=%s for update',
                                (identity,), as_dict=True)
                if len(rows) != 1:
                    raise DraftConflict('An active field rule is unavailable.')
                rule = f.get_doc(dict(rows[0], doctype='HERO Frappe Mapping'))
                rule.check_permission('read')
                if (rule.status != 'Active' or rule.schema_snapshot != parent.schema_snapshot
                        or rule.schema_fingerprint != parent.schema_fingerprint
                        or (rule.personal_graphql_mapping and rule.personal_graphql_mapping != parent.name)
                        or rule.operation != (current.get('target_operation') or parent.operation_name)):
                    raise DraftConflict('An active field rule changed scope.')
                current['field_rules'][identity] = {field: rule.get(field) for field in RULE_FIELDS}
                current['field_rule_bases'][identity] = {'name': rule.name, 'revision': int(rule.mapping_revision),
                    'operation': rule.operation, 'schema_snapshot': rule.schema_snapshot,
                    'schema_fingerprint': rule.schema_fingerprint}
        expected = (draft.reconciliation or {}).get('active_content_hash') if draft.reconciliation else _hash(draft.base_content)
        if not isinstance(expected, str) or _hash(current) != expected:
            raise DraftConflict('The active field-rule content changed; reconcile before publication.')

    def _validate_publication_read_contract(self, content):
        from types import SimpleNamespace
        from hero_integration.services.read_contract import from_documents
        f, parent = self.frappe, self._definition
        snapshot = f.get_doc('HERO GraphQL Schema Snapshot', parent.schema_snapshot, for_update=True)
        seal = content.get('generated_task_seal')
        if content.get('native_task_onboarding') and isinstance(seal, dict):
            from hashlib import sha256
            candidates = f.db.sql(
                'select * from `tabHERO GraphQL Service` where schema_snapshot=%s and status=%s for update',
                (parent.schema_snapshot, 'Validated'), as_dict=True)
            for row in candidates:
                service = f.get_doc(dict(row, doctype='HERO GraphQL Service'))
                service.check_permission('read')
                document = service.query_document
                if (service.modified_by == f.session.user and service.operation_name == 'tasks'
                        and service.schema_fingerprint == parent.schema_fingerprint and isinstance(document, str)
                        and sha256(document.encode('utf-8')).hexdigest() == seal.get('document_sha256')):
                    # This proves a validated fetch definition exists; it does
                    # not approve it or authorize any read execution.
                    return
        services = f.db.sql('select * from `tabHERO GraphQL Service` where read_mapping=%s and status=%s for update',
                            (parent.name, 'Validated'), as_dict=True)
        approved = []
        for row in services:
            service = f.get_doc(dict(row, doctype='HERO GraphQL Service'))
            service.check_permission('read')
            try:
                from_documents(service, parent, snapshot)
            except ValueError:
                continue
            if content.get('native_task_onboarding') and content.get('generated_task_seal'):
                # Publication changes the mapping revision and therefore makes
                # this old approval stale. A new generated service must be
                # separately approved against the resulting active revision.
                approved.append(service.name)
                continue
            values = service.as_dict()
            values['approved_mapping_revision'] = int(parent.mapping_revision) + 1
            candidate = parent.as_dict()
            candidate.update(mapping_json=json.dumps(content, ensure_ascii=False, sort_keys=True),
                             mapping_revision=int(parent.mapping_revision) + 1, status='Active')
            from_documents(SimpleNamespace(**values), SimpleNamespace(**candidate), snapshot)
            approved.append(service.name)
        if not approved:
            raise MappingDraftError('A current approved read service is required for publication.')

    def _publication_objects(self, created, content):
        objects = list(created)
        for identity in content.get('mapping_definition_ids', []):
            rows = self.frappe.db.sql('select name from `tabHERO Frappe Mapping` where definition_id=%s for update',
                                      (identity,), as_dict=True)
            if len(rows) != 1:
                raise DraftConflict('Published field-rule identity is unavailable.')
            objects.append(('HERO Frappe Mapping', rows[0].name))
        return [self._publication_object(doctype, name) for doctype, name in sorted(set(objects))]

    def _publication_object(self, doctype, name):
        from hero_integration.services.mapping_draft_service import _hash
        from hero_integration.services.personal_mapping_content import RULE_FIELDS
        if doctype not in {'HERO GraphQL Mapping', 'HERO Frappe Mapping'}:
            raise MappingDraftError('Invalid publication receipt.')
        rows = self.frappe.db.sql(f'select * from `tab{doctype}` where name=%s for update', (name,), as_dict=True)
        if len(rows) != 1:
            raise DraftConflict('Published mapping is unavailable.')
        row = rows[0]
        keys = ('definition_id', 'mapping_revision', 'status', 'schema_snapshot', 'schema_fingerprint')
        value = {key: row.get(key) for key in keys}
        if doctype == 'HERO GraphQL Mapping':
            value['mapping_json'] = json.loads(row.mapping_json)
        else:
            value.update({key: row.get(key) for key in RULE_FIELDS})
            value['operation'] = row.operation
            value['personal_graphql_mapping'] = row.personal_graphql_mapping
        return {'doctype': doctype, 'name': name, 'hash': _hash(value)}

    def _publication_receipt_current(self, receipt):
        objects = receipt.get('objects')
        if not isinstance(objects, list) or len(objects) > 200:
            return False
        try:
            return all(isinstance(item, dict) and set(item) == {'doctype', 'name', 'hash'}
                       and self._publication_object(item['doctype'], item['name']) == item for item in objects)
        except (ValueError, TypeError, KeyError):
            return False
