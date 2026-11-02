"""Configured adapters retain grouped content, metadata fences and publication evidence."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from graphql import build_schema, introspection_from_schema

from hero_integration.services import frappe_mapping_catalog as catalog
from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_exchange import _portable_content, _validate_portable_query
from hero_integration.services.personal_mapping_content import remap_query_options, validate_edits
from hero_integration.services.read_contract import ReadContract
from hero_integration.sources.hero import GraphQLServiceSource


def field(name, kind='Data', **flags):
    values = dict(fieldname=name, label=name, fieldtype=kind, options='', reqd=0,
                  read_only=0, hidden=0, permlevel=0, is_virtual=0, is_custom_field=False)
    values.update(flags)
    return NS(**values)


class Metadata:
    def __init__(self):
        self.values = {
            'Customer': [field('customer_name'), field('name')],
            'Project': [field('custom_hero_phase')],
            'Task': [field('subject'), field('status', 'Select'), field('secret', 'Password')],
            'Comment': [field('content', 'Text Editor')],
            'Contact': [field('email_id')], 'Address': [field('address_line1')],
        }

    def doctypes(self):
        return [dict(name=name, module='Test', app_name='erpnext', istable=0, issingle=0, is_virtual=0)
                for name in self.values]

    def fields(self, name):
        return self.values[name]

    def can_write(self, name):
        return name != 'Comment'


class TestConfiguredMappingPublication(TestCase):
    def test_create_and_update_profiles_have_separate_active_slots(self):
        from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping as controller
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as rule_controller
        existing = {'usage': 'SEND_TO_HERO', 'source': {'doctype': 'Task'}, 'send_options': {'mode': 'UPDATE'}}
        fake = NS(db=NS(sql=Mock(return_value=[NS(mapping_json=json.dumps(existing))])),
                  _=lambda value: value, throw=Mock(side_effect=ValueError('duplicate')))
        with patch.object(controller, 'frappe', fake), patch.object(rule_controller, '_lock_active_target'):
            controller._check_active_send_conflicts('source', 'Task', 'new', 'CREATE')
            fake.throw.assert_not_called()
            with self.assertRaises(ValueError):
                controller._check_active_send_conflicts('source', 'Task', 'new', 'UPDATE')

    def test_send_readable_sources_do_not_widen_fixed_field_writeback(self):
        meta = Metadata()
        before = {(f.doctype, f.path) for f in catalog.writeback_fields(meta) if f.selectable}
        sources = {(f.doctype, f.path) for f in catalog.send_source_fields(meta)}
        self.assertIn(('Task', 'status'), sources)
        self.assertIn(('Customer', 'customer_name'), sources)
        self.assertNotIn(('Task', 'secret'), sources)
        self.assertNotIn(('Customer', 'name'), sources)
        self.assertEqual(before, {(f.doctype, f.path) for f in catalog.writeback_fields(meta) if f.selectable})

    def test_logbook_date_slot_uses_project_authority_without_exposing_other_system_fields(self):
        class ActivityMetadata(Metadata):
            def fields(self, name):
                if name == 'Comment':
                    raise PermissionError('Comment has no standalone write grant')
                return super().fields(name)

            def project_activity_fields(self):
                self.assert_project = self.can_write('Project')
                return self.values['Comment']

        metadata = ActivityMetadata()
        result = catalog.query_target_catalog(metadata)
        fields = {(f['doctype'], f['path']) for f in result['fields']}
        self.assertIn(('Comment', 'creation'), fields)
        self.assertIn(('Comment', 'content'), fields)
        self.assertNotIn(('Customer', 'name'), fields)
        self.assertTrue(next(d for d in result['doctypes'] if d['doctype'] == 'Comment')['selectable'])
        self.assertTrue(metadata.assert_project)

    def test_predicate_sources_are_authorized_independently_from_mutation_value_types(self):
        meta = Metadata()
        content = {'source': {'doctype': 'Task', 'field': 'subject'}, 'target': {'graphql_type': 'String'},
                   'send_options': {'assignments': {'primary': {'resolver': 'FIELD',
                        'when': {'field': 'status', 'equals': 'Completed', 'otherwise': 'OMIT'}}}}}
        catalog.validate_send_mapping_sources(content, meta)
        content['send_options']['assignments']['primary']['when']['field'] = 'secret'
        with self.assertRaises(ValueError):
            catalog.validate_send_mapping_sources(content, meta)

    def test_query_options_and_empty_lookup_rules_survive_portable_structural_roundtrip(self):
        content = {'target_operation': 'HeroLookup', 'records_path': 'company.partners', 'identity_path': 'id',
            'mapping_definition_ids': [], 'field_rules': {}, 'variables': {}, 'page_size': 100,
            'query_adapter': {'version': 2, 'role': 'LOOKUP', 'read_service': 'validated-lookup', 'max_records': 100},
            'query_adapter_seal': {'source_instance': 'foreign-seal-is-not-authority'}}
        portable = _portable_content(content, 'QUERY_TO_FRAPPE')
        self.assertNotIn('query_adapter_seal', portable)
        self.assertEqual(portable['query_adapter'], content['query_adapter'])
        self.assertEqual(portable['variables'], {})
        self.assertEqual(portable['page_size'], 100)

    def test_option_identity_remapping_preserves_tables_and_derived_seal_is_read_only_in_draft(self):
        content = {'query_adapter': {'version': 2, 'transform_options': {'old': {'map': {'PV': 'Photovoltaik'}}}},
                   'query_adapter_seal': {'hash': 'local-proof'}}
        remap_query_options(content, {'old': 'new'})
        self.assertEqual(content['query_adapter']['transform_options'], {'new': {'map': {'PV': 'Photovoltaik'}}})
        changed = deepcopy(content)
        changed['query_adapter_seal']['hash'] = 'forged'
        with self.assertRaises(ValueError):
            validate_edits(content, changed)

    def test_additional_address_and_contact_rules_keep_their_native_relation_roles(self):
        schema = introspection_from_schema(build_schema('''
          type Query { contacts(first:Int!,offset:Int!):[Customer!]! }
          type Customer { id:Int! email:String address:Address }
          type Address { street:String }
        '''))
        storage = object.__new__(FrappeMappingDraftStorage)
        storage._definition = NS(operation_name='contacts')
        content = {'target_operation': 'HeroCustomers', 'query_adapter': {'version': 2}}
        for path, target, target_field, purpose, relation in [
            ('address.street', 'Address', 'address_line1', 'Native Binding', 'Customer Primary Address'),
            ('email', 'Contact', 'email_id', 'Customer Primary Contact', ''),
        ]:
            item = {'id': 'new', 'source': {'field': path}, 'target': {'doctype': target, 'field': target_field},
                    'transformation': 'Trim'}
            spec = storage._configured_additional_query_rule(item, content, schema)
            self.assertEqual((spec.purpose, spec.relation), (purpose, relation))

    def test_grouped_send_publication_does_not_create_separate_field_writebacks(self):
        storage = object.__new__(FrappeMappingDraftStorage)
        storage._definition = NS(name='mapping', direction='FRAPPE_TO_MUTATION')
        storage.frappe = NS(get_doc=Mock(side_effect=AssertionError('child writeback must not be created')))
        content = {'usage': 'SEND_TO_HERO', 'additional_mappings': [{'id': 'additional'}]}
        storage._validate_configured_send = Mock(side_effect=lambda value, _schema: value)
        observed, created = storage._publication_content(NS(content=content), {})
        self.assertEqual(observed, content)
        self.assertEqual(created, [])
        storage._validate_configured_send.assert_called_once()

    def test_existing_lookup_can_publish_without_business_field_rules(self):
        storage = object.__new__(FrappeMappingDraftStorage)
        storage._definition = NS(name='lookup', direction='QUERY_TO_FRAPPE',
                                  schema_snapshot='schema', operation_name='company')
        storage.frappe = NS(get_doc=Mock(return_value=NS(name='schema')))
        storage._assert_locked_rule_baseline = Mock()
        storage._configured_query_plan = Mock(return_value=(None, {'hash': 'current-proof'}))
        storage._validate_publication_read_contract = Mock()
        content = {'target_operation': 'HeroLookup', 'mapping_definition_ids': [],
                   'field_rules': {}, 'field_rule_bases': {},
                   'query_adapter': {'version': 2, 'role': 'LOOKUP', 'read_service': 'service', 'max_records': 100}}
        result, created = storage._publication_content(NS(content=content), {})
        self.assertEqual(result['query_adapter_seal'], {'hash': 'current-proof'})
        self.assertEqual(created, [])
        storage._assert_locked_rule_baseline.assert_called_once()
        storage._configured_query_plan.assert_called_once()
        storage._validate_publication_read_contract.assert_called_once_with(result)

    def test_large_synthetic_query_population_is_enumerated_without_live_transport(self):
        population = [{'id': index, 'title': f'Synthetic Task {index}'} for index in range(1, 2401)]
        calls = []
        def query(document, variables):
            calls.append((document, dict(variables)))
            return {'tasks': population[variables['offset']:variables['offset'] + variables['first']]}
        contract = ReadContract('query', 1, 'mapping', 1, 'HeroTasks', 'tasks', 'id', ('id', 'title'),
                                {'first': '$page_size', 'offset': '$offset'}, 25,
                                adapter_options={'version': 2})
        source = GraphQLServiceSource(NS(query=query), NS(query_document='approved synthetic query'), contract,
                                     'synthetic-source', revalidate=lambda: contract)
        offset, seen = 0, []
        while True:
            page = source.projection(offset)
            seen.extend(record['id'] for record in page.records)
            if page.next_offset is None:
                break
            offset = page.next_offset
        self.assertEqual(seen, list(range(1, 2401)))
        self.assertEqual(len(calls), 97)
        self.assertTrue(all(document == 'approved synthetic query' for document, _ in calls))
