"""Batch ownership, disjoint native work, and lost queue handoff recovery."""

import copy
from contextlib import nullcontext
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration import api
from hero_integration.services import file_migration as fm, migration_waves as waves, run_reservation as slots


class TestWaveChain(TestCase):
    def setUp(self):
        self.rows = {'root': {'state': 'Running', 'action': 'Full Migration'}}
        self.state = {'parallel_areas': True, 'step': 3, 'child': '', 'notes': [], 'completed_children': []}
        self.staged, self.saved = {}, None
        self.db = Mock()
        self.db.commit.side_effect = lambda: self.rows.update(self.staged)
        self.db.get_value.side_effect = lambda dt,n,k,**kw: self.rows.get(n, {}).get(k)
        self.fake = NS(db=self.db, enqueue=Mock(), local=NS(site='dev'), ValidationError=ValueError)
        self.started = []
        self.fail_action = None
        for seam in (
            patch.object(waves, 'frappe', self.fake),
            patch.object(fm,'frappe', self.fake),
            patch.object(fm, '_start_step', side_effect=self._start),
            patch.object(fm, '_save_full', side_effect=self._save),
            patch.object(waves, '_watch'),
            patch.object(slots, 'acquire', return_value=True),
            patch.object(slots, 'release'),
            patch.object(slots, 'locked_legacy_slot', return_value=''),
            patch.object(slots, 'locked_b8_runs', return_value=[]),
            patch.object(slots, 'locked_pending_migrations', side_effect=lambda *a: [
                NS(name=n) for n,r in self.rows.items() if r['state'] in fm.ACTIVE]),
        ):
            seam.start()
            self.addCleanup(seam.stop)

    def _save(self, name, state):
        self.saved = copy.deepcopy(state)

    def _start(self, action, parent):
        if action == self.fail_action:
            return ''
        slots.admit_migration(self.fake, parent=parent, action=action)
        name = f"child-{len(self.started)}"
        self.staged[name] = {'state':'Queued','action':action}
        self.started.append(action)
        return name

    def test_parallel_batch_idempotency_parent_barrier_and_native_slot(self):
        for step, group in waves.WAVES.items():
            self.assertEqual(self.state['step'], step)
            self.assertFalse(waves.advance_wave('root', self.state))
            children = self.state['wave_children']
            self.assertEqual(tuple(children), group)
            self.assertEqual(sum(action in slots.NATIVE_ACTIONS for action in group), 1)
            self.assertEqual(len(set(fm._area_lock(a) for a in group if a in fm.SLICE_ACTIONS)), 2)
            prior = self.started.copy()
            self.rows[next(iter(children.values()))]['state'] = 'Completed With Conflicts'
            self.assertFalse(waves.advance_wave('root', self.state))
            self.assertEqual(self.started, prior)
            for n in tuple(children.values())[1:]:
                self.rows[n]['state'] = 'Completed'
            self.assertTrue(waves.advance_wave('root', self.state))
            self.assertNotIn('wave_children',self.state)
        self.assertEqual(self.state['step'],9)
        self.assertEqual(len(set(self.state['completed_children'])),6)
        self.assertEqual(len(self.state['notes']),2)

    def test_project_and_product_wave_does_not_start_any_area_twice(self):
        self.state.update(step=2, project_product_wave=True)
        for group in [('Project Migration', 'Product Migration'), ('Schedule Migration', 'Document Migration'),
                      ('Projection Migration', 'Picture Migration', 'Service Migration')]:
            self.assertFalse(waves.advance_wave('root', self.state))
            self.assertEqual(tuple(self.state['wave_children']), group)
            for child in self.state['wave_children'].values():
                self.rows[child]['state'] = 'Completed'
            self.assertTrue(waves.advance_wave('root', self.state))
        self.assertEqual(self.state['step'], 9)
        self.assertCountEqual(self.started, fm.NEW_FULL_STEPS[2:])

    def test_site_without_pictures_keeps_other_parallel_areas(self):
        self.state.update(step=2, project_product_wave=True, steps=list(fm.FULL_STEPS_WITHOUT_PICTURES))
        for group in [('Project Migration', 'Product Migration'), ('Schedule Migration', 'Document Migration'),
                      ('Projection Migration', 'Service Migration')]:
            self.assertFalse(waves.advance_wave('root', self.state))
            self.assertEqual(tuple(self.state['wave_children']), group)
            for child in self.state['wave_children'].values():
                self.rows[child]['state'] = 'Completed'
            self.assertTrue(waves.advance_wave('root', self.state))
        self.assertEqual(self.state['step'], 8)
        self.assertNotIn('Picture Migration', self.started)

    def test_disabled_picture_resume_preserves_original_wave(self):
        self.state.update(step=6, wave_children={'Projection Migration':'p', 'Picture Migration':'i',
                                                 'Service Migration':'s'})
        with patch.object(fm, '_full', return_value=self.state), patch.object(fm, 'pictures_enabled', return_value=False):
            with self.assertRaises(fm.SourceBindingError):
                waves.resume_wave(NS(name='root'))
        self.assertEqual(self.state['wave_children']['Picture Migration'], 'i')

    def test_failed_member_waits_until_remaining_children_settle(self):
        waves.advance_wave('root', self.state)
        a,b,c=self.state['wave_children'].values()
        self.rows[a]['state']='Failed'
        with patch.object(fm,'_finish') as finish:
            self.assertFalse(waves.advance_wave('root',self.state))
            finish.assert_not_called()
            self.rows[b]['state']='Completed'
            self.rows[c]['state']='Cancelled'
            self.assertFalse(waves.advance_wave('root',self.state))
            self.assertEqual(finish.call_args.args[:2],('root','Blocked'))
        self.assertEqual(self.state['step'],3)

    def test_partially_prepared_wave_has_no_commit_or_dispatch(self):
        self.fail_action = 'Product Migration'
        with self.assertRaises(fm.SourceBindingError):
            waves.advance_wave('root', self.state)
        self.assertEqual(len(self.staged),2)
        self.assertEqual(set(self.rows),{'root'})
        self.db.commit.assert_not_called()
        self.assertIsNone(self.saved)
        waves._watch.assert_not_called()
        self.assertFalse(slots.in_wave_intake())

    def test_duplicate_child_admission_does_not_bypass_reservation(self):
        with slots.admit_wave(self.fake,'root',[('Product Migration','')]):
            slots.admit_migration(self.fake,parent='root',action='Product Migration')
            with self.assertRaisesRegex(ValueError,'Unexpected child'):
                slots.admit_migration(self.fake,parent='root',action='Product Migration')
            with self.assertRaisesRegex(ValueError,'Unexpected child'):
                slots.admit_migration(self.fake,parent='foreign-root',action='Product Migration')

    def test_repairs_missing_delivery_without_restarting_running_siblings(self):
        waves.advance_wave('root', self.state)
        a,b,c=self.state['wave_children'].values()
        self.rows[a]['state']='Running'
        token = self.state['wave_watch_token']
        with patch.object(fm,'_full',return_value=self.state),patch.object(waves,'sleep'), \
                patch('hero_integration.services.migration.run_lease_alive',return_value=True), \
                patch.object(fm,'_stored_default',side_effect=lambda key: 'job-' + key.split(':')[-1]), \
                patch('frappe.utils.background_jobs.get_job_status',
                      side_effect=lambda job: 'queued' if job.endswith(c) else None) as receipt, \
                patch.object(fm,'_enqueue') as requeue:
            waves.check_publication('root', token, 6)
            requeue.assert_called_once_with(b,receipt='job-'+b)
            self.assertEqual(receipt.call_count,2)
            waves._watch.assert_called_with('root',token,7)

    def test_only_latest_slice_receipt_can_republish_after_releasing_area_lock(self):
        waves.advance_wave('root', self.state)
        child = self.state['wave_children']['Document Migration']
        self.rows[child]['state'] = 'Running'
        with patch('frappe.utils.synchronization.filelock', return_value=nullcontext()), \
                patch.object(fm, '_stored_default', return_value='next-slice'), patch.object(fm, '_enqueue') as publish:
            waves._repair_running_slice(child, 'Document Migration', 'old-intake')
            publish.assert_not_called()
            waves._repair_running_slice(child, 'Document Migration', 'next-slice')
            publish.assert_called_once_with(child, receipt='next-slice')

    def test_failed_native_handle_requires_expired_lease_and_preserves_child_id(self):
        waves.advance_wave('root', self.state)
        native, direct, product = self.state['wave_children'].values()
        for child in (native, direct, product):
            self.rows[child]['state'] = 'Running'
        token = self.state['wave_watch_token']
        with patch.object(fm, '_full', return_value=self.state), patch.object(waves, 'sleep'), \
                patch('hero_integration.api._run_job_timeout', return_value=3600), \
                patch('hero_integration.services.migration.run_lease_alive', side_effect=[True, False]), \
                patch.object(fm, '_stored_default', return_value='direct'), \
                patch('frappe.utils.background_jobs.get_job_status', side_effect=lambda job: 'started' if job=='direct' else 'failed'):
            waves.check_publication('root', token, 2)
            self.fake.enqueue.assert_not_called()
            waves.check_publication('root', token, 3)
            self.assertEqual(self.fake.enqueue.call_args.kwargs['run_name'], native)
            self.assertEqual(self.fake.enqueue.call_args.kwargs['job_id'], 'hero-integration:dev:'+native)

    def test_lost_watch_callback_is_recovered_once_by_periodic_sweep(self):
        waves.advance_wave('root', self.state)
        token = self.state['wave_watch_token']
        self.fake.conf = {'hero_migration_workers': 4}
        self.fake.get_all = Mock(return_value=['root'])
        with patch.object(fm, '_full', return_value=self.state), \
                patch('frappe.utils.background_jobs.get_job_status', side_effect=['finished','started','failed',None]):
            waves._watch.reset_mock()
            waves.recover_watches()
            waves._watch.assert_not_called()
            waves.recover_watches()
            waves._watch.assert_called_once_with('root', token, 0)

    def test_cancel_uses_every_active_wave_member(self):
        waves.advance_wave('root',self.state)
        a,b,c=self.state['wave_children'].values()
        self.rows[c]['state']='Completed'
        with patch.object(fm,'_full',return_value=self.state):
            self.assertEqual(fm.active_children('root'), [a,b])

    def test_resume_reuses_only_failed_members(self):
        waves.advance_wave('root',self.state)
        a,b,c=self.state['wave_children'].values()
        self.rows['root']['state']='Blocked'
        self.rows[a]['state']='Failed'
        self.rows[b]['state']='Completed'
        self.rows[c]['state']='Blocked'
        run=NS(name='root',save=Mock())
        def resume(name):
            slots.admit_migration(self.fake,parent='root',action=self.rows[name]['action'],resume=name)
        with patch.object(fm,'_full',return_value=self.state),patch.object(fm,'_parent_run',return_value='root'), \
                patch.object(api,'resume_run',side_effect=resume) as child_resume, patch.object(fm,'_enqueue'):
            waves.resume_wave(run)
        self.assertEqual([call.args[0] for call in child_resume.call_args_list],[a,c])
        self.assertEqual(run.state,'Queued')


class TestStandaloneAdmission(TestCase):
    def test_only_vetted_standalone_pairs_share_reservation(self):
        fake=NS(conf={'hero_migration_workers':4},db=Mock(),ValidationError=ValueError,local=NS(site='dev'))
        for candidate, peer, parent, expected in (
            ('Product Migration','Customer Migration','',True),
            ('Customer Migration','Product Migration','',True),
            ('Project Migration','Product Migration','',True),
            ('Product Migration','Project Migration','full-root',False),
            ('Product Migration','Product Migration','',False),
            ('Schedule Migration','Project Migration','',False),
            ('Full Migration','Product Migration','',False),
        ):
            with self.subTest(candidate=candidate, peer=peer, parent=parent):
                fake.db.get_value.return_value=peer
                with patch.object(slots,'acquire',return_value=True), patch.object(slots,'release'), \
                        patch.object(slots,'locked_legacy_slot',return_value=''), \
                        patch.object(slots,'locked_b8_runs',return_value=[]), \
                        patch.object(slots,'locked_pending_migrations',return_value=[NS(name='peer')]), \
                        patch.object(slots,'recover_stopped_owner',return_value=False), \
                        patch.object(fm,'_parent_run',return_value=parent):
                    with nullcontext() if expected else self.assertRaises(ValueError):
                        slots.admit_migration(fake,action=candidate)
