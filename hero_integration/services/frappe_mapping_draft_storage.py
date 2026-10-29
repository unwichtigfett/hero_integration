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
_first_publication = ContextVar("hero_mapping_first_publication", default=None)


def first_publication_admits(kind, identity, row=None):
    """Admit only identities owned by the locked draft in this publication."""
    import frappe
    scope = _first_publication.get()
    if not scope or scope['site'] != frappe.local.site or scope['actor'] != frappe.session.user:
        return False
    allowed = scope['definitions'] if kind == 'definition' else scope['rules'] if kind == 'rule' else ()
    if identity not in allowed:
        return False
    return row is None or (row.draft_key == scope['key'] and row.owner_user == scope['actor']
                           and row.definition_id == scope['definition_id']
                           and int(row.generation) == scope['generation'])


def assert_draft_identity_index(f):
    """Require the source-controlled full definition_id index before range locks."""
    rows = f.db.sql('show index from `tabHERO Mapping Draft`', as_dict=True)
    if not any(row.get('Column_name') == 'definition_id' and int(row.get('Seq_in_index') or 0) == 1
               and row.get('Sub_part') is None for row in rows):
        raise MappingDraftError('Portable Draft identity index is not installed; migrate the app before using this operation.')


def assert_portable_identity_schema(f):
    """Fail closed until the app's portable identity columns and unique indexes exist."""
    for doctype in ('HERO GraphQL Mapping', 'HERO Frappe Mapping'):
        if not f.db.has_column(doctype, 'portable_definition_id'):
            raise MappingDraftError('Portable identity schema is not installed; migrate the app before using this operation.')
        rows = f.db.sql(f'show index from `tab{doctype}`', as_dict=True)
        names = {row.get('Key_name') for row in rows if row.get('Column_name') == 'portable_definition_id'
                 and int(row.get('Seq_in_index') or 0) == 1 and row.get('Non_unique') in (0, '0')
                 and row.get('Sub_part') is None and row.get('Key_name')}
        if not any(sum(row.get('Key_name') == name for row in rows) == 1 for name in names):
            raise MappingDraftError('Portable identity unique index is not installed; migrate the app before using this operation.')
    assert_draft_identity_index(f)


def assert_first_publication_identity_schema(f):
    """Require database-enforced uniqueness for the local definition identity."""
    assert_portable_identity_schema(f)
    for doctype in ('HERO GraphQL Mapping', 'HERO Frappe Mapping'):
        rows = f.db.sql(f'show index from `tab{doctype}`', as_dict=True)
        keys = {row.get('Key_name') for row in rows if row.get('Column_name') == 'definition_id'
                and int(row.get('Seq_in_index') or 0) == 1 and row.get('Non_unique') in (0, '0')
                and row.get('Sub_part') is None and row.get('Key_name')}
        if not any(sum(row.get('Key_name') == key for row in rows) == 1 for key in keys):
            raise MappingDraftError('Definition identity unique index is not installed; migrate the app before publication.')


def _fresh_identity_candidates(f, identity):
    """Find committed candidates without locking unrelated Draft rows."""
    from uuid import UUID
    from frappe.database import get_db
    conf = f.local.conf
    db = get_db(socket=conf.db_socket, host=conf.db_host, port=conf.db_port,
                user=conf.db_user or conf.db_name, password=conf.db_password, cur_db_name=conf.db_name)
    try:
        return db.sql('select d.draft_key, d.content_json, d.new_definition_json, d.base_active_mapping, '
                      'p.direction as active_direction from `tabHERO Mapping Draft` d '
                      'left join `tabHERO GraphQL Mapping` p on p.name=d.base_active_mapping '
                      'where d.site_scope=%s and d.state=%s and (lower(d.content_json) like %s or lower(d.content_json) like %s)',
                      (f.local.site, 'OPEN', '%' + identity + '%', '%' + UUID(identity).hex + '%'), as_dict=True)
    finally:
        db.close()


def _fresh_definition_candidates(f, identity):
    from frappe.database import get_db
    conf = f.local.conf
    db = get_db(socket=conf.db_socket, host=conf.db_host, port=conf.db_port,
                user=conf.db_user or conf.db_name, password=conf.db_password, cur_db_name=conf.db_name)
    try:
        return db.sql('select draft_key from `tabHERO Mapping Draft` where site_scope=%s and definition_id=%s',
                      (f.local.site, identity), as_dict=True)
    finally:
        db.close()


def lock_definition_drafts(f, identities):
    """Under definition mutexes, lock exact existing Draft keys, including discarded generations."""
    identities = set(identities)
    keys = {row.draft_key for identity in sorted(identities) for row in _fresh_definition_candidates(f, identity)}
    result = []
    for key in sorted(keys):
        rows = f.db.sql('select * from `tabHERO Mapping Draft` where draft_key=%s for update', (key,), as_dict=True)
        if len(rows) > 1:
            raise DraftConflict('Personal Draft identity is ambiguous.')
        if rows and rows[0].site_scope == f.local.site and rows[0].definition_id in identities:
            result.append(rows[0])
    return result


def _uuid_equal(value, identity):
    from uuid import UUID
    try:
        return str(UUID(value)) == identity
    except (TypeError, ValueError, AttributeError):
        return False


def _identity_is_referenced(content, identity, kind, direction=None):
    if not isinstance(content, dict):
        raise DraftConflict('Existing personal Draft cannot be safely inspected.')
    from hero_integration.services.personal_mapping_content import identity_sets
    rules, children = identity_sets(content, direction)
    if kind == 'rule':
        return any(_uuid_equal(value, identity) for value in rules)
    if kind == 'child':
        return identity in children
    raise DraftConflict('Unknown Draft identity namespace.')


def _draft_identity_direction(f, row, *, active_direction=None):
    raw = row.get('new_definition_json')
    if raw:
        try:
            context = json.loads(raw)
        except (ValueError, TypeError):
            raise DraftConflict('Existing Draft context cannot be safely inspected.') from None
        direction = context.get('direction') if isinstance(context, dict) else None
    elif active_direction is not None:
        direction = active_direction
    elif row.get('base_active_mapping'):
        rows = f.db.sql('select direction from `tabHERO GraphQL Mapping` where name=%s',
                        (row['base_active_mapping'],), as_dict=True)
        direction = rows[0].get('direction') if len(rows) == 1 else None
    else:
        direction = None
    if direction not in {'QUERY_TO_FRAPPE', 'FRAPPE_TO_MUTATION'}:
        raise DraftConflict('Existing Draft direction cannot be safely inspected.')
    return direction


def lock_identity_drafts(f, identities, kind):
    """After the identity mutex, lock only exact Draft keys and revalidate them."""
    candidates = set()
    identities = sorted(set(identities))
    for identity in identities:
        for row in _fresh_identity_candidates(f, identity):
            try:
                content = json.loads(row.content_json)
            except (TypeError, ValueError):
                raise DraftConflict('Existing personal Draft cannot be safely inspected.') from None
            direction = _draft_identity_direction(f, row, active_direction=row.get('active_direction'))
            if _identity_is_referenced(content, identity, kind, direction):
                candidates.add(row.draft_key)
    result = []
    for key in sorted(candidates):
        rows = f.db.sql('select * from `tabHERO Mapping Draft` where draft_key=%s for update',
                        (key,), as_dict=True)
        if len(rows) > 1:
            raise DraftConflict('Personal Draft identity is ambiguous.')
        if not rows:
            continue
        row = rows[0]
        if row.site_scope != f.local.site or row.state != 'OPEN':
            continue
        try:
            content = json.loads(row.content_json)
        except (TypeError, ValueError):
            raise DraftConflict('Existing personal Draft cannot be safely inspected.') from None
        direction = _draft_identity_direction(f, row)
        if any(_identity_is_referenced(content, identity, kind, direction) for identity in identities):
            result.append(row)
    return result


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
        self._parentless_identity = None
        self._locked_definition_ids = set()
        self._locked_rule_ids = set()
        self._locked_local_rule_ids = set()

    def service(self) -> MappingDraftService:
        return MappingDraftService(self, canonicalize=self.canonicalize)

    def lock_draft_identities(self, actor, definition_id, content, *, extra_rule_ids=(), direction=None):
        """Take sorted definition and rule locks before a personal draft transaction."""
        from uuid import UUID
        from hero_integration.services.personal_mapping_content import identity_sets
        from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
        self._authorize(actor)
        assert_portable_identity_schema(self.frappe)
        value = _object(content)
        contents = [value]
        active_rows = self.frappe.db.sql('select mapping_json, direction from `tabHERO GraphQL Mapping` where definition_id=%s',
                                         (definition_id,), as_dict=True)
        if len(active_rows) > 1:
            raise DraftConflict('Mapping definition identity is ambiguous.')
        if active_rows:
            active_direction = active_rows[0].get('direction')
            if direction and direction != active_direction:
                raise DraftConflict('Mapping direction changed while identities were resolved.')
            direction = active_direction
            try:
                contents.append(_object(json.loads(active_rows[0].mapping_json)))
            except (ValueError, TypeError):
                raise DraftConflict('Active mapping cannot be safely inspected.') from None
        key = draft_key(self.frappe.local.site, definition_id, actor)
        previous = self.frappe.db.sql('select content_json, new_definition_json from `tabHERO Mapping Draft` where draft_key=%s',
                                      (key,), as_dict=True)
        if len(previous) > 1:
            raise DraftConflict('Personal Draft identity is ambiguous.')
        if previous:
            try:
                contents.append(_object(json.loads(previous[0].content_json)))
                context = json.loads(previous[0].get('new_definition_json') or 'null')
                if context:
                    prior_direction = context.get('direction')
                    if direction and direction != prior_direction:
                        raise DraftConflict('Draft direction changed while identities were resolved.')
                    direction = prior_direction
            except (ValueError, TypeError):
                raise DraftConflict('Previous personal Draft cannot be safely inspected.') from None
        definition_ids = {definition_id}
        rule_ids = set()
        for current in contents:
            rules, children = identity_sets(current, direction)
            definition_ids.update(children)
            rule_ids.update(rules)
        ids = set(extra_rule_ids)
        for rule_id in sorted(rule_ids):
            try:
                ids.add(str(UUID(rule_id)))
            except (ValueError, TypeError, AttributeError):
                pass  # Existing nonportable legacy rule IDs are still supported by v1.
            rows = self.frappe.get_all('HERO Frappe Mapping', filters={'definition_id': rule_id},
                                       fields=['portable_definition_id'])
            if len(rows) > 1:
                raise DraftConflict('Field-rule identity is ambiguous.')
            portable = rows[0].get('portable_definition_id') if rows else None
            if portable:
                try:
                    if str(UUID(portable)) != portable: raise ValueError
                except (ValueError, TypeError, AttributeError):
                    raise DraftConflict('Field-rule portable identity is invalid.') from None
                ids.add(portable)
        for identity in sorted(definition_ids):
            _lock_active_target('personal-new-definition', self.frappe.local.site, identity)
        for identity in sorted(ids):
            _lock_active_target('portable-field-rule', self.frappe.local.site, identity)
        self._locked_definition_ids, self._locked_rule_ids, self._locked_local_rule_ids = definition_ids, ids, rule_ids
        self._locked_direction = direction
        for rule_id in sorted(rule_ids):
            rows = self.frappe.db.sql('select portable_definition_id from `tabHERO Frappe Mapping` '
                                      'where definition_id=%s for update', (rule_id,), as_dict=True)
            if len(rows) > 1 or (rows and rows[0].get('portable_definition_id')
                                 and rows[0]['portable_definition_id'] not in ids):
                raise DraftConflict('Field-rule identity changed while locks were acquired.')

    def assert_locked_draft_identities(self, definition_id, actor):
        """Recheck the saved identity set after acquiring the parent and Draft locks."""
        from uuid import UUID
        from hero_integration.services.personal_mapping_content import identity_sets
        draft = self.draft(draft_key(self.frappe.local.site, definition_id, actor))
        if draft is None:
            return
        direction = self._definition.direction if self._definition else (draft.new_definition or {}).get('direction')
        if direction != self._locked_direction:
            raise DraftConflict('Draft direction changed while locks were acquired.')
        current_rules, children = identity_sets(draft.content, direction)
        if not children <= self._locked_definition_ids:
            raise DraftConflict('Personal Draft identities changed while locks were acquired.')
        for identity in current_rules:
            if identity not in self._locked_local_rule_ids:
                raise DraftConflict('Personal Draft identities changed while locks were acquired.')
            try:
                canonical = str(UUID(identity))
            except (ValueError, TypeError, AttributeError):
                continue
            if canonical not in self._locked_rule_ids:
                raise DraftConflict('Personal Draft identities changed while locks were acquired.')

    def materialize_portable_draft(self, definition_id, actor, generation, revision, active_revision, reconciliation_revision):
        """Compile a saved personal draft as a non-executable operation plan."""
        from hero_integration.api import _validated_snapshot
        from hero_integration.services.mapping_draft_service import _hash
        from hero_integration.services.mapping_contract_v1 import (
            MappingContractError, MutationAdapter, MutationDraft, QueryAdapter, QueryDraft, SnapshotAdapter, StoredSnapshot,
        )
        from hero_integration.services.mapping_draft_exchange import _portable_content, _validate_portable_query
        from hero_integration.services.personal_mapping_content import references

        with self.transaction(definition_id, actor):
            active, draft = self.service()._current(definition_id, actor)
            if (type(generation) is not int or generation != draft.generation or type(revision) is not int
                    or revision < 1 or type(reconciliation_revision) is not int
                    or reconciliation_revision != draft.reconciliation_revision):
                raise DraftConflict('Personal draft revision changed before plan generation.')
            self.service()._assert_current(active, draft, revision, active_revision)
            self.frappe.get_doc(self.DRAFT, draft.key, for_update=True).check_permission('read')
            if _hash(draft.content) != draft.content_hash or self.canonicalize(draft.content) != draft.content:
                raise MappingDraftError('Saved personal content is not intact and canonical.')
            saved = self.frappe.db.sql('select content_hash from `tabHERO Mapping Revision` '
                'where draft_key=%s and generation=%s and revision=%s and kind=%s for update',
                (draft.key, generation, revision, 'DRAFT_SAVE'), as_dict=True)
            if len(saved) != 1 or saved[0].content_hash != draft.content_hash:
                raise DraftConflict('Saved draft history does not match the requested revision.')
            parent = self._definition
            context = draft.new_definition if parent is None else {
                'snapshot_name': parent.schema_snapshot, 'source_instance': parent.source_instance,
                'direction': parent.direction, 'operation_name': parent.operation_name}
            snapshot, schema = _validated_snapshot(context['snapshot_name'], for_update=True)
            snapshot.check_permission('read')
            if snapshot.source_instance != context['source_instance'] or snapshot.schema_fingerprint != draft.base_schema_fingerprint:
                raise DraftConflict('Draft schema context changed before plan generation.')
            try:
                validated = SnapshotAdapter().adapt(StoredSnapshot(snapshot.name, schema, snapshot.schema_fingerprint,
                                                                   snapshot.source_instance, snapshot.analysis_status))
                content = _object(draft.content)
                content.pop('_portable_exchange_schema', None)
                content.pop('field_rule_bases', None)
                content = _portable_content(content, context['direction'])
                if context['direction'] == 'QUERY_TO_FRAPPE':
                    if content.get('records_path', content.get('record_path')) != context['operation_name']:
                        raise MappingDraftError('Only direct query-root record paths are supported for this plan.')
                    _validate_portable_query(content, context['operation_name'], schema)
                    paths = tuple(sorted({content['identity_path']} | {
                        content['field_rules'][identity]['source_path'] for identity in references(content)}))
                    request = QueryAdapter().materialize(validated, QueryDraft('personal_draft', context['operation_name'],
                                                          context['operation_name'], paths, {}))
                    return {'direction': context['direction'], 'execution_authority': 'NONE', 'document': request.document,
                            'variables': request.variables, 'selected_paths': list(paths), 'draft_revision': revision}
                assignments = [content] + list(content.get('additional_mappings') or [])
                paths = []
                for assignment in assignments:
                    target = assignment.get('target', {})
                    if target.get('operation') != context['operation_name']:
                        raise MappingDraftError('Mutation operation does not match the draft context.')
                    paths.append((target['argument'], *target['field'].split('.')))
                if len(set(paths)) != len(paths):
                    raise MappingDraftError('Mutation assignments have duplicate input paths.')
                result = MutationAdapter().materialize(validated, MutationDraft(context['operation_name'], tuple(paths),
                                                                                snapshot.source_instance, revision))
                return {**result, 'direction': context['direction'], 'execution_authority': 'NONE', 'draft_revision': revision}
            except (MappingContractError, KeyError, TypeError, ValueError) as error:
                raise MappingDraftError('Saved draft cannot produce a compatible non-executable plan.') from error

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
        if type(active_revision) is int and active_revision == 0:
            return self._publish_parentless(definition_id, actor, generation, revision, reconciliation_revision,
                                            warning_fingerprint=warning_fingerprint)
        # Use the same lock order as directional exchange: context before
        # parent. The initial lookup identifies only the lock to acquire; the
        # current, locked parent is checked again below before publication.
        rows = self.frappe.db.sql(
            'select name, operation_name, source_instance, schema_snapshot, direction from `tabHERO GraphQL Mapping` '
            'where definition_id=%s', (definition_id,), as_dict=True)
        if len(rows) != 1:
            raise MappingDraftError('Exactly one mapping definition is required.')
        scope = (rows[0].source_instance or '', rows[0].schema_snapshot, rows[0].direction)
        from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
        lock_exchange_context(*scope)
        key = draft_key(self.frappe.local.site, definition_id, actor)
        observed = self.frappe.db.sql('select content_json, base_content_json from `tabHERO Mapping Draft` '
            'where draft_key=%s and site_scope=%s and owner_user=%s and state=%s',
            (key, self.frappe.local.site, actor, 'OPEN'), as_dict=True)
        if len(observed) != 1:
            raise DraftConflict('The personal draft is not open.')
        try:
            content = _object(json.loads(observed[0].content_json))
            base_content = _object(json.loads(getattr(observed[0], 'base_content_json', None) or '{}'))
            from hero_integration.services.personal_mapping_content import references, validate_additional_mappings
            validate_additional_mappings(content.get('additional_mappings', []))
            extra_rule_ids = set()
            if scope[2] == 'QUERY_TO_FRAPPE':
                from uuid import NAMESPACE_URL, uuid5
                from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
                operation = content.get('target_operation') or rows[0].operation_name
                targets = {(rule.get('target_doctype'), rule.get('target_field'))
                           for state in (content, base_content) for rule in (state.get('field_rules') or {}).values()
                           if isinstance(rule, dict) and rule.get('purpose') != 'Record Name'}
                targets.update((item['target'].get('doctype'), item['target'].get('field'))
                               for state in (content, base_content) for item in state.get('additional_mappings', []))
                if any(not isinstance(value, str) or not value for target in targets for value in target):
                    raise DraftConflict('Personal draft target cannot be safely locked.')
                target_locks = {(prefix + operation, doctype, field) for doctype, field in targets
                                for prefix in ('', f'personal:{rows[0].name}:')}
                for lock_operation, doctype, field in sorted(target_locks):
                    _lock_active_target(lock_operation, doctype, field)
                self._publication_target_locks = target_locks
                extra_rule_ids.update(str(uuid5(NAMESPACE_URL, f'{key}:{generation}:{revision}:{identity}'))
                                      for identity in references(content))
                extra_rule_ids.update(item['id'] for item in content.get('additional_mappings', []))
            self.lock_draft_identities(actor, definition_id, content, extra_rule_ids=extra_rule_ids)
        except (ValueError, TypeError, KeyError, MappingDraftError):
            raise DraftConflict('Personal draft identities cannot be safely inspected.') from None
        observed_hash = _hash(content)
        with self.transaction(definition_id, actor), _publication_operation_scope(self.frappe.local.site, actor, self._definition.name):
            f = self.frappe
            if ((self._definition.source_instance or '', self._definition.schema_snapshot, self._definition.direction) != scope
                    or self._definition.name != rows[0].name or self._definition.operation_name != rows[0].operation_name):
                raise DraftConflict('Mapping context changed; reload before publication.')
            active = self.active(definition_id)
            key = draft_key(f.local.site, definition_id, actor)
            draft = self.draft(key)
            if draft is None or draft.state != 'OPEN' or draft.owner != actor:
                raise DraftConflict('The personal draft is not open.')
            if draft.content_hash != observed_hash:
                raise DraftConflict('Personal draft changed while publication locks were acquired.')
            if _hash(draft.base_content) != _hash(base_content):
                raise DraftConflict('Personal draft baseline changed while publication locks were acquired.')
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
            if self._definition.direction == 'FRAPPE_TO_MUTATION':
                self._lock_mutation_child_identities(draft)
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
            from hero_integration.services.personal_mapping_content import same
            if not created and same(content, active.content):
                return {'name': active.name, 'revision': active.revision, 'unchanged': True}
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

    def _publish_parentless(self, definition_id, actor, generation, revision, reconciliation_revision, *, warning_fingerprint):
        """Publish a saved parentless draft, or verify an identical prior publication."""
        from hero_integration.services.mapping_draft_service import _hash, _new_definition
        from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context

        f = self.frappe
        key = draft_key(f.local.site, definition_id, actor)
        observed = f.db.sql('select content_json, new_definition_json from `tabHERO Mapping Draft` '
                            'where draft_key=%s and site_scope=%s and owner_user=%s',
                            (key, f.local.site, actor), as_dict=True)
        if len(observed) != 1:
            raise DraftConflict('The personal draft is not open.')
        try:
            content = _object(json.loads(observed[0].content_json))
            context = _new_definition(json.loads(observed[0].new_definition_json))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise DraftConflict('New-definition draft cannot be safely inspected.') from None
        if context['site_scope'] != f.local.site or context['definition_id'] != definition_id:
            raise DraftConflict('New-definition draft context changed.')
        lock_exchange_context(context['source_instance'], context['snapshot_name'], context['direction'])
        assert_first_publication_identity_schema(f)
        self.lock_draft_identities(actor, definition_id, content, direction=context['direction'])
        with self.transaction(definition_id, actor):
            active = self.active_or_none(definition_id)
            self.assert_locked_draft_identities(definition_id, actor)
            draft = self.draft(key)
            if (draft is None or draft.owner != actor or draft.new_definition != context
                    or _hash(content) != draft.content_hash or _hash(draft.content) != draft.content_hash):
                raise DraftConflict('New-definition draft changed while locks were acquired.')
            if (type(generation) is not int or generation != draft.generation
                    or type(revision) is not int or revision < 1 or revision != draft.content_revision
                    or type(reconciliation_revision) is not int or reconciliation_revision != draft.reconciliation_revision):
                raise DraftConflict('The personal draft revision is stale.')
            row = f.get_doc(self.DRAFT, key, for_update=True)
            row.check_permission('write')
            if row.modified_by != actor:
                raise PermissionError('Only the last author may publish this draft.')
            try:
                diagnostics = json.loads(row.diagnostics_json) if row.diagnostics_json else {}
            except (ValueError, TypeError) as error:
                raise MappingDraftError('Draft diagnostics are invalid.') from error
            if not isinstance(diagnostics, dict):
                raise MappingDraftError('Draft diagnostics are invalid.')
            if active is not None:
                receipt = diagnostics.get('personal_publication')
                if (draft.state == 'DISCARDED' and isinstance(receipt, dict)
                        and receipt.get('actor') == actor and receipt.get('definition_id') == definition_id
                        and receipt.get('generation') == generation and receipt.get('revision') == revision
                        and receipt.get('draft_hash') == draft.content_hash and receipt.get('expected_active_revision') == 0
                        and receipt.get('active_name') == active.name and receipt.get('active_revision') == active.revision
                        and receipt.get('active_hash') == active.content_hash
                        and receipt.get('objects') == self._publication_objects(
                            [('HERO GraphQL Mapping', active.name)], active.content)):
                    return {'name': active.name, 'revision': active.revision, 'unchanged': True}
                raise DraftConflict('Expected mapping absence no longer holds.')
            if draft.state != 'OPEN':
                raise DraftConflict('The personal draft is not open.')
            if row.warning_fingerprint:
                warnings = diagnostics.get('warnings')
                if (warning_fingerprint != row.warning_fingerprint or not isinstance(warnings, list) or not warnings
                        or len(warnings) > 100 or any(not isinstance(w, str) or not w or len(w) > 1000 for w in warnings)):
                    raise MappingDraftError('Current warnings must be available and acknowledged.')
            self.service()._assert_current(None, draft, revision, 0)
            saved = f.db.sql('select content_hash from `tabHERO Mapping Revision` where draft_key=%s and generation=%s '
                             'and revision=%s and kind=%s for update',
                             (key, generation, revision, 'DRAFT_SAVE'), as_dict=True)
            if len(saved) != 1 or saved[0].content_hash != draft.content_hash:
                raise DraftConflict('Saved draft history does not match the requested revision.')
            from hero_integration.services.mapping_draft_exchange import _portable_target_metadata
            try:
                locked_metadata = _portable_target_metadata(draft.content, context['direction'])
                for doctype, item in locked_metadata.items():
                    meta = f.get_meta(doctype)
                    declared = item['doctype']
                    if ((meta.get('module') or '') != (declared.get('module') or '')
                            or any(int(meta.get(k) or 0) != int(declared.get(k) or 0)
                                   for k in ('custom', 'istable', 'issingle', 'is_virtual'))):
                        raise MappingDraftError('Cached target DocType differs from the locked metadata.')
                    for stored in item['fields']:
                        field = meta.get_field(stored['fieldname'])
                        if (field is None or any((field.get(k) or '') != (stored.get(k) or '')
                                                 for k in ('fieldtype', 'options'))
                                or any(int(field.get(k) or 0) != int(stored.get(k) or 0)
                                       for k in ('reqd', 'read_only', 'hidden', 'permlevel', 'is_virtual'))):
                            raise MappingDraftError('Cached target metadata differs from the locked metadata.')
            except ValueError as error:
                raise MappingDraftError('Target metadata is unavailable or unsafe for publication.') from error
            if self.canonicalize(draft.content) != draft.content:
                raise MappingDraftError('Saved personal content is not canonical.')
            from hero_integration.api import _validated_snapshot
            snapshot, schema = _validated_snapshot(context['snapshot_name'], for_update=True)
            if snapshot.source_instance != context['source_instance'] or snapshot.schema_fingerprint != context['schema_fingerprint']:
                raise DraftConflict('New-definition schema context changed.')
            snapshot.check_permission('read')
            if draft.content.get('_portable_exchange_schema'):
                raise MappingDraftError('Portable schema dependencies require publication provenance support.')
            value = _object(draft.content)
            value.pop('_portable_exchange_schema', None)
            created = []
            if context['direction'] == 'QUERY_TO_FRAPPE':
                from hero_integration.services.mapping_draft_exchange import _portable_content, _validate_portable_query
                from hero_integration.services.direct_mapping import MappingSpec
                from hero_integration.services.mapping_runtime import plan_from_definitions
                try:
                    value = _portable_content(value, context['direction'])
                    _validate_portable_query(value, context['operation_name'], schema)
                    operation = value.get('target_operation') or context['operation_name']
                    specs = [MappingSpec(operation=operation, definition_id=identity, **value['field_rules'][identity])
                             for identity in value['mapping_definition_ids']]
                    plan_from_definitions(context['operation_name'], specs, target_operation=operation)
                except (ValueError, TypeError, KeyError) as error:
                    raise MappingDraftError('The query mapping is incomplete or incompatible with its schema.') from error
                targets = [(spec.target_doctype, spec.target_field) for spec in specs if spec.purpose != 'Record Name']
                if len(targets) != len(set(targets)):
                    raise MappingDraftError('The published mapping contains duplicate target fields.')
                active_content = dict(value)
                active_content.pop('field_rules')
            else:
                from hero_integration.services.frappe_writeback import normalise_mapping
                from hero_integration.api import _mapping_values_preserved
                if value.get('additional_mappings'):
                    raise MappingDraftError('First publication of multiple mutation assignments is not supported.')
                value.pop('additional_mappings', None)
                try:
                    active_content = normalise_mapping(value, schema)
                except ValueError as error:
                    raise MappingDraftError('Mutation mapping is not approved for activation.') from error
                if not _mapping_values_preserved(value, active_content):
                    raise MappingDraftError('Active writeback normalization would lose draft parameters.')
            rule_ids = set(value.get('mapping_definition_ids', [])) if context['direction'] == 'QUERY_TO_FRAPPE' else set()
            token = _first_publication.set({'site':f.local.site, 'actor':actor, 'key':key, 'generation':generation,
                'definition_id':definition_id, 'definitions':{definition_id}, 'rules':rule_ids})
            try:
                parent = f.get_doc({'doctype':'HERO GraphQL Mapping', 'definition_id':definition_id,
                    'portable_definition_id':definition_id, 'schema_snapshot':context['snapshot_name'],
                    'schema_fingerprint':context['schema_fingerprint'], 'source_instance':context['source_instance'],
                    'operation_kind':context['operation_kind'], 'operation_name':context['operation_name'],
                    'direction':context['direction'], 'status':'Active', 'mapping_revision':1,
                    'mapping_json':json.dumps(active_content, ensure_ascii=False, sort_keys=True, separators=(',', ':'))})
                parent.insert()
                self._definition, self._parentless_identity = parent, None
                with _publication_operation_scope(f.local.site, actor, parent.name):
                    for identity in sorted(rule_ids):
                        rule = f.get_doc({'doctype':'HERO Frappe Mapping', 'definition_id':identity,
                            'portable_definition_id':identity, 'operation':operation,
                            'schema_snapshot':context['snapshot_name'], 'schema_fingerprint':context['schema_fingerprint'],
                            'status':'Active', 'mapping_revision':1,
                            'personal_graphql_mapping':parent.name, **value['field_rules'][identity]})
                        rule.insert()
                        created.append(('HERO Frappe Mapping', rule.name))
                current = self.active(definition_id)
                if current.revision != 1 or current.schema_fingerprint != context['schema_fingerprint']:
                    raise MappingDraftError('First publication did not produce the expected active revision.')
                self.append_revision(MappingDraftRevision(key, generation, revision, 'ACTIVE_ACTIVATION', actor,
                    draft.content, draft.content_hash, '', 0, '', draft.base_schema_fingerprint))
                receipt = {'actor':actor, 'definition_id':definition_id, 'generation':generation, 'revision':revision,
                    'draft_hash':draft.content_hash, 'expected_active_revision':0, 'active_name':current.name,
                    'active_revision':current.revision, 'active_hash':current.content_hash,
                    'objects':self._publication_objects(created + [('HERO GraphQL Mapping', parent.name)], active_content)}
                diagnostics['personal_publication'] = receipt
                row.diagnostics_json = json.dumps(diagnostics, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                row.state = 'DISCARDED'
                row.save()
                return {'name':current.name, 'revision':current.revision, 'unchanged':False}
            finally:
                _first_publication.reset(token)

    def _lock_mutation_child_identities(self, draft):
        """Reject historical child-ID collisions before creating any child definition."""
        from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
        additions = draft.content.get('additional_mappings') or []
        ids = [item['id'] for item in additions]
        if len(ids) != len(set(ids)):
            raise DraftConflict('Mutation assignment identities are ambiguous.')
        for identity in sorted(ids):
            _lock_active_target('personal-new-definition', self.frappe.local.site, identity)
        rows = lock_identity_drafts(self.frappe, ids, 'child')
        for identity in sorted(ids):
            for row in rows:
                if row.draft_key == draft.key:
                    continue
                try:
                    content = json.loads(row.content_json)
                    children = content.get('additional_mappings', [])
                except (TypeError, ValueError, AttributeError):
                    raise DraftConflict('A related personal draft cannot be safely inspected.') from None
                if not isinstance(children, list):
                    raise DraftConflict('A related personal draft cannot be safely inspected.')
                if any(isinstance(child, dict) and child.get('id') == identity for child in children):
                    raise DraftConflict('Mutation assignment identity is already used by another personal draft.')

    @contextmanager
    def transaction(self, definition_id: str, actor: str):
        f = self.frappe
        self._authorize(actor)
        if self._definition is not None or self._parentless_identity is not None:
            raise MappingDraftError("Nested personal draft operations are not supported.")
        savepoint = "hero_draft_" + uuid4().hex
        f.db.savepoint(savepoint)
        token = _write_scope.set((f.local.site, f.session.user))
        try:
            from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
            _lock_active_target('personal-new-definition', f.local.site, definition_id)
            # Lock the EXISTING parent. This serializes first inserts as well as
            # CAS updates, unlike a lock on a not-yet-existing personal draft.
            rows = f.db.sql(
                "select * from `tabHERO GraphQL Mapping` where definition_id=%s for update",
                (definition_id,), as_dict=True,
            )
            if len(rows) > 1:
                raise MappingDraftError("Exactly one mapping definition is required.")
            if rows:
                self._definition = f.get_doc(dict(rows[0], doctype="HERO GraphQL Mapping"))
                self._definition.check_permission("read")
                if self._definition.status != "Active":
                    raise MappingDraftError("A current active mapping definition is required.")
            else:
                rows = f.db.sql('select name from `tabHERO GraphQL Mapping` where definition_id=%s for update',
                                (definition_id,), as_dict=True)
                if rows:
                    raise DraftConflict('Definition appeared while its identity was being locked.')
                rows = f.db.sql('select name from `tabHERO GraphQL Mapping` where portable_definition_id=%s for update',
                                (definition_id,), as_dict=True)
                if rows:
                    raise DraftConflict('Identity is enrolled for another active mapping definition.')
                self._parentless_identity = definition_id
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
            self._parentless_identity = None
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
        if self._definition is None and self._parentless_identity is None:
            raise PermissionError("A locked draft transaction is required.")

    def active_or_none(self, definition_id):
        self._scope()
        if self._definition is None:
            if self._parentless_identity != definition_id:
                raise PermissionError('Mapping definition scope mismatch.')
            return None
        return self.active(definition_id)

    def parentless(self, definition_id, actor):
        self._scope()
        if self._parentless_identity != definition_id or actor != self.frappe.session.user:
            raise PermissionError('Parentless draft scope mismatch.')
        value = self.draft(draft_key(self.frappe.local.site, definition_id, actor))
        if value is not None and value.new_definition is None:
            raise DraftConflict('Existing draft is not a new-definition draft.')
        return value

    def assert_new_identity_available(self, definition_id, actor):
        self._scope()
        if self._parentless_identity != definition_id or actor != self.frappe.session.user:
            raise DraftConflict('Definition identity already exists.')
        assert_draft_identity_index(self.frappe)
        rows = lock_definition_drafts(self.frappe, [definition_id])
        if any(row.owner_user != actor for row in rows):
            raise DraftConflict('Definition identity is already owned by another administrator.')
        if lock_identity_drafts(self.frappe, [definition_id], 'child'):
            raise DraftConflict('Definition identity is already used by a mutation assignment.')

    def validate_new_definition(self, definition, content):
        from hero_integration.api import _validated_snapshot
        from hero_integration.services.schema_operations import catalog
        if definition['site_scope'] != self.frappe.local.site:
            raise PermissionError('New definition belongs to another site.')
        snapshot, schema = _validated_snapshot(definition['snapshot_name'], for_update=True)
        snapshot.check_permission('read')
        if (snapshot.schema_fingerprint != definition['schema_fingerprint']
                or snapshot.source_instance != definition['source_instance']):
            raise DraftConflict('New definition source or schema does not match the target snapshot.')
        if definition['operation_name'] not in {item['operation_name'] for item in catalog(schema, definition['operation_kind'])}:
            raise MappingDraftError('New definition operation is absent from the target snapshot.')
        from hero_integration.services.personal_mapping_content import bounded, references, validate_rule
        value = bounded(_object(content))
        if value.get('native_task_onboarding') is not None:
            raise MappingDraftError('Imported content cannot grant native Task onboarding authority.')
        if definition['direction'] == 'QUERY_TO_FRAPPE':
            if value.get('additional_mappings'):
                raise MappingDraftError('Additional query assignments require an active mapping definition.')
            identities = references(value)
            rules = value.get('field_rules', {})
            if not isinstance(rules, dict) or set(identities) != set(rules) or value.get('field_rule_bases'):
                raise MappingDraftError('New query rules must be complete and have no imported local provenance.')
            from hero_integration.services.direct_mapping import MappingSpec, validate_spec
            from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _FrappeMetadata
            from uuid import UUID
            for identity in identities:
                try:
                    if str(UUID(identity)) != identity:
                        raise ValueError
                except (ValueError, TypeError, AttributeError):
                    raise MappingDraftError('New-definition field rules require canonical UUID identities.') from None
                validate_rule(rules[identity])
                if self._definition is None:
                    self._assert_parentless_rule_identity(definition, identity, rules[identity])
                try:
                    validate_spec(MappingSpec(operation=value.get('target_operation') or definition['operation_name'],
                                              definition_id=identity, **rules[identity]), _FrappeMetadata())
                except (TypeError, ValueError) as error:
                    raise MappingDraftError('New query field rule is incompatible with target metadata.') from error
        else:
            from hero_integration.services.frappe_writeback import normalise_draft_mapping, draft_source_type_compatible
            from hero_integration.services.frappe_mapping_catalog import FrappePickerMetadata, validate_draft_writeback_source
            from hero_integration.api import _mapping_values_preserved
            if set(value) - {'version', 'source', 'target', 'transformation', 'additional_mappings', '_portable_exchange_schema'}:
                raise MappingDraftError('New mutation content contains unsupported configuration.')
            assignments = [value] + list(value.get('additional_mappings') or [])
            from hero_integration.services.personal_mapping_content import validate_additional_mappings
            validate_additional_mappings(value.get('additional_mappings', []))
            if self._definition is None:
                self._assert_parentless_child_identities(definition, value.get('additional_mappings', []))
            seen_paths = set()
            for assignment in assignments:
                candidate = {key: assignment[key] for key in ('version', 'source', 'target', 'transformation') if key in assignment}
                normalized = normalise_draft_mapping(candidate, schema)
                if (not _mapping_values_preserved(candidate, normalized)
                        or normalized['target']['operation'] != definition['operation_name']):
                    raise MappingDraftError('New mutation content is incompatible with its operation.')
                path = (normalized['target']['argument'], normalized['target']['field'])
                if path in seen_paths:
                    raise MappingDraftError('New mutation assignments must have distinct input paths.')
                seen_paths.add(path)
                source = normalized['source']
                field = validate_draft_writeback_source(source['doctype'], source['field'], FrappePickerMetadata())
                if not draft_source_type_compatible(field.fieldtype, normalized['target']['graphql_type']):
                    raise MappingDraftError('New mutation source is incompatible with the target input.')

    def _assert_parentless_child_identities(self, definition, additions):
        f = self.frappe
        if additions:
            assert_draft_identity_index(f)
        for identity in sorted(item['id'] for item in additions):
            rows = f.db.sql('select definition_id from `tabHERO GraphQL Mapping` '
                            'where portable_definition_id=%s or definition_id=%s for update',
                            (identity, identity), as_dict=True)
            rows += lock_definition_drafts(f, [identity])
            if rows or identity == definition['definition_id']:
                raise DraftConflict('Mutation assignment identity is already in use.')
            drafts = lock_identity_drafts(f, [identity], 'child')
            for row in drafts:
                try:
                    content = json.loads(row.content_json)
                    children = content.get('additional_mappings', [])
                except (ValueError, TypeError, AttributeError):
                    raise DraftConflict('Existing mutation draft cannot be safely inspected.') from None
                if not isinstance(children, list):
                    raise DraftConflict('Existing mutation draft cannot be safely inspected.')
                if (any(isinstance(child, dict) and child.get('id') == identity for child in children)
                        and (row.owner_user, row.definition_id) != (f.session.user, definition['definition_id'])):
                    raise DraftConflict('Mutation assignment identity is already used by another draft.')

    def _assert_parentless_rule_identity(self, definition, identity, rule):
        from uuid import UUID
        from hero_integration.services.personal_mapping_content import identity_sets, same
        f = self.frappe
        rows = f.db.sql('select name from `tabHERO Frappe Mapping` where portable_definition_id=%s '
                        'or definition_id in (%s,%s) for update', (identity, identity, UUID(identity).hex), as_dict=True)
        if rows:
            raise DraftConflict('Portable field-rule identity is already used by an existing rule.')
        rows = lock_identity_drafts(f, [identity], 'rule')
        for row in rows:
            if (row.owner_user, row.definition_id) == (f.session.user, definition['definition_id']):
                continue
            try:
                content = json.loads(row.content_json)
                context = json.loads(row.new_definition_json or 'null')
                snapshots = content.get('field_rules', {})
                if not isinstance(snapshots, dict): raise ValueError
                identities, _ = identity_sets(content, _draft_identity_direction(f, row))
                matches = [item for item in identities if _uuid_equal(item, identity)]
            except (ValueError, TypeError, AttributeError):
                raise DraftConflict('Existing field-rule identity cannot be safely inspected.') from None
            if not matches:
                continue
            if (not isinstance(context, dict) or row.owner_user != f.session.user or len(matches) != 1
                    or any(context.get(key) != definition[key] for key in ('site_scope', 'source_instance', 'snapshot_name',
                                                                            'schema_fingerprint', 'direction'))
                    or not same(content.get('field_rules', {}).get(matches[0]), rule)):
                raise DraftConflict('Portable field-rule identity is already used by a different draft.')

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
        identity = self._definition.definition_id if self._definition else self._parentless_identity
        if key != draft_key(self.frappe.local.site, identity, self.frappe.session.user):
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
                            int(row.content_revision), row.base_active_mapping or '', int(row.base_active_revision or 0),
                            row.base_active_hash or '', row.base_schema_fingerprint,
                            json.loads(row.base_content_json) if row.base_content_json else None,
                            int(row.reconciliation_revision or 0),
                            json.loads(row.reconciliation_json) if row.reconciliation_json else None,
                            json.loads(getattr(row, 'new_definition_json', None)) if getattr(row, 'new_definition_json', None) else None)

    def save_draft(self, value: MappingDraft):
        self._scope()
        f = self.frappe
        identity = self._definition.definition_id if self._definition else self._parentless_identity
        expected_key = draft_key(f.local.site, identity, f.session.user)
        if (value.key != expected_key or value.owner != f.session.user or value.site_scope != f.local.site
                or value.definition_id != identity):
            raise PermissionError("Personal draft scope mismatch.")
        if (self._definition is None) != (value.new_definition is not None):
            raise DraftConflict('Draft baseline does not match its definition scope.')
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
            "new_definition_json": json.dumps(value.new_definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if value.new_definition is not None else "",
            "reconciliation_revision": value.reconciliation_revision,
            "reconciliation_json": json.dumps(value.reconciliation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if value.reconciliation is not None else "",
        })
        row.save()

    def append_revision(self, value: MappingDraftRevision):
        self._scope()
        if value.actor != self.frappe.session.user:
            raise PermissionError("Revision actor mismatch.")
        identity = self._definition.definition_id if self._definition else self._parentless_identity
        expected_key = draft_key(self.frappe.local.site, identity, self.frappe.session.user)
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
        if self._definition is None:
            prior = self.parentless(self._parentless_identity, self.frappe.session.user)
            if prior is None or prior.new_definition is None:
                raise DraftConflict('New-definition draft is unavailable.')
            value = validate_edits(prior.content, _object(content))
            self.validate_new_definition(prior.new_definition, value)
            return value
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
            if self._definition.direction == 'FRAPPE_TO_MUTATION':
                self._assert_parentless_child_identities({'definition_id': self._definition.definition_id},
                                                         value['additional_mappings'])
            else:
                self._assert_query_additional_identities(value['additional_mappings'])
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

    def _assert_query_additional_identities(self, additions):
        """New query assignments claim field-rule, rather than child-definition, IDs."""
        from uuid import UUID
        f = self.frappe
        for item in sorted(additions, key=lambda item: item['id']):
            identity = item['id']
            rows = f.db.sql('select name from `tabHERO Frappe Mapping` where portable_definition_id=%s '
                            'or definition_id in (%s,%s) for update',
                            (identity, identity, UUID(identity).hex), as_dict=True)
            if rows:
                raise DraftConflict('Query assignment identity is already used by an existing rule.')
            for row in lock_identity_drafts(f, [identity], 'rule'):
                if (row.owner_user, row.definition_id) != (f.session.user, self._definition.definition_id):
                    raise DraftConflict('Query assignment identity is already used by another draft.')

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
            if not f.db.has_column('HERO Frappe Mapping', 'portable_lineage_json'):
                raise MappingDraftError('Portable rule lineage schema is not installed; migrate the app before publication.')
            from hero_integration.services.mapping_rule_lineage import next_lineage
            try:
                lineage = next_lineage(original, new_identity, values)
            except ValueError as error:
                raise MappingDraftError('Enroll and verify the preceding rule portable identity before publication.') from error
            child = f.get_doc({'doctype': 'HERO Frappe Mapping', 'definition_id': new_identity,
                'portable_definition_id': new_identity, 'portable_lineage_json': lineage,
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
                planned = getattr(self, '_publication_target_locks', None)
                if planned is not None and rule.purpose != 'Record Name':
                    prefix = f'personal:{rule.personal_graphql_mapping}:' if rule.personal_graphql_mapping else ''
                    if (prefix + rule.operation, rule.target_doctype, rule.target_field) not in planned:
                        raise DraftConflict('Active rule target changed while publication locks were acquired.')
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
        for identity in sorted(content.get('mapping_definition_ids', [])):
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
