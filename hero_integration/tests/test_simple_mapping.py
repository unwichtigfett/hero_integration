"""The mapping list reads configured-source definitions without changing them."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from hero_integration.services import simple_mapping as sm


class Row(dict):
    __getattr__ = dict.get


class TestSimpleMapping(unittest.TestCase):
    def test_list_keeps_rule_order_and_mutation_assignments_without_writes(self):
        seen = []
        def rows(doctype, **kwargs):
            seen.append((doctype, kwargs))
            if doctype == 'HERO GraphQL Mapping':
                if kwargs['filters']['direction'] == 'QUERY_TO_FRAPPE':
                    return [Row(name='read', mapping_json=json.dumps({'target_operation':'HeroProducts', 'mapping_definition_ids':['b','a']}),schema_snapshot='snap',mapping_revision=3,operation_name='products')]
                return [Row(name='send',mapping_revision=2,operation_name='update_contact',mapping_json=json.dumps({'source':{'doctype':'Customer','field':'customer_name'},'target':{'argument':'contact','field':'full_name'}}))]
            if doctype == 'HERO Frappe Mapping':
                return [Row(definition_id=key,source_path=key,target_doctype='Item',target_field='item_name',mapping_revision=1) for key in ['a','b']]
            return [Row(name='snap')]
        fake = SimpleNamespace(get_all=rows)
        with patch.object(sm,'frappe',fake), patch.object(sm,'_app',return_value='erpnext'), patch('hero_integration.api._configured_source_instance',return_value='selected-source'):
            result=sm.state()
        self.assertEqual([row['hero'] for row in result['rules']],['b','a'])
        self.assertEqual(result['sends'][0]['hero'],'contact.full_name')
        self.assertEqual(result['sends'][0]['target'],'Customer.customer_name')
        for doctype, kwargs in seen:
            if doctype in ('HERO GraphQL Mapping','HERO GraphQL Schema Snapshot'):
                self.assertEqual(kwargs['filters']['source_instance'],'selected-source')


class TestPreparedMutationDisplay(unittest.TestCase):
    def mapping(self, field='customer_name'):
        return {'usage': 'MAPPED_UPDATE_V2', 'area': 'HeroCustomers', 'source': {'doctype': 'Customer'},
                'target': {'operation': 'update_contact'}, 'assignments': [
                    {'id': 'assignment', 'source': {'doctype': 'Customer', 'field': field},
                     'input_path': 'contact.full_name', 'resolver': 'FIELD'}]}

    def draft(self, identity='new', **changes):
        row = Row(name='draft-' + identity, definition_id=identity, owner_user='Administrator', site_scope='test-site',
            base_active_mapping='', content_revision=2, content_json=json.dumps(self.mapping()),
            new_definition_json=json.dumps({'site_scope': 'test-site', 'source_instance': 'source-a', 'definition_id': identity,
                'direction': 'FRAPPE_TO_MUTATION', 'operation_kind': 'MUTATION', 'operation_name': 'update_contact'}))
        row.update(changes)
        return row

    def test_v2_assignments_display_real_input_paths_without_a_primary_source_field(self):
        with patch.object(sm, '_app', return_value='erpnext'):
            rows = sm._send_rows(self.mapping(), 'active-mapping', 4, 'update_contact', 'Active')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['hero'], rows[0]['target'], rows[0]['operation_label']),
                         ('contact.full_name', 'Customer.customer_name', 'Customers'))

    def test_own_new_draft_is_visible_but_other_actor_source_and_site_are_hidden(self):
        own = self.draft()
        foreign_context = json.loads(own.new_definition_json); foreign_context['source_instance'] = 'other-source'
        rows = [own, self.draft('other-actor', owner_user='Other'), self.draft('other-site', site_scope='other-site'),
                self.draft('other-source', new_definition_json=json.dumps({**foreign_context, 'definition_id': 'other-source'}))]
        def get_all(dt, **kwargs):
            if dt == 'HERO Mapping Draft':
                self.assertEqual(kwargs['filters'], {'owner_user': 'Administrator', 'site_scope': 'test-site', 'state': 'OPEN'})
                return rows
            return []
        fake = SimpleNamespace(get_all=get_all, session=SimpleNamespace(user='Administrator'), local=SimpleNamespace(site='test-site'))
        with patch.object(sm, 'frappe', fake), patch.object(sm, '_app', return_value='erpnext'), patch('hero_integration.api._configured_source_instance', return_value='source-a'):
            state = sm.state()
        self.assertEqual(len(state['sends']), 1)
        self.assertEqual(state['sends'][0]['status'], 'Draft')
        self.assertEqual(state['sends'][0]['mapping'], own.name)
        self.assertEqual(state['sends'][0]['revision'], 2)

    def test_open_revision_replaces_its_active_rows_in_the_list(self):
        active = Row(name='active-mapping', definition_id='old', mapping_revision=4,
                     operation_name='update_contact', mapping_json=json.dumps(self.mapping()))
        draft = self.draft('old', base_active_mapping=active.name, new_definition_json='',
                           content_json=json.dumps(self.mapping('custom_full_name')))
        def get_all(dt, **kwargs):
            if dt == 'HERO GraphQL Mapping' and kwargs['filters']['direction'] == 'FRAPPE_TO_MUTATION': return [active]
            if dt == 'HERO Mapping Draft': return [draft]
            return []
        fake = SimpleNamespace(get_all=get_all, session=SimpleNamespace(user='Administrator'), local=SimpleNamespace(site='test-site'))
        with patch.object(sm, 'frappe', fake), patch.object(sm, '_app', return_value='erpnext'), patch('hero_integration.api._configured_source_instance', return_value='source-a'):
            state = sm.state()
        self.assertEqual([(row['status'], row['target']) for row in state['sends']], [('Draft', 'Customer.custom_full_name')])


class TestConfiguredRelationDisplay(unittest.TestCase):
    def test_native_relationships_and_logbook_context_are_projected_without_new_rule_ids(self):
        mapping = {'name': 'mapping', 'revision': 3, 'config': {'query_adapter': {
            'native_relations': [
                {'kind': 'customer_bank_account', 'identity_path': 'accounting.id', 'fields': {
                    'iban': 'accounting.iban', 'bank_name': 'accounting.bank_name', 'swift_number': 'accounting.bic'}},
                {'kind': 'project_address', 'identity_path': 'address.id', 'fields': {'address_line1': 'address.street'},
                 'country_code_path': 'address.country.code', 'parent_field': 'custom_hero_project_address'},
                {'kind': 'sales_prices', 'collection_path': 'sales_prices', 'fields': {'price_list_rate': 'net_price_per_unit'},
                 'price_lists': {'1': {'name': 'Retail'}}},
                {'kind': 'customer_contacts', 'collection_path': 'customer_contacts', 'fields': {'first_name': 'first_name'}},
            ],
            'logbook': {'path': 'histories', 'context_paths': ['histories.user_id', 'histories.weather_attachment.temp']}}}}
        with patch.object(sm, '_app', return_value='erpnext'):
            rows = sm._configured_relation_rows('HeroProjectMatches', mapping)
        targets = {(row['hero'], row['target_doctype'], row['target_field']) for row in rows}
        self.assertIn(('accounting.iban', 'Bank Account', 'iban'), targets)
        self.assertIn(('accounting.bic', 'Bank', 'swift_number'), targets)
        self.assertIn(('address.country.code', 'Address', 'country'), targets)
        self.assertIn(('address.id', 'Project', 'custom_hero_project_address'), targets)
        self.assertIn(('sales_prices.net_price_per_unit', 'Item Price', 'price_list_rate'), targets)
        self.assertIn(('customer_contacts.first_name', 'Contact', 'first_name'), targets)
        self.assertNotIn(('customer_contacts.first_name', 'Address', 'first_name'), targets)
        self.assertIn(('histories.user_id', 'Comment', 'custom_hero_logbook_context'), targets)
        self.assertTrue(all(row['definition_id'] == '' and row['mapping'] == 'mapping' for row in rows))
        self.assertEqual(len(rows), len(targets))
        price = next(row for row in rows if row['target_doctype'] == 'Item Price')
        self.assertEqual(price['target'], 'Item Price.price_list_rate (Retail)')

    def test_existing_explicit_field_rule_is_not_duplicated_by_relation_projection(self):
        mapping = {'name': 'mapping', 'revision': 1, 'config': {'query_adapter': {'native_relations': [
            {'kind': 'project_address', 'fields': {'city': 'address.city'}}]}}}
        existing = [{'hero': 'address.city', 'target_doctype': 'Address', 'target_field': 'city'}]
        with patch.object(sm, '_app', return_value='erpnext'):
            self.assertEqual(sm._configured_relation_rows('HeroProjectMatches', mapping, existing), [])
