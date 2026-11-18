from hero_integration.tests.saved_reader_fixture import isolate_saved_materialization
"""Identity filters retain the approved selection and all existing restrictions."""
from unittest import TestCase
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from graphql import build_schema, introspection_from_schema, parse, print_ast, validate
from hero_integration.services.project_identity_read import CollectionIdentityReader
from hero_integration.tests import test_mapped_update_direct_readback as direct_fixtures
from hero_integration.services import mapped_update_v2 as update

SDL = '''
input StringFilter { equals: String }
input FileFilters { uuid: StringFilter, category: StringFilter }
type Row { id: Int, product_id: String, name: String, uuid: String }
type Edge { node: Row }
type Connection { edges: [Edge] }
type Query {
 contacts(first: Int, offset: Int, ids: [Int], show_deleted: Boolean): [Row]
 tasks(first: Int, offset: Int, ids: [Int]): [Row]
 calendar_events(first: Int, offset: Int, ids: [Int]): [Row]
 supply_services(first: Int, offset: Int, service_ids: [Int]): [Row]
 supply_product_versions(first: Int, offset: Int, product_ids: [String]): [Row]
 FileUpload_FileUploads(first: Int, offset: Int, filters: FileFilters): Connection
}
'''
SCHEMA = build_schema(SDL)
RAW = introspection_from_schema(SCHEMA)


class TestCollectionIdentityReader(TestCase):
    def setUp(self):
        isolate_saved_materialization(self)

    def test_schema_bound_filters_keep_selection_and_other_arguments(self):
        for area, root, argument, path, identity in (
            ('HeroCustomers', 'contacts', 'ids', 'id', '7'),
            ('HeroTasks', 'tasks', 'ids', 'id', '7'),
            ('HeroCalendarEvents', 'calendar_events', 'ids', 'id', '7'),
            ('HeroServices', 'supply_services', 'service_ids', 'id', '7'),
            ('HeroProducts', 'supply_product_versions', 'product_ids', 'product_id', 'product-7')):
            with self.subTest(area=area):
                reader = CollectionIdentityReader(None, RAW, area, path, identity)
                query = f'query Q($first:Int,$offset:Int) {{ {root}(first:$first,offset:$offset) {{ id name product_id }} }}'
                narrowed = reader.request(query)
                tree = parse(narrowed)
                self.assertFalse(validate(SCHEMA, tree))
                field = tree.definitions[0].selection_set.selections[0]
                original = parse(query).definitions[0].selection_set.selections[0]
                self.assertEqual(print_ast(field.selection_set), print_ast(original.selection_set))
                args = {arg.name.value: arg.value for arg in field.arguments}
                self.assertEqual(args[argument].values[0].value, identity)
                self.assertIn('first', args)
                self.assertIn('offset', args)

    def test_existing_identity_filter_is_never_overridden(self):
        reader = CollectionIdentityReader(None, RAW, 'HeroCustomers', 'id', '7')
        query = '{ contacts(ids:[8],show_deleted:false) { id } }'
        self.assertEqual(reader.request(query), query)

    def test_file_uuid_narrowing_preserves_picture_category(self):
        query = '{ FileUpload_FileUploads(filters:{category:{equals:"images"}}) { edges { node { id uuid } } } }'
        reader = CollectionIdentityReader(None, RAW, 'HeroPictures', 'id', '7', file_uuid='uuid-7')
        narrowed = reader.request(query)
        self.assertFalse(validate(SCHEMA, parse(narrowed)))
        self.assertIn('equals: "images"', narrowed)
        self.assertIn('uuid: {equals: "uuid-7"}', narrowed)
        self.assertIn('id', narrowed)

    def test_unknown_identity_shape_retains_safe_scan(self):
        reader = CollectionIdentityReader(None, RAW, 'HeroProducts', 'id', '7')
        query = '{ supply_product_versions { id } }'
        self.assertEqual(reader.request(query), query)

    def context(self):
        fixture = direct_fixtures.TestDirectReadback()
        ctx = fixture.context('HeroCustomers', 'contacts', page_size=100)
        self.addCleanup(fixture.doCleanups)
        ctx.raw_schema = RAW
        ctx.read.service.query_document = 'query Q($first:Int,$offset:Int) { contacts(first:$first,offset:$offset) { id } }'
        return ctx

    def test_readback_uses_filtered_pages_and_still_exhausts_them(self):
        ctx = self.context()
        client = NS(query=Mock(side_effect=[{'contacts': [{'id': 7}]}, {'contacts': []}]))
        with patch.object(update, '_guard_record'):
            self.assertEqual(update._read_record(ctx, client)['id'], 7)
        self.assertLessEqual(client.query.call_count, 2)
        self.assertTrue(all('ids: [7]' in call.args[0] for call in client.query.call_args_list))

    def test_duplicate_in_a_later_filtered_page_still_blocks(self):
        ctx = self.context()
        client = NS(query=Mock(side_effect=[{'contacts': [{'id': 7}]}, {'contacts': [{'id': 7}]}]))
        with patch.object(update, '_guard_record'), self.assertRaisesRegex(ValueError, 'ambiguous'):
            update._read_record(ctx, client)
