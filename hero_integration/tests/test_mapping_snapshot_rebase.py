"""Identical-schema graph rebinding never grants or loses execution authority."""
from copy import deepcopy
import json
import re
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import mapping_snapshot_rebase as rebase
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.graphql_contract import parse_mutation
from hero_integration.tests.test_config_mutation_contracts import fixtures, SCHEMA


class Doc(dict):
    def __getattr__(self, name): return self.get(name)
    def check_permission(self, *_): pass


class Database:
    def __init__(self, fake): self.fake, self.points = fake, {}
    def savepoint(self, name): self.points[name] = deepcopy(self.fake.rows)
    def rollback(self, *, save_point): self.fake.rows = deepcopy(self.points[save_point])
    def sql(self, query, params=(), **kwargs):
        if query.startswith('select name from '):
            doctype = re.search(r'`tab([^`]+)`', query).group(1)
            self.fake.membership_queries.append(query)
            iterator, filters = iter(params), {}
            for field, expression in re.findall(r'`(\w+)`(=%s| in \([^)]*\))', query):
                filters[field] = ([next(iterator) for _ in range(expression.count('%s'))]
                                  if expression.startswith(' in ') else [next(iterator)])
            return [Doc(name=name) for name, row in sorted(self.fake.rows[doctype].items())
                    if all(row.get(field) in allowed for field, allowed in filters.items())]
        if query.startswith('update '):
            doctype, field = re.search(r'`tab([^`]+)` set `([^`]+)`', query).groups()
            after, name, before, modified = params
            row = self.fake.rows[doctype][name]
            if row.get(field) == before and row.get('modified') == modified:
                row[field] = after
            return []
        if query.startswith('select '):
            field, doctype = re.search(r'select `([^`]+)` from `tab([^`]+)`', query).groups()
            return [Doc({field: self.fake.rows[doctype][params[0]][field]})]
        raise AssertionError(query)


class Fake:
    def __init__(self):
        self.rows = {name: {} for name in rebase.TABLES | {rebase.SNAPSHOT}}
        self.local, self.session = NS(site='test'), NS(user='Administrator')
        self.db = Database(self)
        self.membership_queries = []
        self.only_for, self.clear_document_cache = Mock(), Mock()
        for name in ('old', 'new'):
            self.add(rebase.SNAPSHOT, name, snapshot_json=json.dumps(SCHEMA), analysis_status='Validated',
                     schema_fingerprint=analyze_snapshot(SCHEMA).fingerprint, source_instance='source',
                     endpoint_fingerprint='e'*64, execution_contract_status='Compatible')
        contract, group = fixtures()
        group['content']['read_service'] = 'reader'
        fp = analyze_snapshot(SCHEMA).fingerprint
        common = {'schema_snapshot': 'old', 'schema_fingerprint': fp, 'status': 'Active'}
        self.add(rebase.MAPPING, 'query', **common, source_instance='source', direction='QUERY_TO_FRAPPE',
                 operation_kind='QUERY', definition_id='query-id', mapping_revision=3,
                 mapping_json=json.dumps({'target_operation': 'HeroTasks', 'identity_path': 'id',
                                         'mapping_definition_ids': ['rule-id']}))
        self.add(rebase.MAPPING, 'mutation', **common, source_instance='source', direction='FRAPPE_TO_MUTATION',
                 operation_kind='MUTATION', definition_id=group['definition_id'], portable_definition_id=group['definition_id'],
                 operation_name=group['operation_name'], mapping_revision=5, mapping_json=json.dumps(group['content']))
        self.add(rebase.RULE, 'rule', **common, definition_id='rule-id', mapping_revision=2,
                 source_path='title', target_doctype='Task', target_field='subject')
        self.add(rebase.SERVICE, 'reader', **{**common, 'status': 'Validated'}, read_mapping='query',
                 service_revision=2, execution_status='Approved', approved_service_revision=2,
                 approved_mapping_revision=3, query_document='query {tasks {id title}}')
        self.add(rebase.SERVICE, 'sender', **{**common, 'status': 'Validated'}, read_mapping='',
                 execution_status='Pending Approval', service_revision=4, query_document=contract['query'])
        self.add(rebase.CONTRACT, 'contract', **common, graphql_service='sender', contract_revision=6,
                 operation_name=contract['operation_name'], mapping_json=json.dumps(contract['mapping']),
                 contract_fingerprint=parse_mutation(contract['query']).fingerprint)

    def add(self, doctype, name, **values):
        self.rows[doctype][name] = Doc(doctype=doctype, name=name, modified='2026-11-04 00:00:00', **values)
        return self.rows[doctype][name]

    def get_doc(self, doctype, name, **kwargs): return deepcopy(self.rows[doctype][name])

    def get_all(self, doctype, *, filters, pluck=None, fields=None, **kwargs):
        def match(row):
            for key, value in filters.items():
                if isinstance(value, list) and value[0] == 'in':
                    if row.get(key) not in value[1]: return False
                elif row.get(key) != value: return False
            return True
        result = [deepcopy(row) for row in self.rows[doctype].values() if match(row)]
        return [row.get(pluck) for row in result] if pluck else result


class TestSnapshotRebase(TestCase):
    def setUp(self):
        self.fake = Fake()
        def reader(service, mapping, snapshot):
            if (service.schema_snapshot != snapshot.name or mapping.schema_snapshot != snapshot.name
                    or service.approved_service_revision != service.service_revision
                    or service.approved_mapping_revision != mapping.mapping_revision):
                raise ValueError('stale read approval or seal')
            return NS(target_operation='HeroTasks', identity_path='id')
        patches = [patch.object(rebase, 'frappe', self.fake), patch.object(rebase, '_audit'),
                   patch('hero_integration.services.writeback_source.capture_source', return_value=NS(fingerprint='source-proof')),
                   patch('hero_integration.services.mapping_draft_exchange._lock_context'),
                   patch('hero_integration.services.read_contract.from_documents', side_effect=reader),
                   patch('hero_integration.services.mapped_update_v2.validate_sources')]
        self.mocks = [value.start() for value in patches]
        for value in patches: self.addCleanup(value.stop)

    def configure_send_mapping(self):
        from hero_integration.tests.test_mapped_send import SCHEMA as send_schema, DOCUMENT, content
        value = content(); value['send_options']['read_service'] = 'reader'
        fingerprint = analyze_snapshot(send_schema).fingerprint
        for rows in self.fake.rows.values():
            for row in rows.values():
                if 'schema_fingerprint' in row: row['schema_fingerprint'] = fingerprint
        for snapshot in self.fake.rows[rebase.SNAPSHOT].values():
            snapshot['snapshot_json'] = json.dumps(send_schema)
        self.fake.rows[rebase.MAPPING]['mutation'].update(operation_name='update_contact', mapping_json=json.dumps(value))
        self.fake.rows[rebase.SERVICE]['sender']['query_document'] = DOCUMENT
        self.fake.rows[rebase.CONTRACT]['contract'].update(operation_name='update_contact', mapping_json='{}',
            contract_fingerprint=parse_mutation(DOCUMENT).fingerprint)
        self.mocks[4].side_effect = lambda *args: NS(target_operation='HeroCustomers', identity_path='id')
        validator = patch('hero_integration.services.frappe_mapping_catalog.validate_send_mapping_sources')
        self.addCleanup(validator.stop)
        return validator.start()

    def test_existing_send_mapping_rebases_without_rewriting_or_granting_approval(self):
        validator = self.configure_send_mapping()
        before = deepcopy(self.fake.rows)
        plan = rebase.prepare('new', 'source')
        rebase.prepare('new', 'source', apply=True, expected_fingerprint=plan['fingerprint'])
        self.assertTrue(validator.called)
        for binding in plan['bindings']:
            self.assertEqual(self.fake.rows[binding['doctype']][binding['name']],
                             {**before[binding['doctype']][binding['name']], 'schema_snapshot': 'new'})

    def test_send_rebase_still_checks_native_fields_and_readback_binding(self):
        validator = self.configure_send_mapping()
        before = deepcopy(self.fake.rows)
        validator.side_effect = ValueError('Native field is protected')
        with self.assertRaisesRegex(ValueError, 'protected'):
            rebase.prepare('new', 'source', apply=True)
        self.assertEqual(self.fake.rows, before)
        validator.side_effect = None
        self.mocks[4].side_effect = lambda *args: NS(target_operation='HeroTasks', identity_path='id')
        with self.assertRaisesRegex(ValueError, 'readback graph'):
            rebase.prepare('new', 'source', apply=True)
        self.assertEqual(self.fake.rows, before)

    def test_send_rebase_still_checks_saved_input_and_mutation_selection_schema(self):
        self.configure_send_mapping()
        mapping = self.fake.rows[rebase.MAPPING]['mutation']
        original = mapping['mapping_json']
        content = json.loads(original); content['target']['field'] = 'unknown_field'
        mapping['mapping_json'] = json.dumps(content)
        before = deepcopy(self.fake.rows)
        with self.assertRaises(ValueError): rebase.prepare('new', 'source')
        self.assertEqual(self.fake.rows, before)
        mapping['mapping_json'] = original
        sender = self.fake.rows[rebase.SERVICE]['sender']
        sender['query_document'] = sender['query_document'].replace('{ id }', '{ unknown_field }')
        self.fake.rows[rebase.CONTRACT]['contract']['contract_fingerprint'] = parse_mutation(sender['query_document']).fingerprint
        before = deepcopy(self.fake.rows)
        with self.assertRaisesRegex(ValueError, 'invalid mutation'):
            rebase.prepare('new', 'source')
        self.assertEqual(self.fake.rows, before)

    def test_send_rebase_rejects_different_snapshot_schema(self):
        self.configure_send_mapping()
        snapshot = self.fake.rows[rebase.SNAPSHOT]['new']
        raw = json.loads(snapshot.snapshot_json); raw['__schema']['description'] = 'changed'
        snapshot.update(snapshot_json=json.dumps(raw), schema_fingerprint=analyze_snapshot(raw).fingerprint)
        before = deepcopy(self.fake.rows)
        with self.assertRaisesRegex(ValueError, 'schemas differ'): rebase.prepare('new', 'source')
        self.assertEqual(self.fake.rows, before)

    def test_legacy_reviewed_field_mapping_keeps_its_existing_contract_format(self):
        self.configure_send_mapping()
        raw = json.loads(self.fake.rows[rebase.SNAPSHOT]['old'].snapshot_json)
        input_type = next(row for row in raw['__schema']['types'] if row['name'] == 'CustomerInput')
        field = deepcopy(next(row for row in input_type['inputFields'] if row['name'] == 'remarks'))
        field['name'] = 'first_name'; input_type['inputFields'].append(field)
        fingerprint = analyze_snapshot(raw).fingerprint
        for rows in self.fake.rows.values():
            for row in rows.values():
                if 'schema_fingerprint' in row: row['schema_fingerprint'] = fingerprint
        for snapshot in self.fake.rows[rebase.SNAPSHOT].values(): snapshot['snapshot_json'] = json.dumps(raw)
        self.fake.rows[rebase.MAPPING]['mutation']['mapping_json'] = json.dumps({
            'source': {'doctype': 'Customer', 'field': 'first_name'},
            'target': {'operation': 'update_contact', 'argument': 'contact', 'input_type': 'CustomerInput', 'field': 'first_name'},
            'transformation': 'Trim'})
        before = deepcopy(self.fake.rows)
        with patch('hero_integration.services.frappe_mapping_catalog.validate_draft_writeback_source') as validate_field:
            plan = rebase.prepare('new', 'source')
            rebase.prepare('new', 'source', apply=True, expected_fingerprint=plan['fingerprint'])
        self.assertTrue(validate_field.called)
        self.assertEqual(validate_field.call_args.args[:2], ('Customer', 'first_name'))
        self.assertEqual(self.fake.rows[rebase.MAPPING]['mutation'],
                         {**before[rebase.MAPPING]['mutation'], 'schema_snapshot': 'new'})

    def test_equal_snapshot_proof_recomputes_full_schema_and_endpoint(self):
        self.assertTrue(rebase.snapshots_equivalent('old', 'new', 'source'))
        self.fake.rows[rebase.SNAPSHOT]['new']['endpoint_fingerprint'] = 'f'*64
        self.assertFalse(rebase.snapshots_equivalent('old', 'new', 'source'))
        self.fake.rows[rebase.SNAPSHOT]['new']['endpoint_fingerprint'] = 'e'*64
        self.fake.rows[rebase.SNAPSHOT]['new']['snapshot_json'] = json.dumps({**SCHEMA, '__schema': {**SCHEMA['__schema'], 'description': 'changed'}})
        self.assertFalse(rebase.snapshots_equivalent('old', 'new', 'source'))

    def test_plan_and_apply_preserve_content_revisions_approvals_and_identities(self):
        before = deepcopy(self.fake.rows)
        plan = rebase.prepare('new', 'source')
        self.assertEqual(self.fake.rows, before)
        self.assertEqual(len(plan['bindings']), 6)
        result = rebase.prepare('new', 'source', apply=True, expected_fingerprint=plan['fingerprint'])
        self.assertTrue(result['applied'])
        for binding in plan['bindings']:
            old = before[binding['doctype']][binding['name']]
            current = self.fake.rows[binding['doctype']][binding['name']]
            self.assertEqual(current, {**old, 'schema_snapshot': 'new'})
        self.assertEqual(self.mocks[1].call_count, 6)
        self.assertFalse(rebase.prepare('new', 'source')['needs_rebase'])

    def test_changed_schema_blocks_before_any_reference_write(self):
        raw = deepcopy(SCHEMA);raw['__schema']['description'] = 'changed'
        self.fake.rows[rebase.SNAPSHOT]['new'].update(snapshot_json=json.dumps(raw), schema_fingerprint=analyze_snapshot(raw).fingerprint)
        before = deepcopy(self.fake.rows)
        with self.assertRaisesRegex(ValueError, 'schemas differ'):
            rebase.prepare('new', 'source')
        self.assertEqual(self.fake.rows, before)

    def test_current_membership_detects_mapping_missing_from_preliminary_rr_read(self):
        original = self.fake.get_all
        def stale_membership(doctype, **kwargs):
            rows = original(doctype, **kwargs)
            if doctype == rebase.MAPPING:
                concurrent = deepcopy(self.fake.rows[rebase.MAPPING]['mutation'])
                concurrent['name'] = 'concurrent-mapping'
                self.fake.rows[rebase.MAPPING]['concurrent-mapping'] = concurrent
            return rows
        with patch.object(self.fake, 'get_all', side_effect=stale_membership), \
             self.assertRaisesRegex(ValueError, 'bindings changed'):
            rebase.prepare('new', 'source')
        self.assertTrue(self.fake.membership_queries)
        self.assertTrue(all('for update' in sql for sql in self.fake.membership_queries))
        self.mocks[1].assert_not_called()

    def test_current_membership_also_checks_no_rebase_fast_path(self):
        for rows in self.fake.rows.values():
            for row in rows.values():
                if row.get('schema_snapshot') == 'old': row['schema_snapshot'] = 'new'
        original = self.fake.get_all
        def stale_membership(doctype, **kwargs):
            rows = original(doctype, **kwargs)
            if doctype == rebase.MAPPING:
                self.fake.rows[rebase.MAPPING]['mutation']['schema_snapshot'] = 'old'
            return rows
        with patch.object(self.fake, 'get_all', side_effect=stale_membership), \
             self.assertRaisesRegex(ValueError, 'bindings changed'):
            rebase.prepare('new', 'source')
        self.mocks[2].assert_not_called()

    def test_unselected_active_template_rebinds_without_semantic_changes(self):
        row = deepcopy(self.fake.rows[rebase.RULE]['rule'])
        row.update(name='template', definition_id='template-id')
        self.fake.rows[rebase.RULE]['template'] = row
        before = deepcopy(row)
        plan = rebase.prepare('new', 'source')
        rebase.prepare('new', 'source', apply=True, expected_fingerprint=plan['fingerprint'])
        self.assertEqual(self.fake.rows[rebase.RULE]['template'], {**before, 'schema_snapshot': 'new'})

    def test_private_template_baseline_is_protected_even_for_unmoved_parent(self):
        self.fake.add(rebase.DRAFT, 'private-template', site_scope='test', state='OPEN', owner_user='other',
                      base_active_mapping='different-parent', content_json=json.dumps({
                          'field_rule_bases': {'template-id': {'schema_snapshot': 'old'}}}))
        with self.assertRaisesRegex(ValueError, 'Open Query drafts'):
            rebase.prepare('new', 'source')

    def test_membership_filters_are_closed_and_values_remain_parameters(self):
        with self.assertRaises(ValueError): rebase._documents('Other DocType', {'status': 'Active'})
        with self.assertRaises(ValueError): rebase._documents(rebase.MAPPING, {'untrusted_column': 'Active'})
        with self.assertRaises(ValueError): rebase._documents(rebase.MAPPING, {'status': ['like', ['Active']]})
        with self.assertRaises(ValueError): rebase._documents(rebase.MAPPING, {'status': ['in', []]})
        source = "source' OR 1=1 --"
        with patch.object(self.fake.db, 'sql', return_value=[]) as query:
            self.assertEqual(rebase._documents(rebase.MAPPING, {'source_instance': source, 'status': 'Active'}), [])
        self.assertNotIn(source, query.call_args.args[0])
        self.assertIn(source, query.call_args.args[1])

    def test_no_rebase_does_not_add_connection_or_contract_requirements(self):
        for rows in self.fake.rows.values():
            for row in rows.values():
                if row.get('schema_snapshot') == 'old': row['schema_snapshot'] = 'new'
        self.fake.rows[rebase.SERVICE]['reader']['approved_mapping_revision'] = 0
        self.mocks[2].side_effect = AssertionError('No connection proof needed for an unchanged context.')
        with rebase.preview_target('new', 'source') as plan:
            self.assertFalse(plan['needs_rebase'])
        self.mocks[2].assert_not_called()
        self.mocks[4].assert_not_called()

    def test_source_and_endpoint_mismatch_never_rebind(self):
        for key, value in (('source_instance', 'other'), ('endpoint_fingerprint', 'f'*64)):
            with self.subTest(key=key):
                original = self.fake.rows[rebase.SNAPSHOT]['old'][key]
                self.fake.rows[rebase.SNAPSHOT]['old'][key] = value
                before = deepcopy(self.fake.rows)
                with self.assertRaises(ValueError): rebase.prepare('new', 'source')
                self.assertEqual(self.fake.rows, before)
                self.fake.rows[rebase.SNAPSHOT]['old'][key] = original

    def test_cas_failure_restores_earlier_reference_moves(self):
        plan = rebase.prepare('new', 'source');before = deepcopy(self.fake.rows)
        original = self.fake.db.sql
        def sql(query, params=(), **kwargs):
            if query.startswith('update `tabHERO Frappe Mapping`'): return []
            return original(query, params, **kwargs)
        with patch.object(self.fake.db, 'sql', side_effect=sql), self.assertRaisesRegex(ValueError, 'bindings changed'):
            rebase.prepare('new', 'source', apply=True, expected_fingerprint=plan['fingerprint'])
        self.assertEqual(self.fake.rows, before)

    def test_source_change_after_writes_rolls_back_the_group(self):
        plan = rebase.prepare('new', 'source');before = deepcopy(self.fake.rows)
        self.mocks[2].side_effect = [NS(fingerprint='source-proof'), NS(fingerprint='changed')]
        with self.assertRaisesRegex(ValueError, 'source changed during'):
            rebase.prepare('new', 'source', apply=True, expected_fingerprint=plan['fingerprint'])
        self.assertEqual(self.fake.rows, before)

    def test_plan_fingerprint_rejects_intervening_revision_or_source_change(self):
        plan = rebase.prepare('new', 'source')
        self.fake.rows[rebase.CONTRACT]['contract']['contract_revision'] += 1
        before = deepcopy(self.fake.rows)
        with self.assertRaisesRegex(ValueError, 'changed since preview'):
            rebase.prepare('new', 'source', apply=True, expected_fingerprint=plan['fingerprint'])
        self.assertEqual(self.fake.rows, before)

    def test_existing_stale_approval_never_becomes_valid(self):
        self.fake.rows[rebase.SERVICE]['reader']['approved_mapping_revision'] = 1
        before = deepcopy(self.fake.rows)
        with self.assertRaisesRegex(ValueError, 'stale read approval'):
            rebase.prepare('new', 'source')
        self.assertEqual(self.fake.rows, before)

    def test_post_rebind_validation_failure_rolls_back_all_moves(self):
        plan = rebase.prepare('new', 'source');before = deepcopy(self.fake.rows)
        original = rebase._validate_group
        def validate(*args):
            if 'old' not in args[-1]: raise ValueError('new seal mismatch')
            return original(*args)
        with patch.object(rebase, '_validate_group', side_effect=validate), self.assertRaisesRegex(ValueError, 'new seal mismatch'):
            rebase.prepare('new', 'source', apply=True, expected_fingerprint=plan['fingerprint'])
        self.assertEqual(self.fake.rows, before)
        self.assertTrue(self.fake.clear_document_cache.called)

    def test_preview_target_always_restores_bindings_and_caches(self):
        before = deepcopy(self.fake.rows)
        with rebase.preview_target('new', 'source') as plan:
            self.assertTrue(plan['needs_rebase'])
            self.assertEqual(self.fake.rows[rebase.MAPPING]['mutation']['schema_snapshot'], 'new')
        self.assertEqual(self.fake.rows, before)
        with self.assertRaisesRegex(RuntimeError, 'preview fail'):
            with rebase.preview_target('new', 'source'):
                raise RuntimeError('preview fail')
        self.assertEqual(self.fake.rows, before)

    def test_private_query_draft_is_blocked_without_content_or_history_changes(self):
        self.fake.add(rebase.DRAFT, 'private', site_scope='test', state='OPEN', owner_user='other',
                      base_active_mapping='query', content_json=json.dumps({'field_rule_bases': {'rule-id': {'schema_snapshot': 'old'}}}))
        before = deepcopy(self.fake.rows)
        with self.assertRaisesRegex(ValueError, 'Open Query drafts'):
            rebase.prepare('new', 'source')
        self.assertEqual(self.fake.rows, before)

    def test_parentless_mutation_context_moves_without_rewriting_its_content(self):
        self.fake.add(rebase.DRAFT, 'own', site_scope='test', state='OPEN', owner_user='Administrator',
                      new_definition_json=json.dumps({'source_instance': 'source', 'snapshot_name': 'old',
                          'schema_fingerprint': analyze_snapshot(SCHEMA).fingerprint, 'direction': 'FRAPPE_TO_MUTATION'}),
                      content_json=json.dumps({'usage': 'MAPPED_UPDATE_V2'}), content_hash='unchanged', content_revision=4)
        before = deepcopy(self.fake.rows[rebase.DRAFT]['own'])
        plan = rebase.prepare('new', 'source')
        rebase.prepare('new', 'source', apply=True, expected_fingerprint=plan['fingerprint'])
        current = self.fake.rows[rebase.DRAFT]['own']
        self.assertEqual(current['content_json'], before['content_json'])
        self.assertEqual(current['content_revision'], before['content_revision'])
        self.assertEqual(json.loads(current['new_definition_json'])['snapshot_name'], 'new')

    def test_foreign_parentless_draft_is_preserved_and_blocks_transition(self):
        self.fake.add(rebase.DRAFT, 'private', site_scope='test', state='OPEN', owner_user='other',
                      new_definition_json=json.dumps({'source_instance': 'source', 'snapshot_name': 'old', 'direction': 'FRAPPE_TO_MUTATION'}),
                      content_json=json.dumps({'usage': 'MAPPED_UPDATE_V2'}))
        with self.assertRaisesRegex(ValueError, 'by its owner'):
            rebase.prepare('new', 'source')
