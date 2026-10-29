"""Offline copy-on-write proof using the production publication projection."""
from copy import deepcopy
from contextlib import contextmanager
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from hero_integration import api  # Load Frappe-decorated API before replacing the DB boundary.
from hero_integration.patches.v0_1 import normalize_mapping_priorities
from hero_integration.services import mapping_runtime, mapping_suggestions, direct_mapping
from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import ActiveMapping, MappingDraft, draft_key, _hash
from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping
from hero_integration.services.personal_mapping_content import RULE_FIELDS
from hero_integration.services.mapping_rule_lineage import parse_lineage, next_lineage
from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping


class Rule:
    def __init__(self, fake, values): self.fake, self.values = fake, dict(values)
    def __getattr__(self, key):
        try: return self.values[key]
        except KeyError: raise AttributeError(key) from None
    def get(self, key): return self.values.get(key)
    def check_permission(self, _permission): pass
    def insert(self):
        self.values.setdefault('name', 'DOC-' + self.values['definition_id'])
        self.values.setdefault('schema_fingerprint', 'fingerprint')
        self.fake.rules[self.values['definition_id']] = dict(self.values)


class Fake:
    def __init__(self, rows):
        self.rules = {row['definition_id']:dict(row) for row in rows}
        self.mappings = {}
        self.db = SimpleNamespace(sql=self.sql, has_column=lambda *_: True, get_value=self.get_value)
    def get_value(self, _doctype, filters, _fields, **_kwargs):
        value = self.rules.get(filters['definition_id'])
        return {key:value.get(key) for key in ('portable_definition_id', 'portable_lineage_json', 'schema_manifest', 'schema_dependencies')} if value else None
    def sql(self, query, args, **kwargs):
        if 'tabHERO Frappe Mapping' in query:
            value = self.rules.get(args[0]); return [dict(value)] if value else []
        raise AssertionError(query)
    def get_doc(self, *args, **kwargs):
        if isinstance(args[0], dict): return Rule(self, args[0])
        if args[0] == 'HERO GraphQL Mapping':
            return SimpleNamespace(status='Active', direction='QUERY_TO_FRAPPE', schema_snapshot='snapshot', schema_fingerprint='fingerprint',
                                   mapping_json=json.dumps(self.mappings[args[1]]))
        raise AssertionError(args)
    def get_all(self, doctype, **kwargs):
        if doctype != 'HERO Frappe Mapping': raise AssertionError(doctype)
        fields = kwargs.get('fields') or []
        return [{key:value.get(key) for key in fields} for value in self.rules.values() if value['status'] == 'Active']


class TestSharedRulePublication(unittest.TestCase):
    def test_scoped_portable_root_keeps_reserved_customer_semantics_closed(self):
        from hero_integration.services.mapping_rule_lineage import customer_legacy_literals
        identity = 'a16b15fa-7ca8-4722-b63c-ac60cc46629a'
        mapping = SimpleNamespace(name='mapping-a', schema_snapshot='snapshot', schema_fingerprint='fingerprint')
        root = {'definition_id':identity, 'portable_definition_id':identity, 'portable_lineage_json':'',
                'personal_graphql_mapping':'mapping-a', 'status':'Active', 'operation':'HeroCustomers',
                'schema_snapshot':'snapshot', 'schema_fingerprint':'fingerprint', 'source_path':'email'}
        lookup = lambda value: [root] if value == identity else []
        self.assertEqual(customer_legacy_literals([identity], mapping, lookup, lookup), set())
        root['source_path'] = 'company_legal_form'
        with self.assertRaisesRegex(ValueError, 'legacy rule identity'):
            customer_legacy_literals([identity], mapping, lookup, lookup)
        root['source_path'], root['portable_definition_id'] = 'email', ''
        with self.assertRaisesRegex(ValueError, 'lineage is missing'):
            customer_legacy_literals([identity], mapping, lookup, lookup)

    def test_rule_controller_rejects_predecessor_from_stale_schema(self):
        from hero_integration.services import frappe_mapping_draft_storage
        revision = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        root = 'a16b15fa-7ca8-4722-b63c-ac60cc46629a'
        lineage = json.dumps({'version':1, 'logical_id':root, 'revision_id':revision,
                              'predecessor_id':root, 'content_hash':'a' * 64})
        class Doc:
            personal_graphql_mapping, schema_snapshot, operation = 'mapping-a', 'snapshot', 'HeroCustomers'
            definition_id, status, purpose = revision, 'Active', 'Field'
            def is_new(self): return True
            def get(self, key): return {'portable_definition_id':revision, 'portable_lineage_json':lineage}.get(key)
        original = SimpleNamespace(status='Active', schema_snapshot='snapshot', schema_fingerprint='stale',
            operation='HeroCustomers', personal_graphql_mapping='', check_permission=lambda *_: None)
        snapshot = SimpleNamespace(analysis_status='Validated', snapshot_json='{}', schema_fingerprint='current')
        def get_doc(doctype, *_args, **_kwargs): return snapshot if doctype == 'HERO GraphQL Schema Snapshot' else original
        def fail(message, *_args): raise ValueError(message)
        fake = SimpleNamespace(only_for=lambda *_: None, get_doc=get_doc, throw=fail, PermissionError=PermissionError,
            get_all=lambda *_args, **_kwargs: [SimpleNamespace(name='previous')])
        with patch.object(hero_frappe_mapping, 'frappe', fake), \
             patch.object(hero_frappe_mapping, 'analyze_snapshot', return_value=SimpleNamespace(fingerprint='current')), \
             patch.object(frappe_mapping_draft_storage, 'personal_publication_parent', return_value='mapping-a'):
            with self.assertRaisesRegex(ValueError, 'authorized preceding revision'):
                hero_frappe_mapping.HEROFrappeMapping.validate(Doc())

    def test_lineage_rejects_unenrolled_predecessor_and_invalid_payload(self):
        row = {'portable_definition_id':'', 'operation':'HeroCustomers', 'schema_fingerprint':'fingerprint',
               **{key:None for key in RULE_FIELDS}}
        with self.assertRaisesRegex(ValueError, 'enrolled canonical UUID'):
            next_lineage(Rule(None, row), '09a2bc52-4b21-46b7-9a11-3a62bf5a6348',
                         {key:row[key] for key in RULE_FIELDS})
        with self.assertRaisesRegex(ValueError, 'invalid'):
            parse_lineage('{"version":1,"version":1}')
        from hero_integration.services.mapping_rule_lineage import semantic_fingerprint
        fields = {key:row[key] for key in RULE_FIELDS}
        self.assertNotEqual(semantic_fingerprint('HeroCustomers', 'fingerprint', fields, schema_dependencies='["one"]'),
                            semantic_fingerprint('HeroCustomers', 'fingerprint', fields, schema_dependencies='["two"]'))
        with self.assertRaisesRegex(ValueError, 'schema provenance'):
            semantic_fingerprint('HeroCustomers', 'fingerprint', fields, schema_manifest='{"x":1,"x":2}')

    def test_query_duplicate_binding_is_rejected_by_save_api(self):
        def fail(message): raise ValueError(message)
        fake = SimpleNamespace(only_for=lambda _role: None, throw=fail)
        payload = '{"mapping_definition_ids":["one"],"mapping_definition_ids":["two"]}'
        with patch.object(api, 'frappe', fake):
            with self.assertRaisesRegex(ValueError, 'must be an object'):
                api.graphql_mapping_save('snapshot', 'QUERY', 'customers', 'QUERY_TO_FRAPPE', payload)

    def test_historical_priority_patch_rejects_active_rule_before_any_write(self):
        writes = []
        def fail(message): raise ValueError(message)
        fake = SimpleNamespace(get_all=lambda *_args, **_kwargs: [SimpleNamespace(name='active', priority=100)],
                               db=SimpleNamespace(set_value=lambda *args, **kwargs: writes.append(args)), throw=fail)
        with patch.object(normalize_mapping_priorities, 'frappe', fake):
            with self.assertRaisesRegex(ValueError, 'revision-safe reconciliation'):
                normalize_mapping_priorities.execute()
        self.assertEqual(writes, [])

    def test_saved_semantic_noop_does_not_create_active_revision(self):
        site, actor, identity = 'fixture', 'owner', 'definition'
        content = {'target_operation':'HeroCustomers', 'mapping_definition_ids':['rule']}
        active = ActiveMapping(site, identity, 'mapping-a', 2, 'fingerprint', content)
        draft = MappingDraft(draft_key(site, identity, actor), site, identity, actor, 1, 'OPEN', content,
            _hash(content), 1, active.name, active.revision, active.content_hash, active.schema_fingerprint,
            base_content=content)
        row = SimpleNamespace(modified_by=actor, diagnostics_json='{}', warning_fingerprint='',
                              check_permission=lambda *_: None)
        def sql(query, *_args, **_kwargs):
            if query.startswith('select name, operation_name'):
                return [SimpleNamespace(name='mapping-a', operation_name='customers', source_instance='source',
                                        schema_snapshot='snapshot', direction='QUERY_TO_FRAPPE')]
            if query.startswith('select content_json'):
                return [SimpleNamespace(content_json=json.dumps(content), base_content_json=json.dumps(content))]
            if 'tabHERO Mapping Revision' in query:
                return [SimpleNamespace(content_hash=draft.content_hash)]
            raise AssertionError(query)
        fake = SimpleNamespace(local=SimpleNamespace(site=site), session=SimpleNamespace(user=actor),
            db=SimpleNamespace(sql=sql), get_roles=lambda: ['System Manager'], get_doc=lambda *_args, **_kwargs: row)
        storage = FrappeMappingDraftStorage(fake)
        parent = SimpleNamespace(name='mapping-a', operation_name='customers', source_instance='source',
            schema_snapshot='snapshot', direction='QUERY_TO_FRAPPE', check_permission=lambda *_: None)
        @contextmanager
        def transaction(*_args):
            storage._definition = parent
            try: yield
            finally: storage._definition = None
        storage.transaction = transaction
        with patch.object(hero_graphql_mapping, 'lock_exchange_context'), \
             patch.object(storage, 'lock_draft_identities'), patch.object(storage, 'active', return_value=active), \
             patch.object(storage, 'draft', return_value=draft), patch.object(storage, 'canonicalize', return_value=content), \
             patch.object(storage, '_publication_content', return_value=(content, [])), \
             patch.object(storage, 'append_revision') as append, \
             patch.object(api, '_validated_snapshot', return_value=(SimpleNamespace(schema_fingerprint='fingerprint'), {})):
            result = storage.publish(identity, actor, 1, 1, 2, 0, confirmed=True)
        self.assertEqual(result, {'name':'mapping-a', 'revision':2, 'unchanged':True})
        append.assert_not_called()

    def test_copy_on_write_changes_only_selected_mapping_rule(self):
        ids = ['identity', 'name', 'kind', 'lifecycle', 'field']
        purposes = ['Source Identity', 'Record Name', 'Customer Type', 'Lifecycle Flag', 'Field']
        rows = [dict(name='DOC-'+identity, definition_id=identity, operation='HeroCustomers', status='Active',
                     schema_snapshot='snapshot', schema_fingerprint='fingerprint', personal_graphql_mapping='',
                     source_path=identity, source_type='string|null', target_doctype='Customer', target_field='field_'+identity,
                     purpose=purpose, transformation='Identity', relation='', priority=5, mapping_revision=1,
                     schema_dependencies='[]', schema_manifest='',
                     portable_definition_id=str(uuid5(NAMESPACE_URL, 'fixture-rule:' + identity))) for identity, purpose in zip(ids, purposes)]
        fake = Fake(rows)
        parent_config = {'target_operation':'HeroCustomers', 'records_path':'customers', 'identity_path':'id',
                         'mapping_definition_ids':list(ids)}
        parent = SimpleNamespace(name='mapping-a', direction='QUERY_TO_FRAPPE', schema_snapshot='snapshot',
            schema_fingerprint='fingerprint', operation_name='customers', mapping_json=json.dumps(parent_config))
        storage = FrappeMappingDraftStorage(fake); storage._definition = parent
        before = deepcopy(fake.rules)
        rules = {row['definition_id']:{key:row.get(key) for key in RULE_FIELDS} for row in rows}
        bases = {row['definition_id']:{'name':row['name'], 'revision':1, 'operation':row['operation'],
                 'schema_snapshot':'snapshot', 'schema_fingerprint':'fingerprint'} for row in rows}
        baseline = {**parent_config, 'field_rules':deepcopy(rules), 'field_rule_bases':deepcopy(bases)}
        rules['field']['target_field'] = 'new_field'
        draft = SimpleNamespace(key='draft-a', generation=1, content_revision=1, base_content=baseline,
                                reconciliation=None, content={**parent_config, 'field_rules':rules, 'field_rule_bases':bases})
        metadata = SimpleNamespace(field=lambda *_args: SimpleNamespace(is_custom_field=True))
        with patch.object(mapping_suggestions, 'source_fields', return_value=[]), \
             patch.object(direct_mapping, 'validate_spec'), \
             patch.object(hero_frappe_mapping, '_FrappeMetadata', return_value=metadata), \
             patch.object(storage, '_validate_publication_read_contract'), \
             patch.dict(sys.modules, {'frappe':fake}):
            storage._publication_target_locks = set()
            with self.assertRaisesRegex(ValueError, 'target changed while publication locks'):
                storage._publication_content(draft, {})
            del storage._publication_target_locks
            with patch.object(fake.db, 'has_column', return_value=False):
                with self.assertRaisesRegex(ValueError, 'lineage schema is not installed'):
                    storage._publication_content(draft, {})
            fake.rules['field']['portable_definition_id'] = ''
            with self.assertRaisesRegex(ValueError, 'Enroll and verify'):
                storage._publication_content(draft, {})
            fake.rules['field']['portable_definition_id'] = before['field']['portable_definition_id']
            self.assertEqual(fake.rules, before)
            projected, created = storage._publication_content(draft, {})
            new_id = projected['mapping_definition_ids'][-1]
            self.assertEqual(fake.rules['field'], before['field'])
            self.assertNotEqual(new_id, 'field')
            self.assertEqual(fake.rules[new_id]['personal_graphql_mapping'], 'mapping-a')
            self.assertEqual(fake.rules[new_id]['target_field'], 'new_field')
            lineage = parse_lineage(fake.rules[new_id]['portable_lineage_json'])
            self.assertEqual(lineage['logical_id'], before['field']['portable_definition_id'])
            self.assertEqual(lineage['predecessor_id'], before['field']['portable_definition_id'])
            self.assertEqual(lineage['revision_id'], fake.rules[new_id]['portable_definition_id'])
            self.assertNotEqual(lineage['logical_id'], lineage['revision_id'])
            successor = str(uuid5(NAMESPACE_URL, 'fixture-successor'))
            after = dict(fake.rules[new_id]); after['target_field'] = 'third_field'
            successor_lineage = parse_lineage(next_lineage(Rule(fake, fake.rules[new_id]), successor,
                                                          {key:after.get(key) for key in RULE_FIELDS}))
            self.assertEqual(successor_lineage['logical_id'], lineage['logical_id'])
            self.assertEqual(successor_lineage['predecessor_id'], lineage['revision_id'])
            self.assertEqual(len(created), 1)
            fake.mappings = {'mapping-a': projected, 'mapping-b': parent_config}
            plan_a = mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=tuple(projected['mapping_definition_ids']),
                                                         mapping_name='mapping-a')
            plan_b = mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=tuple(ids), mapping_name='mapping-b')
            self.assertEqual(next(r.target_field for r in plan_a.definitions if r.definition_id == new_id), 'new_field')
            self.assertEqual(next(r.target_field for r in plan_b.definitions if r.definition_id == 'field'), 'field_field')
            with self.assertRaises(mapping_runtime.MappingPlanError):
                mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=(new_id,), mapping_name='mapping-b')
            with self.assertRaisesRegex(mapping_runtime.MappingPlanError, 'binding'):
                mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=tuple(ids), mapping_name='mapping-a')
            removed_content = deepcopy(draft.content)
            removed_content['mapping_definition_ids'].remove('field')
            removed_draft = SimpleNamespace(key='draft-a', generation=1, content_revision=2, base_content=baseline,
                                            reconciliation=None, content=removed_content)
            retired_projection, retired_created = storage._publication_content(removed_draft, {})
            self.assertNotIn('field', retired_projection['mapping_definition_ids'])
            self.assertEqual(retired_created, [])
            self.assertEqual(fake.rules['field'], before['field'])
            self.assertIn('field', parent_config['mapping_definition_ids'])

            # Run the actual publication service with only persistence and
            # read-approval boundaries replaced by this synthetic fixture.
            fake.rules = deepcopy(before)
            fake.local, fake.session = SimpleNamespace(site='fixture'), SimpleNamespace(user='owner')
            fake.get_roles = lambda: ['System Manager']
            parent.definition_id, parent.source_instance, parent.mapping_revision, parent.status = 'def-a', 'source', 1, 'Active'
            parent.check_permission = lambda *_: None
            fake.mappings = {'mapping-a': deepcopy(parent_config), 'mapping-b': deepcopy(parent_config)}
            parent.save = lambda: fake.mappings.__setitem__('mapping-a', json.loads(parent.mapping_json))
            initial = ActiveMapping('fixture', 'def-a', 'mapping-a', 1, 'fingerprint', deepcopy(parent_config))
            saved = MappingDraft(draft_key('fixture','def-a','owner'), 'fixture', 'def-a', 'owner', 1, 'OPEN',
                draft.content, _hash(draft.content), 1, initial.name, 1, initial.content_hash, 'fingerprint',
                base_content=baseline)
            draft_row = SimpleNamespace(modified_by='owner', diagnostics_json='{}', warning_fingerprint='',
                check_permission=lambda *_: None, save=lambda: None)
            old_sql, old_get_doc = fake.sql, fake.get_doc
            def sql(query, args=(), **kwargs):
                if query.startswith('select name, operation_name'):
                    return [SimpleNamespace(name='mapping-a', operation_name='customers', source_instance='source',
                                            schema_snapshot='snapshot', direction='QUERY_TO_FRAPPE')]
                if query.startswith('select content_json'):
                    return [SimpleNamespace(content_json=json.dumps(saved.content), base_content_json=json.dumps(baseline))]
                if 'tabHERO Mapping Revision' in query:
                    return [SimpleNamespace(content_hash=saved.content_hash)]
                return old_sql(query, args, **kwargs)
            fake.db.sql = sql
            fake.get_doc = lambda *args, **kwargs: draft_row if args[0] == 'HERO Mapping Draft' else old_get_doc(*args, **kwargs)
            @contextmanager
            def transaction(*_args):
                storage._definition = parent
                try: yield
                finally: storage._definition = None
            storage.transaction = transaction
            def active(_identity):
                return ActiveMapping('fixture','def-a','mapping-a', parent.mapping_revision, 'fingerprint',
                                     json.loads(parent.mapping_json))
            activations = []
            with patch.object(hero_graphql_mapping, 'lock_exchange_context'), \
                 patch.object(hero_frappe_mapping, '_lock_active_target'), \
                 patch.object(storage, 'lock_draft_identities'), patch.object(storage, 'active', side_effect=active), \
                 patch.object(storage, 'draft', return_value=saved), patch.object(storage, 'canonicalize', return_value=saved.content), \
                 patch.object(storage, 'append_revision', side_effect=activations.append), \
                 patch.object(storage, '_publication_objects', return_value=[]), \
                 patch.object(api, '_validated_snapshot', return_value=(SimpleNamespace(schema_fingerprint='fingerprint'), {})):
                result = storage.publish('def-a', 'owner', 1, 1, 1, 0, confirmed=True)
                self.assertEqual(result, {'name':'mapping-a', 'revision':2, 'unchanged':False})
                repeated = storage.publish('def-a', 'owner', 1, 1, 1, 0, confirmed=True)
                self.assertTrue(repeated['unchanged'])
            self.assertEqual(len(activations), 1)
            published_id = fake.mappings['mapping-a']['mapping_definition_ids'][-1]
            self.assertNotEqual(published_id, 'field')
            self.assertEqual(fake.mappings['mapping-b']['mapping_definition_ids'], ids)
            self.assertEqual(fake.rules['field'], before['field'])
            self.assertEqual(fake.rules[published_id]['target_field'], 'new_field')
            a_plan = mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=tuple(fake.mappings['mapping-a']['mapping_definition_ids']), mapping_name='mapping-a')
            b_plan = mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=tuple(ids), mapping_name='mapping-b')
            self.assertNotEqual(a_plan.mapping_version, b_plan.mapping_version)
