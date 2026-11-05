"""Consolidation retains valuable mappings and rebases only reviewed inputs."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import mapping_draft_consolidation as consolidation
from hero_integration.services import mapping_deployment_repair as repair
from hero_integration.services.mapping_draft_service import _hash, draft_key


def rule(source='notes', target='notes', **overrides):
    return {**dict(source_path=source, source_type='string|null', target_doctype='Project',
                   target_field=target, purpose='Field', transformation='Trim', relation='', priority=5),
            **overrides}


def content(rules):
    return {'target_operation': 'HeroProjectMatches', 'mapping_definition_ids': list(rules),
            'field_rules': deepcopy(rules), 'project_contract_version': 'v5'}


class AttrDict(dict):
    __getattr__ = dict.__getitem__


class Environment:
    """Transactional store double; real draft/metadata validation has separate tests."""
    def __init__(self):
        self.local = SimpleNamespace(site='test.example')
        self.session = SimpleNamespace(user='Administrator')
        original = content({'notes-rule': rule()})
        self.parent = SimpleNamespace(name='PARENT', definition_id='current-definition',
            schema_snapshot='TARGET-SNAPSHOT', mapping_revision=9,
            mapping_json=json.dumps({'target_operation': 'HeroProjectMatches'}))
        self.active_content = deepcopy(original)
        self.available = [AttrDict(**rule(), definition_id='notes-rule', personal_graphql_mapping='PARENT')]
        self.drafts = {}
        self.events, self.created, self.receipts = [], [], []
        self.fail_save = False
        self.package = {'target_site': self.local.site, 'source_instance': 'default',
            'schema_fingerprint': 'a' * 64, 'package_id': 'test-consolidation',
            'mappings': [{'content': original}], 'merge_inputs': []}
        for identity in ('current-definition', 'standalone-definition'):
            key = draft_key(self.local.site, identity, self.session.user)
            self.drafts[key] = SimpleNamespace(state='OPEN', content_json=json.dumps(original),
                generation=1, content_revision=2, definition_id=identity)
            self.package['merge_inputs'].append({'definition_id': identity, 'operation': 'HeroProjectMatches',
                'content_hash': _hash(original)})
        self.db = SimpleNamespace(exists=self.exists, get_value=self.get_value,
                                  savepoint=self.savepoint, rollback=self.rollback)

    def set_user(self, actor):
        self.session.user = actor

    def exists(self, doctype, key):
        if doctype == 'HERO Mapping Draft':
            return key in self.drafts
        if doctype == 'Patch Log':
            return False
        raise AssertionError(doctype)

    def get_value(self, doctype, key, field):
        self.assert_type(doctype, 'HERO Mapping Draft')
        return getattr(self.drafts[key], field) if key in self.drafts else None

    @staticmethod
    def assert_type(actual, expected):
        if actual != expected:
            raise AssertionError(actual)

    def get_all(self, doctype, **kwargs):
        if doctype == 'HERO GraphQL Mapping':
            return [self.parent.name]
        if doctype == 'HERO Frappe Mapping':
            return self.available
        raise AssertionError(doctype)

    def get_doc(self, doctype, name=None):
        if isinstance(doctype, dict):
            values = deepcopy(doctype)
            def insert():
                (self.receipts if values['doctype'] == 'Patch Log' else self.created).append(values)
            return SimpleNamespace(insert=insert)
        if doctype == 'HERO Mapping Draft':
            return self.drafts[name]
        if doctype == 'HERO GraphQL Mapping':
            return self.parent
        raise AssertionError(doctype)

    def storage(self):
        return SimpleNamespace(service=lambda: SimpleNamespace(
            discard=self.discard, open_draft=self.open_draft, save=self.save))

    def discard(self, identity, actor, generation, revision, *, confirmed):
        key = draft_key(self.local.site, identity, actor)
        doc = self.drafts[key]
        assert confirmed and (doc.generation, doc.content_revision) == (generation, revision)
        doc.state = 'DISCARDED'
        self.events.append(('discard', identity))

    def open_draft(self, identity, actor):
        key = draft_key(self.local.site, identity, actor)
        prior = self.drafts.get(key)
        generation = prior.generation + 1 if prior else 1
        draft = SimpleNamespace(key=key, content=deepcopy(self.active_content), content_revision=0,
            generation=generation, reconciliation_revision=0, base_active_revision=self.parent.mapping_revision,
            state='OPEN', content_json=json.dumps(self.active_content), definition_id=identity)
        self.drafts[key] = draft
        self.events.append(('open', identity))
        return draft

    def save(self, identity, actor, revision, active_revision, value, **kwargs):
        if self.fail_save:
            raise ValueError('Target save failed')
        key = draft_key(self.local.site, identity, actor)
        current = self.drafts[key]
        assert revision == current.content_revision
        assert active_revision == self.parent.mapping_revision
        assert kwargs['expected_generation'] == current.generation
        current.content = deepcopy(value)
        current.content_json = json.dumps(value)
        current.content_revision += 1
        self.events.append(('save', identity))
        return SimpleNamespace(draft=current)

    def savepoint(self, name):
        self.before = deepcopy((self.drafts, self.created, self.receipts))

    def rollback(self, **kwargs):
        self.drafts, self.created, self.receipts = deepcopy(self.before)

    def call(self, wrapped=False):
        with patch.object(consolidation, 'frappe', self), \
             patch.object(consolidation, 'FrappeMappingDraftStorage', self.storage), \
             patch.object(repair, 'frappe', self), \
             patch.object(repair, 'repair_readers', return_value={}):
            return repair.repair(self.package) if wrapped else consolidation.consolidate(self.package)


class TestMappingDraftConsolidation(unittest.TestCase):
    def test_name_fallback_and_price_lists_are_distinct_targets(self):
        company = rule('company_name', 'customer_name', purpose='Record Name')
        person = rule('full_name', 'customer_name', purpose='Record Name')
        self.assertNotEqual(consolidation.target_key(company), consolidation.target_key(person))
        buying = rule('base_price', 'price_list_rate', target_doctype='Item Price', relation='Standard Buying')
        selling = dict(buying, relation='Standard Selling')
        self.assertNotEqual(consolidation.target_key(buying), consolidation.target_key(selling))
        self.assertEqual(consolidation.target_key(rule()), consolidation.target_key(rule('other_source')))

    def test_missing_useful_rule_blocks_archival(self):
        old = content({'one': rule(), 'two': rule('name', 'custom_designation')})
        with self.assertRaisesRegex(ValueError, 'useful rule absent'):
            consolidation.assert_covered(old, content({'one': rule()}))

    def test_changed_fallback_priority_is_not_silently_accepted(self):
        old = content({'name': rule('company_name', 'customer_name', purpose='Record Name', priority=0)})
        changed = deepcopy(old)
        changed['field_rules']['name']['priority'] = 1
        with self.assertRaisesRegex(ValueError, 'useful rule absent'):
            consolidation.assert_covered(old, changed)

    def test_changed_value_map_options_are_not_lost_during_merge(self):
        old = content({'old-id': rule('status', 'status', transformation='Value Map')})
        old['query_adapter'] = {'version': 2, 'transform_options': {
            'old-id': {'map': {'open': 'Open', 'done': 'Completed'}}}}
        merged = content({'new-id': deepcopy(old['field_rules']['old-id'])})
        merged['query_adapter'] = {'version': 2, 'transform_options': {
            'new-id': {'map': {'open': 'Open', 'done': 'Completed'}}}}
        consolidation.assert_covered(old, merged)
        merged['query_adapter']['transform_options']['new-id']['map']['done'] = 'Cancelled'
        with self.assertRaises(ValueError):
            consolidation.assert_covered(old, merged)

    def test_changed_source_type_or_role_is_not_silently_accepted(self):
        old = content({'one': rule()})
        for field, value in [('source_type', 'int|null'), ('purpose', 'Record Name')]:
            with self.subTest(field=field):
                merged = deepcopy(old)
                merged['field_rules']['one'][field] = value
                with self.assertRaises(ValueError):
                    consolidation.assert_covered(old, merged)

    def test_additional_assignment_is_preserved_as_regular_rule(self):
        old = content({})
        old['additional_mappings'] = [{'id': 'old-id', 'source': {'field': 'base_data.quantity_min'},
            'target': {'doctype': 'Item', 'field': 'min_order_qty'}, 'transformation': 'Number'}]
        merged = content({'new-id': rule('base_data.quantity_min', 'min_order_qty',
                                        target_doctype='Item', transformation='Number')})
        consolidation.assert_covered(old, merged)

    def test_only_reviewed_category_replacement_is_allowed(self):
        old = content({'category': rule('base_data.category', 'custom_hero_category', target_doctype='Item')})
        merged = content({'group': rule('base_data.category', 'item_group', target_doctype='Item',
                                        transformation='HERO Category Group')})
        consolidation.assert_covered(old, merged)
        merged['field_rules']['group']['source_path'] = 'catalog.name'
        with self.assertRaisesRegex(ValueError, 'useful rule absent'):
            consolidation.assert_covered(old, merged)

    def test_changed_reviewed_draft_fails_before_any_write(self):
        env = Environment()
        key = draft_key(env.local.site, 'standalone-definition', 'Administrator')
        env.drafts[key].content_json = json.dumps(content({'new': rule('extra', 'extra')}))
        with self.assertRaisesRegex(ValueError, 'changed after the reviewed merge'):
            env.call()
        self.assertEqual(env.events, [])
        self.assertEqual(env.created, [])

    def test_rebased_draft_uses_current_parent_revision_before_input_archival(self):
        env = Environment()
        result = env.call()
        self.assertEqual(result[0]['active_revision'], 9)
        key = draft_key(env.local.site, 'current-definition', 'Administrator')
        self.assertEqual(env.drafts[key].generation, 2)
        self.assertEqual(env.drafts[key].base_active_revision, 9)
        self.assertEqual(env.events, [('discard', 'current-definition'), ('open', 'current-definition'),
            ('save', 'current-definition'), ('discard', 'standalone-definition')])
        self.assertEqual(env.created, [], 'the existing field template is reused')

    def test_outer_repair_failure_restores_old_drafts_and_writes_no_receipt(self):
        env = Environment()
        before = deepcopy(env.drafts)
        env.fail_save = True
        with self.assertRaisesRegex(ValueError, 'Target save failed'):
            env.call(wrapped=True)
        self.assertEqual(env.drafts, before)
        self.assertEqual(env.created, [])
        self.assertEqual(env.receipts, [])

    def test_new_rule_on_current_active_revision_is_preserved_or_blocks_merge(self):
        env = Environment()
        env.active_content['mapping_definition_ids'].append('recent-active-rule')
        env.active_content['field_rules']['recent-active-rule'] = rule('recent', 'custom_recent')
        before = deepcopy(env.drafts)
        with self.assertRaises(ValueError):
            env.call(wrapped=True)
        self.assertEqual(env.drafts, before)
        self.assertEqual(env.receipts, [])

    def test_unselected_package_snapshot_cannot_be_silently_added_to_selection(self):
        env = Environment()
        env.package['mappings'][0]['content']['field_rules']['unselected'] = rule('extra', 'custom_extra')
        before = deepcopy(env.drafts)
        with self.assertRaises(ValueError):
            env.call(wrapped=True)
        self.assertEqual(env.drafts, before)
        self.assertEqual(env.receipts, [])


if __name__ == '__main__':
    unittest.main()
