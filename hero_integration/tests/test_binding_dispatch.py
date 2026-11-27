"""The relationship barrier must survive redelivery without skipping an observation."""

from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import binding_dispatch as dispatch, deferred_references as refs


class TestParallelBinding(TestCase):
    def setUp(self):
        self.origins = ['child']
        self.state = {'reconciliation_stage': 0}
        self.names = [f'{i:064x}' for i in range(9)]
        self.jobs = {}
        self.frappe = NS(enqueue=Mock(side_effect=self._enqueue))
        self.rows = Mock(return_value=[NS(name=name) for name in self.names])
        self.lease = '2026-01-01'
        for seam in (
            patch.object(dispatch, 'frappe', self.frappe),
            patch.object(dispatch, '_lease', side_effect=lambda p: self.lease),
            patch.object(dispatch, '_owns_record', side_effect=lambda p, lease: self.lease == lease),
            patch.object(dispatch, 'sleep'),
            patch.object(refs, '_waiting_page', self.rows),
            patch('hero_integration.services.file_migration._parent_run', return_value='root'),
            patch('hero_integration.services.file_migration._full', side_effect=lambda p: self.state),
            patch('frappe.utils.background_jobs.get_job_status', side_effect=self.jobs.get),
        ):
            seam.start()
            self.addCleanup(seam.stop)

    def _enqueue(self, method, **job):
        self.assertEqual((job['queue'], job['enqueue_after_commit']), ('hero_binding', True))
        self.jobs[job['job_id']] = 'queued'

    def advance(self):
        dispatch.advance_page('source', self.origins, self.state, 'HeroDocuments')

    def process(self, shard, realized):
        batch = self.state['binding_batch']
        def realize(names, check_owner, *, require_complete):
            self.assertTrue(require_complete)
            self.assertTrue(check_owner())
            realized.extend(names)

        with patch.object(refs, '_realize_names', side_effect=realize):
            dispatch.realize_page('root', batch['token'], shard)
        self.jobs[batch['lanes'][shard]['job']] = 'finished'

    def test_disjoint_workers_and_stage_barrier(self):
        self.advance()
        self.assertEqual(len(self.jobs), 3)
        realized = []
        for shard in (1, 2):
            self.process(shard, realized)
        self.advance()
        self.assertEqual(self.state['reconciliation_stage'], 0)
        self.assertEqual(self.frappe.enqueue.call_count, 3)
        self.process(0, realized)
        self.advance()
        self.assertEqual(len(realized), len(self.names))
        self.assertCountEqual(realized, self.names)
        self.assertEqual(self.state, {'reconciliation_stage': 1, 'reconciliation_after': ''})

    def test_lost_job_replays_only_its_original_slice(self):
        self.advance()
        batch = self.state['binding_batch']
        old = batch['lanes'][1]['job']
        del self.jobs[old]
        self.advance()
        self.assertEqual(self.frappe.enqueue.call_count, 4)
        self.assertNotEqual(old, batch['lanes'][1]['job'])
        self.assertEqual(self.frappe.enqueue.call_args.kwargs['shard'], 1)
        realized = []
        self.process(1, realized)
        self.assertEqual(realized, self.names[1::3])
        self.assertEqual(self.rows.call_count, 1)
        self.assertEqual(self.state['reconciliation_stage'], 0)

    def test_failed_delivery_cannot_confirm_and_new_owner_can_resume(self):
        self.advance()
        batch = self.state['binding_batch']
        for _ in range(2):
            self.jobs[batch['lanes'][0]['job']] = 'failed'
            self.advance()
        self.jobs[batch['lanes'][0]['job']] = 'failed'
        with self.assertRaisesRegex(RuntimeError, 'three deliveries'):
            self.advance()
        self.assertEqual(self.state['reconciliation_stage'], 0)
        self.lease = 'new'
        self.advance()
        self.assertNotEqual(self.state['binding_batch']['token'], batch['token'])
        with patch.object(refs, '_realize_names') as realize:
            dispatch.realize_page('root', batch['token'], 1)
            realize.assert_not_called()

    def test_shared_pool_offers_short_chunks_and_waits_for_the_slowest(self):
        self.names = [f'{i:064x}' for i in range(243)]
        self.rows.return_value = [NS(name=name) for name in self.names]
        with patch.object(refs, '_shared_workers', return_value=8):
            self.advance()
        batch = self.state['binding_batch']
        self.assertEqual(len(batch['lanes']), 31)
        realized = []
        for index in reversed(range(1, len(batch['lanes']))):
            previous = len(realized)
            self.process(index, realized)
            self.assertLessEqual(len(realized) - previous, refs.BINDING_CHUNK_SIZE)
        self.advance()
        self.assertEqual(self.state['reconciliation_stage'], 0)
        self.process(0, realized)
        self.advance()
        self.assertCountEqual(realized, self.names)
        self.assertEqual(self.state['reconciliation_stage'], 1)

    def test_inflight_stride_from_three_worker_release_survives_reconfiguration(self):
        self.advance()
        self.state['binding_batch'].pop('stride')  # Deployed legacy checkpoint.
        realized = []
        with patch.object(refs, '_shared_workers', return_value=8):
            for index in (2, 0, 1):
                self.process(index, realized)
            self.advance()
        self.assertCountEqual(realized, self.names)
        self.assertEqual(self.state['reconciliation_stage'], 1)

    def test_partial_page_moves_cursor_only_after_all_jobs_finish(self):
        with patch.object(refs, 'BATCH', 3):
            self.advance()
        for key in self.jobs:
            self.jobs[key] = 'finished'
        self.advance()
        self.assertEqual(self.state, {'reconciliation_stage': 0, 'reconciliation_after': self.names[-1]})
        self.rows.return_value = []
        self.advance()
        self.assertEqual(self.state, {'reconciliation_stage': 1, 'reconciliation_after': ''})


class TestBindingQueue(TestCase):
    def test_completed_notification_starts_three_ranges_in_binding_queue(self):
        fake = NS(conf={'workers': {'hero_binding': {}}}, enqueue=Mock(),
                  db=NS(exists=Mock(return_value=True), get_value=Mock(return_value=NS(state='Completed',mode='Execute'))))
        with patch.object(refs, 'frappe', fake), patch.object(refs, 'installed', return_value=True):
            refs.notify_completed_run('run')
        calls = [c.kwargs for c in fake.enqueue.call_args_list]
        self.assertEqual([c['shard'] for c in calls], [0, 1, 2])
        self.assertTrue(all(c['queue'] == 'hero_binding' for c in calls))
        self.assertEqual(len({c['job_id'] for c in calls}), 3)

    def test_range_boundaries_partition_binary_hash_names_and_preserve_cursor(self):
        db = Mock()
        with patch.object(refs, 'frappe', NS(db=db)), patch.object(refs, 'installed', return_value=True):
            for shard in range(3):
                refs._waiting_page('s', 'cursor', 50, ['origin'], ['op'], shard=shard)
        args = [c.args for c in db.sql.call_args_list]
        self.assertEqual([p for _, p in args], [
            ('s','cursor',('origin',),('op',),'5',50),
            ('s','cursor',('origin',),('op',),'5','a',50),
            ('s','cursor',('origin',),('op',),'a',50)])
        self.assertIn('AND name<%s', args[0][0])
        self.assertIn('AND name>=%s AND name<%s', args[1][0])
        self.assertIn('AND name>=%s', args[2][0])

    def test_eight_frozen_ranges_have_distinct_job_ids_and_preserve_legacy_ranges(self):
        db = Mock()
        db.get_single_value.return_value = 8
        db.get_value.return_value = NS(state='Completed', mode='Execute')
        fake = NS(conf={'workers': {'hero_binding': {}}, 'hero_shared_workers': 8}, db=db, enqueue=Mock())
        with patch.object(refs, 'frappe', fake), patch.object(refs, 'installed', return_value=True):
            refs.notify_completed_run('run')
            current = [c.kwargs for c in fake.enqueue.call_args_list]
            self.assertEqual([(c['shard'],c['shard_count']) for c in current], [(i,8) for i in range(8)])
            for job in current:
                refs._waiting_page('s', 'cursor', 8, None, None, shard=job['shard'], shard_count=job['shard_count'])
            boundaries = [call.args[1][2:-1] for call in db.sql.call_args_list]
            self.assertEqual(boundaries, [('2',), ('2','4'), ('4','6'), ('6','8'), ('8','a'), ('a','c'),
                                          ('c','e'), ('e',)])
            refs._enqueue_completed_run('run', shard=0)
            self.assertNotEqual(fake.enqueue.call_args.kwargs['job_id'], current[0]['job_id'])
            for width, shard, next_options in ((None,2,{'shard':2}), (8,3,{'shard':3,'shard_count':8})):
                fake.get_doc = Mock(return_value=NS(mode='Execute',state='Completed',source_instance='s'))
                with patch.object(refs,'reconcile_source_page',return_value=('more', True)) as page, \
                        patch.object(refs,'_enqueue_completed_run') as submit:
                    refs.reconcile_completed_run('run', 'cursor', shard=shard, shard_count=width)
                    submit.assert_called_once_with('run','more',**next_options)
                    self.assertEqual(page.call_args.kwargs.get('limit',50), 8 if width else 50)

    def test_large_dependency_event_distributes_revision_bound_work_by_frozen_keyset(self):
        names = [f'{i:064x}' for i in range(refs.BATCH)]
        db = Mock()
        db.get_single_value.return_value = 8
        db.sql.return_value = [NS(name=n) for n in names]
        fake = NS(conf={'hero_shared_workers':8}, db=db, session=NS(user='actor'), set_user=Mock())
        with patch.object(refs,'frappe',fake), patch.object(refs,'installed',return_value=True), \
                patch.object(refs,'_enqueue_dependency') as submit, patch.object(refs,'_resolve_committed') as write:
            refs.resolve_dependency('s','supply_product','123',test_revision=9)
            calls = [c.kwargs for c in submit.call_args_list]
            self.assertEqual(len(calls), 9)
            cursor = ''
            counts = []
            for job in calls[:-1]:
                self.assertEqual((job['after'],job['test_revision']), (cursor,9))
                counts.append(sum(cursor < n <= job['through'] for n in names))
                cursor = job['through']
            self.assertEqual(sum(counts), len(names))
            self.assertLessEqual(max(counts)-min(counts), 1)
            self.assertEqual((cursor, calls[-1]['after']), (names[-1],names[-1]))
            self.assertNotIn('through',calls[-1])
            write.assert_not_called()
            db.sql.return_value = [NS(name=n) for n in names[:counts[0]]]
            submit.reset_mock()
            first=calls[0]
            refs.resolve_dependency('s','supply_product','123',after=first['after'],
                                    through=first['through'],test_revision=first['test_revision'])
            sql, params = db.sql.call_args.args
            self.assertIn('AND p.name<=%s', sql)
            self.assertEqual(params[-3:], (9,first['through'],refs.BATCH))
            self.assertEqual([c.args[0] for c in write.call_args_list], names[:counts[0]])
            submit.assert_not_called()

    def test_superseded_record_lease_stops_redelivery_before_retry_or_failure_accounting(self):
        class Deadlock(Exception):
            pass
        db = Mock()
        fake = NS(db=db, QueryDeadlockError=Deadlock, session=NS(user='actor'), set_user=Mock(), log_error=Mock())
        for exception in (Deadlock(), ValueError('failure after a record write')):
            with self.subTest(exception=type(exception).__name__), patch.object(refs,'frappe',fake), \
                    patch.object(refs.time,'sleep'), patch.object(refs,'resolve_one',side_effect=exception) as writer:
                owned = Mock(side_effect=[True, False])
                refs._realize_names(['one','two'],check_owner=owned)
                writer.assert_called_once_with('one')
                self.assertEqual(owned.call_count, 2)
                db.set_value.assert_not_called()
                db.commit.assert_not_called()
                fake.log_error.assert_not_called()

    def test_parent_fence_uses_shared_current_read_and_requires_exact_running_generation(self):
        db = Mock()
        with patch.object(dispatch,'frappe',NS(db=db)):
            for row, expected in ((('Running','right'),True), (('Running','old'),False), (('Queued','right'),False),
                                  (('Cancelled','right'),False)):
                db.sql.return_value = [row]
                self.assertIs(dispatch._owns_record('root','right'), expected)
                sql, args = db.sql.call_args.args
                self.assertIn('LOCK IN SHARE MODE',sql)
                self.assertEqual(args, ('root',))
