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
