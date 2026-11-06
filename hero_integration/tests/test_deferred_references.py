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
    def test_missing_storage_is_reported_before_a_run_is_accepted(self):
        owner = NS(db=NS(table_exists=lambda name: name == deferred.MAIN), _=lambda message: message,
                   throw=Mock(side_effect=ValueError('Pending relationship storage is not installed.')))
        with self.assertRaisesRegex(ValueError, 'Pending relationship storage'):
            deferred.require_storage(owner)
        owner.throw.assert_called_once()

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

    def test_arrived_dependency_does_not_hide_an_independent_conflict(self):
        deps = [('project', '5')]
        deferred.save('run', 'source-a', 'HeroTasks', '7', 'plan', {'id': 7}, deps)
        pending = self.store[deferred.key('source-a', 'HeroTasks', '7')]
        self.ready.add(('source-a', 'project', '5'))
        deferred.save('run-2', 'source-a', 'HeroTasks', '7', 'plan', {'id': 7}, deps, result_code='CONFLICT')
        self.assertEqual(pending.status, 'Conflict')
        self.assertTrue(pending.record_json)

    def test_available_bindings_do_not_hide_a_still_pending_assignment(self):
        deps = [('User', '5')]
        self.ready.add(('source-a', 'User', '5'))
        deferred.save('run', 'source-a', 'HeroTasks', '7', 'plan', {'id': 7}, deps, result_code='PENDING_REVIEW')
        pending = self.store[deferred.key('source-a', 'HeroTasks', '7')]
        self.assertEqual(pending.status, 'Conflict')
        self.assertTrue(pending.record_json)
        deferred.save('run-2', 'source-a', 'HeroTasks', '7', 'plan', {'id': 7}, deps, result_code='SKIPPED')
        self.assertEqual(pending.status, 'Resolved')
        self.assertEqual(pending.record_json, '')

    def test_preview_and_replays_do_not_create_more_observations(self):
        with patch.object(deferred, 'save') as save:
            deferred.remember(NS(mode='Preview'), 'project', {}, NS())
            token = deferred._replaying.set(True)
            try:
                deferred.remember(NS(mode='Execute'), 'project', {}, NS())
            finally:
                deferred._replaying.reset(token)
        save.assert_not_called()

    def test_verified_historical_users_are_not_missing_dependencies(self):
        from hero_integration.services.employee_migration import historical_user_scope
        plan = NS(definitions=[], adapter_options={})
        record = {'_hero_source_instance': 'source-a', 'project_match_assignments': [
            {'user_id': 10}, {'user_id': 20}]}
        with historical_user_scope('source-a', {'10'}):
            self.assertEqual(deferred.references(plan, record), [('User', '20')])
            self.assertEqual(deferred.references(plan, {**record, '_hero_source_instance': 'other'}),
                             [('User', '10'), ('User', '20')])
        self.assertEqual(deferred.references(plan, record), [('User', '10'), ('User', '20')])

    def test_reference_extraction_uses_mapping_paths_not_fixed_field_names(self):
        definitions = [NS(source_path=path, transformation=transform, purpose='Field') for path, transform in
            [('client.identity', 'HERO Customer Link'), ('project_id', 'HERO Project Link'),
             ('responsible', 'User By Partner ID'), ('assignee', 'HERO User Link')]]
        record = {'client': {'identity': 1}, 'project_id': 0, 'responsible': 2, 'assignee': 3,
            'project_match_assignments': [{'user_id': 3}, {'user_id': True}, {'user_id': 4}]}
        self.assertEqual(deferred.references(NS(definitions=definitions), record),
            [('User', '3'), ('User', '4'), ('customer', '1'), ('partner', '2')])

    def test_secondary_address_waits_for_its_own_source_binding(self):
        relation = {'kind': 'customer_addresses', 'collection_path': 'addresses',
                    'identity_path': 'location.id', 'primary_identity_path': 'primary.id', 'deleted_path': 'deleted'}
        plan = NS(definitions=[], adapter_options={'native_relations': [relation]})
        record = {'primary': {'id': 1}, 'addresses': [
            {'deleted': False, 'location': {'id': 1}}, {'deleted': False, 'location': {'id': 2}},
            {'deleted': True, 'location': {'id': 3}}]}
        deps = deferred.references(plan, record)
        self.assertEqual(deps, [('customer_address', '2')])
        deferred.save('run', 'source-a', 'HeroCustomers', '7', 'plan', record, deps)
        pending = self.store[deferred.key('source-a', 'HeroCustomers', '7')]
        self.assertEqual(pending.status, 'Waiting')
        self.ready.add(('source-a', 'customer_address', '2'))
        deferred.save('run-2', 'source-a', 'HeroCustomers', '7', 'plan', record, deps, result_code='SKIPPED')
        self.assertEqual(pending.status, 'Resolved')

    def test_deletion_or_rejection_cancels_the_old_observation_before_dependency_arrives(self):
        for code in ('SKIPPED_SOURCE_DELETED', 'SKIPPED_REJECTED'):
            with self.subTest(code=code):
                deferred.save('old', 'source-a', 'HeroTasks', '7', 'plan',
                    {'id': 7, 'is_deleted': False}, [('project', '5')])
                deferred.save('foreign', 'source-b', 'HeroTasks', '7', 'plan',
                    {'id': 7, 'is_deleted': False}, [('project', '5')])
                name = deferred.key('source-a', 'HeroTasks', '7')
                run = NS(name='new', mode='Execute')
                record = {'id': 7, 'is_deleted': True, '_hero_source_instance': 'source-a'}
                plan = NS(source_identity=lambda row:str(row['id']))
                with patch.object(deferred, 'installed', return_value=True), \
                     patch.object(migration, '_mapping_plan_for_run', return_value=plan):
                    deferred.remember(run, 'Task', record, NS(code=code))
                self.assertEqual(self.store[name].status, 'Resolved')
                self.assertEqual(self.store[name].record_json, '')
                self.assertEqual(self.store[deferred.key('source-b', 'HeroTasks', '7')].status, 'Waiting')
                self.ready.add(('source-a', 'project', '5'))
                with patch.object(migration, '_migrate_native_task') as create:
                    deferred.resolve_one(name)
                create.assert_not_called()
                self.ready.clear()

    def test_files_keep_customer_and_project_alternatives_without_requiring_all_of_them(self):
        read = NS(plan=NS(source_identity=lambda row:'7'), mapping_version='plan')
        detail = {'file_upload_matches': [{'project_match_id': 1}, {'project_match_id': 2}],
                  'file_upload_links': [{'customer_id': 3}]}
        with patch.object(deferred, 'installed', return_value=True):
            deferred.remember_file('run', 'source-a', 'HeroDocuments', read, {'id': 7}, detail)
        pending = self.store[deferred.key('source-a', 'HeroDocuments', '7')]
        self.assertEqual({(d['source_type'], d['source_id']) for d in pending.dependencies},
                         {('project', '1'), ('project', '2'), ('customer', '3')})
        self.ready.add(('source-a', 'customer', '3'))
        self.assertFalse(deferred.dependencies_ready('source-a', 'HeroDocuments',
            [('project', '1'), ('project', '2'), ('customer', '3')]))
        self.ready.add(('source-a', 'project', '2'))
        self.assertTrue(deferred.dependencies_ready('source-a', 'HeroDocuments',
            [('project', '1'), ('project', '2'), ('customer', '3')]))
        self.assertFalse(deferred.dependencies_ready('source-a', 'HeroTasks', [('project', '1'), ('customer', '3')]))

    def test_test_file_observation_keeps_only_selected_attachment_dependencies(self):
        read = NS(plan=NS(source_identity=lambda row:'7'), mapping_version='plan')
        detail = {'file_upload_matches': [{'project_match_id': 1}, {'project_match_id': 2}],
                  'file_upload_links': [{'customer_id': 3}]}
        cohort = {'project_ids': ['2'], 'customer_ids': [], 'version': 1}
        with patch.object(deferred, 'installed', return_value=True):
            deferred.remember_file('run', 'source-a', 'HeroDocuments', read, {'id': 7}, detail, cohort=cohort)
        pending = self.store[deferred.key('source-a', 'HeroDocuments', '7')]
        self.assertEqual([(d['source_type'], d['source_id']) for d in pending.dependencies], [('project', '2')])
        self.assertEqual(json.loads(pending.record_json)['scope'], {'project_ids': ['2'], 'customer_ids': []})

    def test_many_assignees_stop_reading_when_one_missing_identity_decides_readiness(self):
        deps = [('User', str(i)) for i in range(1, 201)]
        with patch.object(deferred, 'available', side_effect=lambda source, kind, identity: identity != '2') as lookup:
            self.assertFalse(deferred.dependencies_ready('source-a', 'HeroProjectMatches', deps))
        self.assertEqual(lookup.call_count, 2)
        projects = [('project', str(i)) for i in range(1, 201)]
        with patch.object(deferred, 'available', side_effect=lambda source, kind, identity: identity == '2') as lookup:
            self.assertTrue(deferred.dependencies_ready('source-a', 'HeroDocuments', projects))
        self.assertEqual(lookup.call_count, 2)

    def test_generic_document_writes_are_rejected(self):
        with self.assertRaises(PermissionError): deferred.assert_owned_write()


class TestDeferredReplay(TestCase):
    def setUp(self):
        self.pending = NS(name='pending', status='Waiting', source_instance='source-a', origin_run='run',
            operation='HeroProjectMatches', source_id='7', mapping_version='plan',
            record_json=json.dumps({'id': 7, '_hero_source_instance': 'source-a'}),
            dependencies=[NS(source_type='customer', source_id='8')])
        self.run = NS(name='run', owner='Administrator', state='Completed With Conflicts', mode='Execute',
            source_instance='source-a', source_binding_revision=1, graphql_mapping='mapping')
        self.config = NS(source_instance='source-a', source_binding_revision=1, source_binding_confirmed=True,
            endpoint=NS(url='https://same-endpoint.invalid/graphql'))
        self.fake = NS(get_doc=lambda dt,name,**kw:self.pending if dt==deferred.MAIN else self.run,
            set_user=Mock(), only_for=Mock(), db=NS(set_value=Mock()))
        self.apply = Mock(return_value=migration.Result('UPDATED', '', 'Project', 'PROJECT-7'))
        self.plan = NS(mapping_version='plan', source_identity=lambda row:str(row['id']))
        for p in (patch.object(deferred, 'frappe', self.fake), patch.object(deferred, 'available', return_value=True),
                  patch('hero_integration.client.settings.get_configuration', side_effect=lambda **kw:self.config),
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

    def test_full_reconciliation_pages_by_source_and_keeps_failures_visible(self):
        rows = [NS(name='a'), NS(name='b')]
        db = NS(sql=Mock(return_value=rows), commit=Mock(), rollback=Mock(), set_value=Mock())
        fake = NS(db=db, session=NS(user='Administrator'), set_user=Mock(), log_error=Mock())
        with patch.object(deferred, 'frappe', fake), patch.object(deferred, 'installed', return_value=True), \
                patch.object(deferred, 'resolve_one', side_effect=[None, ValueError('changed')]):
            cursor, more = deferred.reconcile_source_page('source-a', 'previous', limit=2)
        self.assertEqual((cursor, more), ('b', True))
        self.assertEqual(db.sql.call_args.args[1], ('source-a', 'previous', 2))
        self.assertEqual(db.rollback.call_count, 1)
        self.assertEqual(db.set_value.call_args.args[1], 'b')
        self.assertEqual(db.set_value.call_args.args[2]['status'], 'Conflict')
        self.assertEqual(fake.set_user.call_count, 2)

    def test_test_sweep_never_replays_unrelated_origin_runs(self):
        db = NS(sql=Mock(return_value=[]))
        fake = NS(db=db, session=NS(user='Administrator'))
        with patch.object(deferred, 'frappe', fake), patch.object(deferred, 'installed', return_value=True):
            self.assertEqual(deferred.reconcile_source_page('source-a', origin_runs=[]), ('', False))
            db.sql.assert_not_called()
            deferred.reconcile_source_page('source-a', 'after', limit=3, origin_runs=['RUN-1', 'RUN-2'])
        self.assertIn('origin_run IN %s', db.sql.call_args.args[0])
        self.assertEqual(db.sql.call_args.args[1], ('source-a', 'after', ('RUN-1', 'RUN-2'), 3))

    def test_missing_dependency_or_cancelled_run_never_writes_native_data(self):
        with patch.object(deferred, 'available', return_value=False): deferred.resolve_one('pending')
        self.apply.assert_not_called()
        self.run.state = 'Cancelled'
        with self.assertRaises(ValueError): deferred.resolve_one('pending')
        self.apply.assert_not_called()

    def test_newly_bound_project_assignee_is_added_without_waiting_for_other_users(self):
        record = {'id': 7, '_hero_source_instance': 'source-a', 'project_match_assignments': [
            {'user_id': 11}, {'user_id': 12}]}
        self.pending.record_json = json.dumps(record)
        self.pending.dependencies = [NS(source_type='User', source_id='11'), NS(source_type='User', source_id='12')]
        project = NS(name='PROJECT-7')
        link = NS(target_doctype='Project', target_name='PROJECT-7', sync_status='Synchronized',
                  mapping_version='plan', source_payload_hash='source-hash')
        self.fake.get_doc = lambda dt, name, **kw: self.pending if dt == deferred.MAIN else self.run if dt == 'HERO Integration Run' else project
        self.fake.db.exists = Mock(return_value=True)
        self.fake.has_permission = Mock()
        with patch.object(deferred, 'available', side_effect=lambda source, kind, value: value == '11'), \
             patch.object(migration, '_link', return_value=link), patch.object(migration, '_digest', return_value='source-hash'), \
             patch('hero_integration.integrations.project_assignments.sync') as sync:
            deferred.resolve_one('pending', dependency=('User', '11'))
            sync.assert_called_once_with('PROJECT-7', {**record, 'project_match_assignments': [{'user_id': 11}]},
                                         allow_removals=False)
            link.source_payload_hash = 'changed-source'
            deferred.resolve_one('pending', dependency=('User', '11'))
            self.assertEqual(sync.call_count, 1)
        self.assertEqual(self.pending.status, 'Waiting')

    def test_mapping_change_does_not_reinterpret_a_saved_record(self):
        self.plan.mapping_version = 'changed'
        with self.assertRaisesRegex(ValueError, 'mapping changed'): deferred.resolve_one('pending')
        self.apply.assert_not_called()

    def test_local_conflict_remains_a_conflict_instead_of_being_acknowledged(self):
        self.apply.return_value = migration.Result('CONFLICT', 'locally edited')
        deferred.resolve_one('pending')
        self.assertEqual(self.fake.db.set_value.call_args.args[2]['status'], 'Conflict')
        self.assertTrue(self.fake.db.set_value.call_args.args[2]['record_json'])

    def test_file_replay_rejects_token_revision_change_or_missing_confirmation_before_relink(self):
        from hero_integration.services.file_migration import SourceBindingError
        self.pending.operation = 'HeroDocuments'
        for revision, confirmed in ((2, True), (1, False)):
            with self.subTest(revision=revision, confirmed=confirmed):
                self.config.source_binding_revision, self.config.source_binding_confirmed = revision, confirmed
                with patch.object(deferred, '_resolve_file') as relink, \
                     patch('hero_integration.services.file_migration._configuration', return_value=NS(
                         source_instance='source-a', source_binding_revision=1, source_binding_confirmed=True)) as stale:
                    with self.assertRaises(SourceBindingError): deferred.resolve_one('pending')
                stale.assert_not_called()
                relink.assert_not_called()
        self.fake.db.set_value.assert_not_called()

    def test_file_replay_checks_binding_again_before_allowing_commit(self):
        from hero_integration.services.file_migration import SourceBindingError
        self.pending.operation = 'HeroDocuments'
        def changed(_pending):
            self.config.source_binding_revision = 2
            return 'UPDATED'
        with patch.object(deferred, '_resolve_file', side_effect=changed):
            with self.assertRaises(SourceBindingError): deferred.resolve_one('pending')
        self.fake.db.set_value.assert_not_called()

    def test_one_file_target_is_sufficient_and_uses_the_confirmed_original_binding(self):
        self.pending.operation = 'HeroPictures'
        self.pending.dependencies = [NS(source_type='project', source_id='1'), NS(source_type='project', source_id='2')]
        with patch.object(deferred, 'available', side_effect=lambda source, kind, identity:identity == '2'), \
             patch.object(deferred, '_resolve_file', return_value='UPDATED') as relink:
            deferred.resolve_one('pending')
        relink.assert_called_once()
        self.assertEqual(self.fake.db.set_value.call_args.args[2]['status'], 'Resolved')

    def test_v2_task_replay_reuses_the_initial_scope_and_processor_without_legacy_onboarding(self):
        self.pending.operation = 'HeroTasks'
        self.pending.dependencies = [NS(source_type='project', source_id='8')]
        self.plan.operation, self.plan.target_operation = 'tasks', 'HeroTasks'
        self.plan.adapter_options, self.plan.definitions = {'version': 2}, [NS(source_path='title')]
        self.pending.record_json = json.dumps({'id': 7, 'comment': 'not selected', '_hero_source_instance': 'source-a'})
        mapping = NS(mapping_json=json.dumps({'query_adapter': {'version': 2}}))
        original_get_doc = self.fake.get_doc
        self.fake.get_doc = lambda dt,name,**kw:mapping if dt == 'HERO GraphQL Mapping' else original_get_doc(dt,name,**kw)
        migration._TASK_USER_DIRECTORY['emails'] = {9: 'foreign@example.invalid'}
        self.addCleanup(migration._TASK_USER_DIRECTORY.clear)
        with patch.object(migration, '_source_for_run', return_value=NS(source_instance='source-a')), \
             patch.object(migration, '_migrate_native_task', return_value=migration.Result('UPDATED', '', 'Task', 'TASK-7')) as write:
            deferred.resolve_one('pending')
        self.assertEqual(write.call_args.args[3], {'confirmed': True, 'source_instance': 'source-a', 'source_type': 'Task'})
        self.assertNotIn('comment', write.call_args.args[0])
        self.assertNotIn('emails', migration._TASK_USER_DIRECTORY)
        self.apply.assert_not_called()
        self.assertEqual(self.fake.db.set_value.call_args.args[2]['status'], 'Resolved')


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
        fake = NS(db=NS(get_value=Mock(return_value=NS(source_instance='source-a', status='Waiting', operation='HeroProjectMatches'))),
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
        resolve.assert_called_once_with('a', dependency=('project', '7'))
        self.assertEqual(fake.enqueue.call_args.kwargs['after'], 'a')
