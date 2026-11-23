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
             patch.object(api, '_import_conflicts', return_value=([], 12)) as failures:
            result = api.sync_page_conflicts(40000, 8)
        self.assertEqual(result['total'], 50012)
        self.assertEqual(fake.get_all.call_args.kwargs['limit_start'], 40000)
        self.assertEqual(fake.get_all.call_args.kwargs['limit_page_length'], 8)
        self.assertEqual(fake.get_all.call_args.kwargs['filters']['source_instance'], 'source-a')
        self.assertEqual(failures.call_args.kwargs['page_size'], 7)

    def test_import_conflict_pages_keep_source_scope_and_cross_area_order(self):
        areas = [NS(run_action='A', source_type='a', label='A', operation='AreaA'),
                 NS(run_action='B', source_type='b', label='B', operation='AreaB')]
        slices = []
        def sql(query, args, as_dict=False):
            if query.startswith('SELECT r.name'):
                self.assertEqual(args[1], 'source-a')
                return [(args[0],)]
            filters = args if query.startswith('SELECT COUNT') else args[1:-2]
            self.assertEqual((filters[3], filters[4], filters[6]), ('source-a',) * 3)
            self.assertIn('l.source_instance=%s', query)
            self.assertIn("l.sync_status IN ('Conflict', 'Pending Review')", query)
            self.assertIn("p.operation=%s AND p.status='Conflict'", query)
            run, source_type = filters[:2]
            count = 3 if run == 'A' else 5
            if query.startswith('SELECT COUNT'):
                return [(count,)]
            limit, offset = args[-2:]
            self.assertTrue(query.endswith('LIMIT %s OFFSET %s'))
            slices.append((run, limit, offset))
            return [NS(name=f'{run}-{i}', source_type=source_type, source_id=str(i), detail='',
                       result='CONFLICT', creation='2026-01-01', run=run, has_link=run == 'A',
                       target_doctype='Customer', target_name='Record') for i in range(offset, min(count, offset + limit))]
        with patch.object(api, 'frappe', NS(db=NS(sql=sql))):
            rows, total = api._import_conflicts(areas, 'source-a', start=2, page_size=3)
        self.assertEqual(total, 8)
        self.assertEqual([r['name'] for r in rows], ['A-2', 'B-0', 'B-1'])
        self.assertEqual([r['sync_status'] for r in rows], ['Import conflict', 'Not imported', 'Not imported'])
        self.assertEqual([r['target_name'] for r in rows], ['Record', '', ''])
        self.assertEqual(slices, [('A', 3, 2), ('B', 2, 0)])

    def test_offset_beyond_links_is_passed_to_unlinked_source(self):
        fake = NS(only_for=Mock(), db=NS(count=Mock(return_value=3)), get_all=Mock())
        with patch.object(api, 'frappe', fake), patch.object(api, '_configured_source_instance', return_value='source-a'), \
             patch.object(api, '_import_conflicts', return_value=([], 8)) as failures:
            result = api.sync_page_conflicts(5, 8)
        fake.get_all.assert_not_called()
        self.assertEqual(failures.call_args.kwargs, {'start': 2, 'page_size': 8})
        self.assertEqual(result['total'], 11)
