"""Conflict pages bound loaded rows independently of total history size."""
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration import api


class TestConflictPagination(TestCase):
    def test_large_link_history_only_loads_the_requested_slice(self):
        fake = NS(only_for=Mock(), db=NS(count=Mock(return_value=50000)),
                  get_all=Mock(return_value=[{'name': 'link', 'source_type': 'customer'}]))
        with patch.object(api, 'frappe', fake), patch.object(api, '_configured_source_instance', return_value='source-a'), \
             patch.object(api, '_unlinked_import_failures', return_value=([], 12)) as failures:
            result = api.sync_page_conflicts(40000, 8)
        self.assertEqual(result['total'], 50012)
        self.assertEqual(fake.get_all.call_args.kwargs['limit_start'], 40000)
        self.assertEqual(fake.get_all.call_args.kwargs['limit_page_length'], 8)
        self.assertEqual(fake.get_all.call_args.kwargs['filters']['source_instance'], 'source-a')
        self.assertEqual(failures.call_args.kwargs['page_size'], 7)

    def test_unlinked_area_pages_keep_source_scope_and_cross_area_order(self):
        areas = [NS(run_action='A', source_type='a', label='A'), NS(run_action='B', source_type='b', label='B')]
        slices = []
        def sql(query, args, as_dict=False):
            if query.startswith('SELECT r.name'):
                self.assertEqual(args[1], 'source-a')
                return [(args[0],)]
            self.assertEqual(args[3], 'source-a')
            self.assertIn('l.source_instance=%s', query)
            count = 3 if args[0] == 'A' else 5
            if query.startswith('SELECT COUNT'):
                return [(count,)]
            limit, offset = args[-2:]
            self.assertTrue(query.endswith('LIMIT %s OFFSET %s'))
            slices.append((args[0], limit, offset))
            return [NS(name=f'{args[0]}-{i}', source_type=args[1], source_id=str(i), detail='',
                       result='Conflict', creation='2026-01-01', run=args[0]) for i in range(offset, min(count, offset + limit))]
        with patch.object(api, 'frappe', NS(db=NS(sql=sql))):
            rows, total = api._unlinked_import_failures(areas, 'source-a', start=2, page_size=3)
        self.assertEqual(total, 8)
        self.assertEqual([r['name'] for r in rows], ['A-2', 'B-0', 'B-1'])
        self.assertEqual(slices, [('A', 3, 2), ('B', 2, 0)])

    def test_offset_beyond_links_is_passed_to_unlinked_source(self):
        fake = NS(only_for=Mock(), db=NS(count=Mock(return_value=3)), get_all=Mock())
        with patch.object(api, 'frappe', fake), patch.object(api, '_configured_source_instance', return_value='source-a'), \
             patch.object(api, '_unlinked_import_failures', return_value=([], 8)) as failures:
            result = api.sync_page_conflicts(5, 8)
        fake.get_all.assert_not_called()
        self.assertEqual(failures.call_args.kwargs, {'start': 2, 'page_size': 8})
        self.assertEqual(result['total'], 11)
