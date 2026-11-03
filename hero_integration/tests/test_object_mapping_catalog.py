import json
import unittest

from hero_integration.services.object_mapping_catalog import catalog, field
from hero_integration.services.personal_mapping_content import validate_edits, MappingDraftError


def ref(kind, name=None, of=None):
    return {'kind': kind, 'name': name, 'ofType': of}


def schema():
    address = ref('OBJECT', 'Address')
    return {'__schema': {'queryType': {'name': 'Query'}, 'types': [
        {'kind': 'OBJECT', 'name': 'Query', 'fields': [
            {'name': 'contacts', 'type': ref('LIST', of=ref('OBJECT', 'Customer'))},
            {'name': 'other_contacts', 'type': ref('LIST', of=ref('OBJECT', 'Customer'))}]},
        {'kind': 'OBJECT', 'name': 'Customer', 'fields': [
            {'name': 'title', 'type': ref('NON_NULL', of=ref('SCALAR', 'String'))},
            {'name': 'customer_address', 'type': address, 'args': [
                {'name': 'role', 'type': ref('SCALAR', 'String'), 'defaultValue': None}]},
            {'name': 'project_addresses', 'type': ref('NON_NULL', of=ref('LIST', of=address))}]},
        {'kind': 'OBJECT', 'name': 'Address', 'fields': [
            {'name': 'street', 'type': ref('SCALAR', 'String')},
            {'name': 'owner', 'type': ref('OBJECT', 'Customer')}]},
        {'kind': 'INPUT_OBJECT', 'name': 'CustomerInput', 'inputFields': []},
        {'kind': 'OBJECT', 'name': 'Task', 'fields': [
            {'name': 'title', 'type': ref('SCALAR', 'String')}]},
    ]}}


class ObjectMappingCatalogTest(unittest.TestCase):
    def test_areas_follow_reviewed_query_roots_and_real_relations(self):
        value = schema()
        query = next(t for t in value['__schema']['types'] if t['name'] == 'Query')
        query['fields'].append({'name': 'tasks', 'type': ref('NON_NULL', of=ref('LIST', of=ref('OBJECT', 'Task')))})
        query['fields'].append({'name': 'project_matches', 'type': ref('LIST', of=ref('OBJECT', 'ProjectMatch'))})
        value['__schema']['types'].append({'kind': 'OBJECT', 'name': 'ProjectMatch', 'fields': [
            {'name': 'customer', 'type': ref('OBJECT', 'Customer')},
            {'name': 'task', 'type': ref('OBJECT', 'Task')}]})
        next(t for t in value['__schema']['types'] if t['name'] == 'Customer')['fields'].append(
            {'name': 'address', 'type': ref('OBJECT', 'Address')})
        next(t for t in value['__schema']['types'] if t['name'] == 'Task')['fields'].append(
            {'name': 'project', 'type': ref('OBJECT', 'ProjectMatch')})
        found = {item['name']: item['areas'] for item in catalog(value)['objects']}
        self.assertEqual(found['Task'], ['Tasks'])
        self.assertEqual(found['Customer'], ['Customers', 'Projects'])
        self.assertEqual(found['Address'], ['Customers'])
        self.assertEqual(found['ProjectMatch'], ['Projects'])
        self.assertNotIn('Calendar', found['Task'])
        self.assertEqual(catalog(value)['areas'], ['Customers', 'Projects', 'Tasks', 'Calendar', 'Services', 'Products', 'Documents', 'Pictures'])

    def test_product_and_file_objects_belong_to_their_direct_migration_areas(self):
        value = schema()
        query = next(t for t in value['__schema']['types'] if t['name'] == 'Query')
        query['fields'] += [
            {'name': 'supply_product_versions', 'type': ref('LIST', of=ref('OBJECT', 'SupplyProductVersion'))},
            {'name': 'FileUpload_FileUploads', 'type': ref('OBJECT', 'FileUploadConnection')}]
        value['__schema']['types'] += [
            {'kind': 'OBJECT', 'name': 'SupplyProductVersion', 'fields': [{'name': 'base_data', 'type': ref('OBJECT', 'BaseData')}]},
            {'kind': 'OBJECT', 'name': 'BaseData', 'fields': [{'name': 'supply_catalog', 'type': ref('OBJECT', 'Catalog')}]},
            {'kind': 'OBJECT', 'name': 'Catalog', 'fields': [{'name': 'name', 'type': ref('SCALAR', 'String')}]},
            {'kind': 'OBJECT', 'name': 'FileUploadConnection', 'fields': [{'name': 'edges', 'type': ref('LIST', of=ref('OBJECT', 'FileUploadEdge'))}]},
            {'kind': 'OBJECT', 'name': 'FileUploadEdge', 'fields': [{'name': 'node', 'type': ref('OBJECT', 'FileUpload')}]},
            {'kind': 'OBJECT', 'name': 'FileUpload', 'fields': [{'name': 'filename', 'type': ref('SCALAR', 'String')}]}]
        found = {item['name']: item['areas'] for item in catalog(value)['objects']}
        for name in ('SupplyProductVersion', 'BaseData', 'Catalog'):
            self.assertEqual(found[name], ['Products'], name)
        for name in ('FileUploadConnection', 'FileUploadEdge', 'FileUpload'):
            self.assertEqual(found[name], ['Documents', 'Pictures'], name)
        self.assertNotIn('Products', found['Customer'])

    def test_objects_are_schema_types_not_operations_or_definitions(self):
        names = [x['name'] for x in catalog(schema())['objects']]
        self.assertEqual(names.count('Customer'), 1)
        self.assertNotIn('contacts', names)
        self.assertNotIn('CustomerInput', names)
        self.assertEqual([x['path'] for x in catalog(schema(), 'Task')['items']], ['title'])
        with self.assertRaises(ValueError):
            catalog(schema(), 'HeroCustomers')

    def test_relationship_roles_lists_and_arguments_remain_distinct(self):
        items = {x['path']: x for x in catalog(schema(), 'Customer')['items']}
        first, second = items['customer_address.street'], items['project_addresses.street']
        self.assertNotEqual(first['schema_context'], second['schema_context'])
        self.assertEqual(first['relations'][0]['arguments'][0]['name'], 'role')
        self.assertTrue(second['relations'][0]['is_list'])
        self.assertEqual(second['relations'][0]['type'], '[Address]!')
        self.assertFalse(items['title']['nullable'])
        self.assertFalse(first['activation_allowed'])
        self.assertNotIn('customer_address.owner.customer_address.street', items)
        self.assertEqual(field(schema(), 'Customer', first['path'], first['schema_context']), first)
        with self.assertRaises(ValueError):
            field(schema(), 'Customer', first['path'], second['schema_context'])
        altered = schema()
        customer = next(t for t in altered['__schema']['types'] if t['name'] == 'Customer')
        customer['fields'][1]['args'][0]['defaultValue'] = 'new-default'
        with self.assertRaises(ValueError):
            field(altered, 'Customer', first['path'], first['schema_context'])

    def test_oversized_schema_context_cannot_be_selected_or_saved_as_field_context(self):
        snapshot = schema()
        task = next(item for item in snapshot['__schema']['types'] if item['name'] == 'Task')
        task['fields'][0]['args'] = [
            {'name': 'locale', 'type': ref('SCALAR', 'String'), 'defaultValue': 'x' * 4200},
        ]
        selected = catalog(snapshot, 'Task')['items'][0]
        self.assertGreater(len(selected['schema_context']), 4096)
        self.assertFalse(selected['selectable'])
        self.assertFalse(selected['activation_allowed'])
        content = {'mapping_definition_ids': ['task-rule'], 'field_rules': {'task-rule': {'source_path': 'title'}},
                   'field_contexts': {'task-rule': {'object_type': 'Task', 'schema_context': selected['schema_context']}}}
        with self.assertRaises(MappingDraftError):
            validate_edits({}, content)

    def test_context_survives_draft_and_forged_shape_fails(self):
        selected = catalog(schema(), 'Task')['items'][0]
        content = {'mapping_definition_ids': ['task-rule'], 'field_rules': {'task-rule': {'source_path': 'title'}},
                   'field_contexts': {'task-rule': {'object_type': 'Task', 'schema_context': selected['schema_context']}}}
        self.assertEqual(validate_edits({}, content), content)
        bad = json.loads(json.dumps(content)); bad['field_contexts']['task-rule']['approval'] = 'yes'
        with self.assertRaises(MappingDraftError):
            validate_edits({}, bad)


if __name__ == '__main__':
    unittest.main()
