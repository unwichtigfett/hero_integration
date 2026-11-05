"""Replacement preserves native snapshot scope and explicit mutation readback identities."""
from contextlib import ExitStack
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import config_exchange as exchange


class Row(dict):
    def __getattr__(self, key):
        return self.get(key)


def rule(identity):
    return Row(definition_id=identity, source_path='name', source_type='String',
               target_doctype='Customer', target_field='customer_name', purpose='Field',
               transformation='Trim', relation=None, priority=10)


def package():
    return {'read_services': [{'target_operation': 'HeroCustomers',
                              'mapping': {'mapping_definition_ids': ['old']}}],
            'mutation_mappings': [{'content': {'usage': 'MAPPED_UPDATE_V2', 'area': 'HeroCustomers',
                                              'assignments': [{'query_rule_id': 'old'}, {'query_rule_id': ''}]}}]}


class TestMutationRuleReplacement(unittest.TestCase):
    def setUp(self):
        self.payload = package()
        self.mapping = Row(name='published', schema_snapshot='snapshot', mapping_json=json.dumps({
            'mapping_definition_ids': ['new']}))
        self.active = [rule('new')]
        self.fake = SimpleNamespace(get_all=Mock(side_effect=lambda *a, **k: self.active))
        context = patch.object(exchange, 'frappe', self.fake)
        context.start()
        self.addCleanup(context.stop)

    def remap(self):
        return exchange._remap_mutation_rules(self.payload, {'old': rule('old')},
                                             {'HeroCustomers': self.mapping})

    def test_explicit_identity_follows_actual_published_selection_without_mutating_file(self):
        original = deepcopy(self.payload)
        result = self.remap()
        self.assertEqual(result['mutation_mappings'][0]['content']['assignments'],
                         [{'query_rule_id': 'new'}, {'query_rule_id': ''}])
        self.assertEqual(self.payload, original)
        filters = self.fake.get_all.call_args.kwargs['filters']
        self.assertEqual(filters, {'definition_id': ['in', ['new']], 'status': 'Active',
                                  'schema_snapshot': 'snapshot', 'operation': 'HeroCustomers'})

    def test_reused_equivalent_mapping_keeps_its_existing_rule_identity(self):
        self.mapping['mapping_json'] = json.dumps({'mapping_definition_ids': ['already-approved']})
        self.active = [rule('already-approved')]
        result = self.remap()
        self.assertEqual(result['mutation_mappings'][0]['content']['assignments'][0]['query_rule_id'],
                         'already-approved')

    def test_rule_selected_only_in_another_area_is_rejected(self):
        self.payload['read_services'].append({'target_operation': 'HeroTasks',
                                             'mapping': {'mapping_definition_ids': ['task-rule']}})
        self.payload['mutation_mappings'][0]['content']['assignments'][0]['query_rule_id'] = 'task-rule'
        with self.assertRaises(exchange.ConfigExchangeError):
            self.remap()
        self.fake.get_all.assert_not_called()

    def test_missing_ambiguous_or_changed_published_rule_is_rejected(self):
        for active, selected in [([], ['new']), ([rule('a'), rule('b')], ['a', 'b']),
                                 ([Row(**{**rule('new'), 'source_path': 'other'})], ['new'])]:
            with self.subTest(selected=selected, active=active):
                self.active = active
                self.mapping['mapping_json'] = json.dumps({'mapping_definition_ids': selected})
                with self.assertRaises(exchange.ConfigExchangeError):
                    self.remap()

    def test_transform_options_are_part_of_reference_equivalence(self):
        self.payload['read_services'][0]['mapping']['query_adapter'] = {
            'transform_options': {'old': {'map': {'A': 'B'}}}}
        self.mapping['mapping_json'] = json.dumps({'mapping_definition_ids': ['new'], 'query_adapter': {
            'transform_options': {'new': {'map': {'A': 'C'}}}}})
        with self.assertRaises(exchange.ConfigExchangeError):
            self.remap()

    def test_no_explicit_rules_needs_no_identity_query(self):
        self.payload['mutation_mappings'][0]['content']['assignments'] = [{'query_rule_id': ''}]
        self.assertIs(self.remap(), self.payload)
        self.fake.get_all.assert_not_called()


class TestExistingMutationRuleReplacement(unittest.TestCase):
    def setUp(self):
        self.content = {'usage': 'MAPPED_UPDATE_V2', 'area': 'HeroCustomers', 'read_service': 'old-reader',
                        'assignments': [{'query_rule_id': 'LOCAL-L'}, {'query_rule_id': ''}]}
        self.parent = Row(source_instance='source-a', schema_snapshot='old-snapshot', mapping_json=json.dumps(self.content))
        self.previous = Row(source_instance='source-a', direction='QUERY_TO_FRAPPE', schema_snapshot='old-snapshot',
            mapping_json=json.dumps({'target_operation': 'HeroCustomers', 'mapping_definition_ids': ['LOCAL-L']}))
        self.current = Row(name='new-map', schema_snapshot='new-snapshot',
            mapping_json=json.dumps({'mapping_definition_ids': ['LOCAL-N']}))
        self.old_rule, self.new_rule = rule('LOCAL-L'), rule('LOCAL-N')
        self.old_rule.update(status='Disabled', schema_snapshot='old-snapshot')
        self.new_rule.update(status='Active', schema_snapshot='new-snapshot')
        docs = {'old-reader': Row(read_mapping='old-map', schema_snapshot='old-snapshot'), 'old-map': self.previous}
        self.fake = SimpleNamespace(get_doc=lambda dt,name:docs[name], get_all=Mock(side_effect=lambda dt,**kw:
            [self.old_rule] if kw['filters']['schema_snapshot']=='old-snapshot' else [self.new_rule]))
        self.patch = patch.object(exchange, 'frappe', self.fake)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_local_reference_uses_retired_local_reader_instead_of_portable_package_identity(self):
        item = {'definition_id': 'PACKAGE-P', 'content': {'assignments': [{'query_rule_id': 'PACKAGE-R'}]}}
        original = deepcopy(self.content)
        result = exchange._remap_existing_mutation_rules(self.parent, item, {'HeroCustomers': self.current})
        self.assertEqual(result['assignments'], [{'query_rule_id': 'LOCAL-N'}, {'query_rule_id': ''}])
        self.assertEqual(json.loads(self.parent.mapping_json), original)
        self.assertEqual(item['content']['assignments'][0]['query_rule_id'], 'PACKAGE-R')
        self.assertEqual(self.fake.get_all.call_args_list[0].kwargs['filters']['definition_id'], ['in', ['LOCAL-L']])

    def test_changed_rule_semantics_or_transform_options_are_not_rebound(self):
        self.new_rule['source_path'] = 'different'
        with self.assertRaises(exchange.ConfigExchangeError):
            exchange._remap_existing_mutation_rules(self.parent, {}, {'HeroCustomers': self.current})
        self.new_rule['source_path'] = self.old_rule['source_path']
        previous = json.loads(self.previous.mapping_json)
        previous['query_adapter'] = {'transform_options': {'LOCAL-L': {'map': {'A': 'B'}}}}
        self.previous['mapping_json'] = json.dumps(previous)
        with self.assertRaises(exchange.ConfigExchangeError):
            exchange._remap_existing_mutation_rules(self.parent, {}, {'HeroCustomers': self.current})

    def test_foreign_reader_or_unselected_old_identity_is_rejected(self):
        for change in ('source', 'selection'):
            with self.subTest(change=change):
                self.previous['source_instance'] = 'foreign' if change == 'source' else 'source-a'
                if change == 'selection': self.previous['mapping_json'] = json.dumps({'target_operation': 'HeroCustomers', 'mapping_definition_ids': []})
                with self.assertRaises(exchange.ConfigExchangeError):
                    exchange._remap_existing_mutation_rules(self.parent, {}, {'HeroCustomers': self.current})


class TestReplacementRebaseOrder(unittest.TestCase):
    def setUp(self):
        self.events = []
        config = {'target_operation': 'HeroCustomers', 'mapping_definition_ids': ['old'],
                  'records_path': 'customers', 'identity_path': 'id', 'page_size': 100}
        self.item = {'target_operation': 'HeroCustomers', 'mapping': config, 'query': 'query Q { id }'}
        self.payload = {'read_services': [self.item], 'mapping_package': {'schema': {}}, 'settings': {}}
        self.snapshot = Row(name='new-snapshot', schema_fingerprint='same-fingerprint', snapshot_json=json.dumps({'__schema': {}}))
        self.parent = Row(name='parent', schema_snapshot='old-snapshot', mapping_revision=1,
                          mapping_json=json.dumps(config))
        self.reader = Row(name='reader', query_document=self.item['query'], read_mapping='parent',
                          execution_status='Approved', approved_service_revision=1, service_revision=1,
                          approved_mapping_revision=1)
        self.fake = SimpleNamespace(db=SimpleNamespace(
            savepoint=Mock(side_effect=lambda name: self.events.append('savepoint')),
            rollback=Mock()))
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(exchange, 'frappe', self.fake))
        for name, result in [('_replacement_rules', {}), ('_settings_changes', {}),
                             ('_source_snapshot', (self.snapshot, 'selected')), ('_replacement_check', {}),
                             ('_existing_equivalent', self.parent), ('_matching_reader', self.reader),
                             ('_archive_replaced_drafts', None), ('_retire_replaced', {}),
                             ('_verify_replacement_readers', None), ('_apply_settings', None)]:
            stack.enter_context(patch.object(exchange, name, return_value=result))
        stack.enter_context(patch.object(exchange, '_source_mappings', side_effect=self.candidates))
        stack.enter_context(patch('hero_integration.services.schema_provisioning.provision_schema',
                                 side_effect=lambda schema: self.events.append('ddl')))
        stack.enter_context(patch('hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping.lock_exchange_context',
                                 side_effect=lambda *a: self.events.append('lock')))
        stack.enter_context(patch('hero_integration.services.read_contract.from_documents'))
        self.save_mutations = stack.enter_context(patch('hero_integration.services.config_mutation_drafts.save', return_value={}))

    def candidates(self, *args):
        self.events.append('parents')
        return [self.parent]

    def rebase(self, snapshot):
        self.events.append('rebase')
        self.assertEqual(snapshot.name, 'new-snapshot')
        self.parent['schema_snapshot'] = snapshot.name

    def test_native_parent_is_checked_after_rebase_with_fresh_candidates(self):
        result = exchange._replace_config('new-snapshot', self.payload, 'source', before_mapping_write=self.rebase)
        self.assertEqual(self.events, ['ddl', 'lock', 'rebase', 'savepoint', 'parents'])
        self.assertEqual(result['read_services'][0]['state'], 'Unchanged')
        self.fake.db.rollback.assert_not_called()

    def test_real_native_contract_difference_still_blocks_and_rolls_back(self):
        changed = json.loads(self.parent.mapping_json)
        changed['page_size'] = 20
        self.parent['mapping_json'] = json.dumps(changed)
        with self.assertRaises(exchange.ConfigExchangeError):
            exchange._replace_config('new-snapshot', self.payload, 'source', before_mapping_write=self.rebase)
        self.fake.db.rollback.assert_called_once_with(save_point='hero_configuration_replace')
        self.save_mutations.assert_not_called()

    def test_combined_import_passes_remapped_payload_to_mutation_draft_import(self):
        remapped = {**self.payload, 'mutation_mappings': [{'content': {'query_rule_id': 'published'}}]}
        from contextlib import nullcontext
        with patch.object(exchange, '_remap_mutation_rules', return_value=remapped) as convert, \
             patch('hero_integration.services.simple_mapping_exchange._target_mutations', side_effect=lambda *a, **kw:nullcontext()):
            exchange._replace_config('new-snapshot', self.payload, 'source', before_mapping_write=self.rebase)
        self.assertIs(convert.call_args.args[2]['HeroCustomers'], self.parent)
        self.save_mutations.assert_called_once_with(remapped, 'source', 'new-snapshot')
        self.assertEqual(remapped['schema_snapshot'], {'schema_fingerprint': 'same-fingerprint', 'schema': {'__schema': {}}})


if __name__ == '__main__':
    unittest.main()
