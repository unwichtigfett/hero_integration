"""Order-independent, source-scoped retries without collection imports or baseline bypasses."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import deferred_references as deferred
from hero_integration.services import migration


class PendingDoc(NS):
    def update(self, values):
        self.__dict__.update(deepcopy(values))
        return self
    def insert(self, **kwargs):
        deferred.assert_owned_write()
        self.name = self.identity_key
        self.store[self.name] = self
        return self
    def save(self, **kwargs):
        deferred.assert_owned_write()
        self.store[self.name] = self
        return self


class TestDeferredObservation(TestCase):
    def setUp(self):
        self.store, self.ready = {}, set()
        self.callbacks = []
        def get_doc(dt, name=None, **kwargs):
            return PendingDoc(**dt, store=self.store) if isinstance(dt, dict) else self.store[name]
        def set_value(dt, name, values, **kwargs): self.store[name].update(values)
        self.fake = NS(db=NS(exists=lambda dt, name:name in self.store if isinstance(name, str) else False,
            set_value=set_value, after_commit=NS(add=self.callbacks.append)),
            get_doc=get_doc, enqueue=Mock())
        for p in (patch.object(deferred, 'frappe', self.fake),
                  patch.object(deferred, 'available', side_effect=lambda source, kind, identity:(source, kind, identity) in self.ready)):
            p.start(); self.addCleanup(p.stop)

    def test_waiting_observation_is_upserted_and_removed_when_dependency_exists(self):
        record = {'id': 7, '_hero_source_instance': 'source-a', 'customer': {'id': 8},
            'downloadUrl': 'https://private.invalid/signed', 'user': {'api_token': 'sensitive', 'id': 9}}
        deps = [('customer', '8')]
        for run in ('first', 'second'):
            deferred.save(run, 'source-a', 'HeroProjectMatches', '7', 'mapping-v1', record, deps)
        self.assertEqual(len(self.store), 1)
        pending = next(iter(self.store.values()))
        self.assertEqual(pending.origin_run, 'second')
        saved = json.loads(pending.record_json)
        self.assertNotIn('downloadUrl', saved)
        self.assertNotIn('api_token', saved['user'])
        self.assertEqual(pending.status, 'Waiting')
        self.assertEqual(len(pending.dependencies), 1)
        self.assertEqual(pending.dependencies[0]['dependency_key'], deferred.key('source-a', 'customer', '8'))
        self.ready.add(('source-a', 'customer', '8'))
        deferred.save('third', 'source-a', 'HeroProjectMatches', '7', 'mapping-v1', record, deps)
        self.assertEqual(pending.status, 'Resolved')
        self.assertEqual(pending.record_json, '')

    def test_foreign_source_with_same_id_does_not_satisfy_a_dependency(self):
        self.ready.add(('source-b', 'project', '5'))
        deferred.save('run', 'source-a', 'HeroTasks', '7', 'mapping-v1', {'id': 7}, [('project', '5')])
        self.assertEqual(next(iter(self.store.values())).status, 'Waiting')
        self.assertNotEqual(deferred.key('source-a', 'project', '5'), deferred.key('source-b', 'project', '5'))

    def test_preview_and_replays_do_not_create_more_observations(self):
        with patch.object(deferred, 'save') as save:
            deferred.remember(NS(mode='Preview'), 'project', {}, NS())
            token = deferred._replaying.set(True)
            try:
                deferred.remember(NS(mode='Execute'), 'project', {}, NS())
            finally:
                deferred._replaying.reset(token)
        save.assert_not_called()

    def test_reference_extraction_uses_mapping_paths_not_fixed_field_names(self):
        definitions = [NS(source_path=path, transformation=transform, purpose='Field') for path, transform in
            [('client.identity', 'HERO Customer Link'), ('project_id', 'HERO Project Link'),
             ('responsible', 'User By Partner ID'), ('assignee', 'HERO User Link')]]
        record = {'client': {'identity': 1}, 'project_id': 0, 'responsible': 2, 'assignee': 3,
            'project_match_assignments': [{'user_id': 3}, {'user_id': True}, {'user_id': 4}]}
        self.assertEqual(deferred.references(NS(definitions=definitions), record),
            [('User', '3'), ('User', '4'), ('customer', '1'), ('partner', '2')])

    def test_generic_document_writes_are_rejected(self):
        with self.assertRaises(PermissionError): deferred.assert_owned_write()


class TestDeferredReplay(TestCase):
    def setUp(self):
        self.pending = NS(name='pending', status='Waiting', source_instance='source-a', origin_run='run',
            operation='HeroProjectMatches', source_id='7', mapping_version='plan',
            record_json=json.dumps({'id': 7, '_hero_source_instance': 'source-a'}),
            dependencies=[NS(source_type='customer', source_id='8')])
        self.run = NS(name='run', owner='Administrator', state='Completed With Conflicts', mode='Execute')
        self.fake = NS(get_doc=lambda dt,name,**kw:self.pending if dt==deferred.MAIN else self.run,
            set_user=Mock(), only_for=Mock(), db=NS(set_value=Mock()))
        self.apply = Mock(return_value=migration.Result('UPDATED', '', 'Project', 'PROJECT-7'))
        self.plan = NS(mapping_version='plan', source_identity=lambda row:str(row['id']))
        for p in (patch.object(deferred, 'frappe', self.fake), patch.object(deferred, 'available', return_value=True),
                  patch('hero_integration.client.settings.get_configuration', return_value=NS(source_instance='source-a')),
                  patch.object(migration, 'validate_run_mappings'), patch.object(migration, '_source_for_run'),
                  patch.object(migration, '_mapping_plan_for_run', return_value=self.plan),
                  patch.object(migration, '_migrate_isolated', side_effect=lambda callback:callback()),
                  patch.object(migration, '_migrate_project', self.apply),
                  patch('hero_integration.services.integration_events.record_output')):
            p.start(); self.addCleanup(p.stop)

    def test_customer_arriving_later_uses_normal_reconciliation_and_original_actor(self):
        deferred.resolve_one('pending')
        self.fake.set_user.assert_called_once_with('Administrator')
        self.apply.assert_called_once_with({'id': 7, '_hero_source_instance': 'source-a'}, self.run)
        self.assertEqual(self.fake.db.set_value.call_args.args[2]['status'], 'Resolved')
        self.assertEqual(self.fake.db.set_value.call_args.args[2]['record_json'], '')
        self.assertFalse(deferred._replaying.get())

    def test_missing_dependency_or_cancelled_run_never_writes_native_data(self):
        with patch.object(deferred, 'available', return_value=False): deferred.resolve_one('pending')
        self.apply.assert_not_called()
        self.run.state = 'Cancelled'
        with self.assertRaises(ValueError): deferred.resolve_one('pending')
        self.apply.assert_not_called()

    def test_mapping_change_does_not_reinterpret_a_saved_record(self):
        self.plan.mapping_version = 'changed'
        with self.assertRaisesRegex(ValueError, 'mapping changed'): deferred.resolve_one('pending')
        self.apply.assert_not_called()

    def test_local_conflict_remains_a_conflict_instead_of_being_acknowledged(self):
        self.apply.return_value = migration.Result('CONFLICT', 'locally edited')
        deferred.resolve_one('pending')
        self.assertEqual(self.fake.db.set_value.call_args.args[2]['status'], 'Conflict')
        self.assertTrue(self.fake.db.set_value.call_args.args[2]['record_json'])


class TestDeferredWakeups(TestCase):
    def test_identity_updates_without_waiters_do_not_enqueue_jobs(self):
        fake = NS(db=NS(sql=Mock(return_value=[])), enqueue=Mock())
        doc = NS(source_type='project', sync_status='Synchronized', source_instance='source-a', source_id='7')
        with patch.object(deferred, 'frappe', fake), patch.object(deferred, 'installed', return_value=True):
            deferred.binding_updated(doc)
            fake.enqueue.assert_not_called()
            fake.db.sql.return_value = [('dependency',)]
            deferred.binding_updated(doc)
        self.assertTrue(fake.enqueue.call_args.kwargs['enqueue_after_commit'])
        self.assertTrue(fake.enqueue.call_args.kwargs['deduplicate'])
        self.assertTrue(fake.enqueue.call_args.kwargs['job_id'].startswith('hero-reference-'))
        self.assertEqual(fake.enqueue.call_args.kwargs['source'], 'source-a')
        self.assertEqual(fake.db.sql.call_args.args[1], (deferred.MAIN, deferred.key('source-a', 'project', '7')))
        self.assertIn("p.status='Waiting'", fake.db.sql.call_args.args[0])

    def test_dependency_job_processes_one_bounded_keyset_page(self):
        rows = [NS(name=f'{n:064d}') for n in range(deferred.BATCH)]
        fake = NS(db=NS(sql=Mock(return_value=rows), commit=Mock(), rollback=Mock()),
            session=NS(user='Administrator'), set_user=Mock(), enqueue=Mock())
        with patch.object(deferred, 'frappe', fake), patch.object(deferred, 'installed', return_value=True), \
             patch.object(deferred, 'resolve_one') as resolve:
            deferred.resolve_dependency('source-a', 'project', '7')
        self.assertEqual(resolve.call_count, deferred.BATCH)
        self.assertEqual(fake.db.commit.call_count, deferred.BATCH)
        self.assertEqual(fake.enqueue.call_count, 1)
        self.assertEqual(fake.enqueue.call_args.kwargs['after'], rows[-1].name)
        self.assertEqual(fake.db.sql.call_args.args[1][-1], deferred.BATCH)
        self.assertNotIn('source-a', fake.db.sql.call_args.args[0])

    def test_post_commit_check_closes_provider_arrival_race_without_empty_retry_jobs(self):
        fake = NS(db=NS(get_value=Mock(return_value=NS(source_instance='source-a', status='Waiting'))),
            get_all=Mock(return_value=[NS(source_type='customer', source_id='8')]), enqueue=Mock(), log_error=Mock())
        with patch.object(deferred, 'frappe', fake), patch.object(deferred, 'available', return_value=False) as available:
            deferred.wake_reference('pending')
            fake.enqueue.assert_not_called()
            available.return_value = True
            deferred.wake_reference('pending')
        self.assertEqual(fake.enqueue.call_count, 1)
        self.assertEqual(fake.enqueue.call_args.kwargs['identity'], '8')

    def test_slow_page_continues_after_last_processed_reference(self):
        fake = NS(db=NS(sql=Mock(return_value=[NS(name='a'), NS(name='b')]), commit=Mock(), rollback=Mock()),
            session=NS(user='Administrator'), set_user=Mock(), enqueue=Mock())
        with patch.object(deferred, 'frappe', fake), patch.object(deferred, 'installed', return_value=True), \
             patch.object(deferred.time, 'monotonic', side_effect=[0, 1, 121]), \
             patch.object(deferred, 'resolve_one') as resolve:
            deferred.resolve_dependency('source-a', 'project', '7')
        resolve.assert_called_once_with('a')
        self.assertEqual(fake.enqueue.call_args.kwargs['after'], 'a')
