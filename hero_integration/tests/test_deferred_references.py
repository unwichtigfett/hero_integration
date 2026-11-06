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
            get_doc=get_doc, enqueue=Mock(), get_all=lambda dt, filters, **kwargs: [
                row for row in self.store.values() if all(
                    getattr(row, field) in value[1] if isinstance(value, list) else getattr(row, field) == value
                    for field, value in filters.items())])
        for p in (patch.object(deferred, 'frappe', self.fake),
                  patch.object(deferred, '_origin_binding', return_value={'name':'origin'}),
                  patch.object(deferred, 'available', side_effect=lambda source, kind, identity:(source, kind, identity) in self.ready)):
            p.start(); self.addCleanup(p.stop)

    def test_available_dependencies_remain_saved_for_post_success_realization(self):
        record = {'id': 7, '_hero_source_instance': 'source-a', 'customer': {'id': 8},
            'downloadUrl': 'https://private.invalid/signed', 'url_download':'https://private.invalid/other', 'user': {'api_token': 'sensitive', 'id': 9}}
        deps = [('customer', '8')]
        for run in ('first', 'second'):
            deferred.save(run, 'source-a', 'HeroProjectMatches', '7', 'mapping-v1', record, deps)
        self.assertEqual(len(self.store), 2)
        pending = self.store[deferred.observation_key('source-a', 'HeroProjectMatches', '7', 'second')]
        self.assertEqual(pending.origin_run, 'second')
        saved = json.loads(pending.record_json)
        self.assertNotIn('downloadUrl', saved)
        self.assertNotIn('url_download', saved)
        self.assertNotIn('api_token', saved['user'])
        self.assertEqual(pending.status, 'Waiting')
        self.assertEqual(len(pending.dependencies), 1)
        self.assertEqual(pending.dependencies[0]['dependency_key'], deferred.key('source-a', 'customer', '8'))
        self.ready.add(('source-a', 'customer', '8'))
        deferred.save('third', 'source-a', 'HeroProjectMatches', '7', 'mapping-v1', record, deps)
        self.assertEqual(pending.status, 'Resolved')
        pending = self.store[deferred.observation_key('source-a', 'HeroProjectMatches', '7', 'third')]
        self.assertEqual(pending.status, 'Waiting')
        self.assertTrue(pending.record_json)

    def test_empty_assignment_observation_is_saved_for_deferred_removal(self):
        deferred.save('run', 'source-a', 'HeroProjectMatches', '7', 'plan',
            {'id': 7, 'project_match_assignments': []}, [], result_code='UPDATED', defer_empty=True)
        pending = self.store[deferred.observation_key('source-a', 'HeroProjectMatches', '7', 'run')]
        self.assertEqual(pending.status, 'Waiting')
        self.assertEqual(json.loads(pending.record_json)['project_match_assignments'], [])

    def test_foreign_source_with_same_id_does_not_satisfy_a_dependency(self):
        self.ready.add(('source-b', 'project', '5'))
        deferred.save('run', 'source-a', 'HeroTasks', '7', 'mapping-v1', {'id': 7}, [('project', '5')])
        self.assertEqual(next(iter(self.store.values())).status, 'Waiting')
        self.assertNotEqual(deferred.key('source-a', 'project', '5'), deferred.key('source-b', 'project', '5'))

    def test_arrived_dependency_does_not_hide_an_independent_conflict(self):
        deps = [('project', '5')]
        deferred.save('run', 'source-a', 'HeroTasks', '7', 'plan', {'id': 7}, deps)
        pending = self.store[deferred.observation_key('source-a', 'HeroTasks', '7', 'run')]
        self.ready.add(('source-a', 'project', '5'))
        deferred.save('run-2', 'source-a', 'HeroTasks', '7', 'plan', {'id': 7}, deps, result_code='CONFLICT')
        pending = self.store[deferred.observation_key('source-a', 'HeroTasks', '7', 'run-2')]
        self.assertEqual(pending.status, 'Conflict')
        self.assertTrue(pending.record_json)

    def test_available_bindings_do_not_hide_a_still_pending_assignment(self):
        deps = [('User', '5')]
        self.ready.add(('source-a', 'User', '5'))
        deferred.save('run', 'source-a', 'HeroTasks', '7', 'plan', {'id': 7}, deps, result_code='PENDING_REVIEW')
        pending = self.store[deferred.observation_key('source-a', 'HeroTasks', '7', 'run')]
        self.assertEqual(pending.status, 'Conflict')
        self.assertTrue(pending.record_json)
        deferred.save('run-2', 'source-a', 'HeroTasks', '7', 'plan', {'id': 7}, deps, result_code='SKIPPED')
        pending = self.store[deferred.observation_key('source-a', 'HeroTasks', '7', 'run-2')]
        self.assertEqual(pending.status, 'Waiting')
        self.assertTrue(pending.record_json)

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

    def test_legacy_task_lifecycle_keeps_assignee_and_author_ids(self):
        plan = NS(operation='HeroTasks', target_operation='', definitions=[], adapter_options={})
        record = {'target_user_id': 7, 'author_user_id': 8}
        self.assertEqual(deferred.references(plan, record), [('User', '7'), ('User', '8')])
        self.assertEqual(deferred.references(NS(operation='HeroTasks', definitions=[], adapter_options={'version':2}), record), [])

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
        pending = self.store[deferred.observation_key('source-a', 'HeroCustomers', '7', 'run')]
        self.assertEqual(pending.status, 'Waiting')
        self.ready.add(('source-a', 'customer_address', '2'))
        deferred.save('run-2', 'source-a', 'HeroCustomers', '7', 'plan', record, deps, result_code='SKIPPED')
        pending = self.store[deferred.observation_key('source-a', 'HeroCustomers', '7', 'run-2')]
        self.assertEqual(pending.status, 'Waiting')

    def test_deletion_or_rejection_cancels_the_old_observation_before_dependency_arrives(self):
        for code in ('SKIPPED_SOURCE_DELETED', 'SKIPPED_REJECTED'):
            with self.subTest(code=code):
                deferred.save('old', 'source-a', 'HeroTasks', '7', 'plan',
                    {'id': 7, 'is_deleted': False}, [('project', '5')])
                deferred.save('foreign', 'source-b', 'HeroTasks', '7', 'plan',
                    {'id': 7, 'is_deleted': False}, [('project', '5')])
                name = deferred.observation_key('source-a', 'HeroTasks', '7', 'old')
                run = NS(name='new', mode='Execute')
                record = {'id': 7, 'is_deleted': True, '_hero_source_instance': 'source-a'}
                plan = NS(source_identity=lambda row:str(row['id']))
                with patch.object(deferred, 'installed', return_value=True), \
                     patch.object(migration, '_mapping_plan_for_run', return_value=plan):
                    deferred.remember(run, 'Task', record, NS(code=code))
                self.assertEqual(self.store[name].status, 'Resolved')
                self.assertEqual(self.store[name].record_json, '')
                self.assertEqual(self.store[deferred.observation_key('source-b', 'HeroTasks', '7', 'foreign')].status, 'Waiting')
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
        pending = self.store[deferred.observation_key('source-a', 'HeroDocuments', '7', 'run')]
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
        pending = self.store[deferred.observation_key('source-a', 'HeroDocuments', '7', 'run')]
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
            record_json=json.dumps({'id': 7, '_hero_source_instance': 'source-a', '__hero_origin_binding':{'name':'origin'}}),
            dependencies=[NS(source_type='customer', source_id='8')])
        self.run = NS(name='run', owner='Administrator', state='Completed', mode='Execute',
            source_instance='source-a', source_binding_revision=1, graphql_mapping='mapping')
        self.config = NS(source_instance='source-a', source_binding_revision=1, source_binding_confirmed=True,
            endpoint=NS(url='https://same-endpoint.invalid/graphql'))
        self.fake = NS(get_doc=lambda dt,name,**kw:self.pending if dt==deferred.MAIN else self.run,
            set_user=Mock(), only_for=Mock(), db=NS(set_value=Mock()))
        self.apply = Mock(return_value=migration.Result('UPDATED', '', 'Project', 'PROJECT-7'))
        self.plan = NS(mapping_version='plan', source_identity=lambda row:str(row['id']))
        for p in (patch.object(deferred, 'frappe', self.fake), patch.object(deferred, 'available', return_value=True),
                  patch.object(deferred, '_origin_binding', return_value={'name':'origin'}),
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

    def test_replay_waits_for_successful_origin_run_even_if_dependency_is_available(self):
        for state in ('Queued', 'Running', 'Blocked', 'Completed With Conflicts'):
            with self.subTest(state=state):
                self.run.state = state
                deferred.resolve_one('pending')
                self.apply.assert_not_called()
                self.fake.db.set_value.assert_not_called()
        self.run.state = 'Completed'
        deferred.resolve_one('pending')
        self.apply.assert_called_once()

    def test_full_reconciliation_pages_by_source_and_keeps_failures_visible(self):
        rows = [NS(name='a'), NS(name='b')]
        db = NS(sql=Mock(return_value=rows), commit=Mock(), rollback=Mock(), set_value=Mock(), get_value=Mock(return_value='Waiting'))
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
        self.pending.record_json = json.dumps({**record, '__hero_origin_binding':{'name':'origin'}})
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

    def test_changed_origin_binding_blocks_before_native_reconciliation(self):
        for current in (None, {'name':'replacement'}):
            with self.subTest(current=current), patch.object(deferred,'_origin_binding',return_value=current):
                with self.assertRaisesRegex(ValueError,'source observation or binding changed'):
                    deferred.resolve_one('pending')
        self.apply.assert_not_called()

    def test_older_pending_record_without_origin_proof_requires_refresh(self):
        record = json.loads(self.pending.record_json)
        record.pop('__hero_origin_binding')
        self.pending.record_json = json.dumps(record)
        with self.assertRaisesRegex(ValueError,'refresh the source record'):
            deferred.resolve_one('pending')
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

    def test_v2_task_replay_uses_only_the_binding_processor(self):
        self.pending.operation = 'HeroTasks'
        self.pending.dependencies = [NS(source_type='project', source_id='8')]
        self.plan.operation, self.plan.target_operation = 'tasks', 'HeroTasks'
        self.plan.adapter_options, self.plan.definitions = {'version': 2}, [NS(source_path='title')]
        self.pending.record_json = json.dumps({'id': 7, 'comment': 'not selected', '_hero_source_instance': 'source-a',
                                              '__hero_origin_binding':{'name':'origin'}})
        mapping = NS(mapping_json=json.dumps({'query_adapter': {'version': 2}}))
        original_get_doc = self.fake.get_doc
        self.fake.get_doc = lambda dt,name,**kw:mapping if dt == 'HERO GraphQL Mapping' else original_get_doc(dt,name,**kw)
        migration._TASK_USER_DIRECTORY['emails'] = {9: 'foreign@example.invalid'}
        self.addCleanup(migration._TASK_USER_DIRECTORY.clear)
        with patch.object(migration, '_source_for_run', return_value=NS(source_instance='source-a')), \
             patch.object(migration,'_migrate_native_task',side_effect=AssertionError('Full intake replay')), \
             patch.object(migration, 'realize_mapped_bindings', return_value=migration.Result('UPDATED', '', 'Task', 'TASK-7')) as write:
            deferred.resolve_one('pending')
        self.assertEqual(write.call_args.args[1:],(self.run,self.plan))
        self.assertNotIn('emails', migration._TASK_USER_DIRECTORY)
        self.apply.assert_not_called()
        self.assertEqual(self.fake.db.set_value.call_args.args[2]['status'], 'Resolved')


class TestDeferredWakeups(TestCase):
    def test_only_successful_nonpreview_runs_enqueue_their_own_relationship_page(self):
        run = NS(state='Running',mode='Execute')
        fake = NS(db=NS(get_value=Mock(return_value=run),exists=Mock(return_value=True)),enqueue=Mock())
        with patch.object(deferred,'frappe',fake), patch.object(deferred,'installed',return_value=True):
            for state in ('Running','Queued','Blocked','Failed','Cancelled','Completed With Conflicts'):
                run.state = state
                deferred.notify_completed_run('origin')
            fake.enqueue.assert_not_called()
            run.state, run.mode = 'Completed','Preview'
            deferred.notify_completed_run('origin')
            fake.enqueue.assert_not_called()
            run.mode = 'Execute'
            deferred.notify_completed_run('origin')
            fake.enqueue.assert_called_once()
            kwargs = fake.enqueue.call_args.kwargs
            self.assertEqual((kwargs['run_name'],kwargs['after']),('origin',''))
            self.assertTrue(kwargs['enqueue_after_commit'])
            self.assertTrue(kwargs['deduplicate'])
            fake.enqueue.reset_mock()
            fake.db.exists.return_value = False
            deferred.notify_completed_run('origin')
            fake.enqueue.assert_not_called()

    def test_completion_worker_pages_only_its_original_area_and_rechecks_success(self):
        run = NS(state='Completed',mode='Execute',source_instance='source-a')
        fake = NS(get_doc=Mock(return_value=run),enqueue=Mock())
        with patch.object(deferred,'frappe',fake), \
             patch.object(deferred,'reconcile_source_page',return_value=('cursor',True)) as page:
            deferred.reconcile_completed_run('origin','previous')
            page.assert_called_once_with('source-a','previous',origin_runs=['origin'])
            self.assertEqual(fake.enqueue.call_args.kwargs['after'],'cursor')
            fake.enqueue.reset_mock()
            page.reset_mock()
            run.state = 'Failed'
            deferred.reconcile_completed_run('origin')
            page.assert_not_called()
            fake.enqueue.assert_not_called()

    def test_lost_completion_notification_leaves_import_success_for_scheduler_recovery(self):
        fake = NS(db=NS(get_value=Mock(return_value=NS(state='Completed',mode='Execute')),
                        exists=Mock(return_value=True)),enqueue=Mock(side_effect=RuntimeError('Queue unavailable')),
                  log_error=Mock())
        with patch.object(deferred,'frappe',fake), patch.object(deferred,'installed',return_value=True):
            deferred.notify_completed_run('origin')
        fake.log_error.assert_called_once()
        fake.enqueue.assert_called_once()

    def test_standalone_migration_completion_reaches_the_notification_boundary(self):
        fake = NS(db=NS(get_default=Mock(return_value=None),get_value=Mock(return_value='Completed')))
        with patch.object(migration,'frappe',fake), \
             patch('hero_integration.services.file_migration.on_run_finished') as notify:
            migration._advance_full_migration('standalone')
        notify.assert_called_once_with('standalone')

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
        pending = NS(source_instance='source-a', status='Waiting', operation='HeroProjectMatches', origin_run='run')
        fake = NS(db=NS(get_value=Mock(side_effect=lambda doctype, *a, **k: pending if doctype == deferred.MAIN else 'Completed')),
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


class TestBatchAvailability(TestCase):
    def setUp(self):
        self.tables = {
            'HERO Integration Link': [NS(source_system='HERO', source_instance='a', source_type='project',
                source_id='1', target_doctype='Project', target_name='P', sync_status='Conflict')],
            'Project': [NS(name='P')], 'User': [NS(name='u', enabled=1)], 'HERO Object Binding': [],
        }
        def get_all(doctype, filters, fields=None, pluck=None, **kwargs):
            def matches(row):
                return all(getattr(row,key,None) in val[1] if isinstance(val,list) else
                    getattr(row,key,None)==val for key,val in filters.items())
            rows=[row for row in self.tables.get(doctype,[]) if matches(row)]
            return [getattr(row,pluck) for row in rows] if pluck else rows
        self.fake=NS(get_all=Mock(side_effect=get_all),local=NS(site='site'))
        patcher=patch.object(deferred,'frappe',self.fake)
        patcher.start();self.addCleanup(patcher.stop)

    def test_duplicate_demands_share_queries_and_sources_remain_separate(self):
        result=deferred.available_many('a',[('project','1')]*50+[('project','missing')])
        self.assertEqual(result,{('project','1'):True,('project','missing'):False})
        self.assertEqual(self.fake.get_all.call_count,2)
        self.assertFalse(deferred.available_many('b',[('project','1')])['project','1'])

    def test_conflicting_duplicate_links_and_missing_native_targets_are_unavailable(self):
        self.tables['HERO Integration Link']*=2
        self.assertFalse(deferred.available_many('a',[('project','1')])['project','1'])
        self.tables['HERO Integration Link']=self.tables['HERO Integration Link'][:1]
        self.tables['Project']=[]
        self.assertFalse(deferred.available_many('a',[('project','1')])['project','1'])

    def test_user_requires_enabled_target_and_unique_matching_current_site_binding(self):
        self.tables['HERO Integration Link']=[NS(source_system='HERO',source_instance='a',source_type='User',
            source_id='7',target_doctype='User',target_name='u',sync_status='Synchronized')]
        binding=NS(site_scope='site',source_instance='a',object_type='User',external_object_id='7',
            target_doctype='User',target_name='u',lifecycle='VALID')
        self.tables['HERO Object Binding']=[binding]
        self.assertTrue(deferred.available_many('a',[('User','7')])['User','7'])
        self.tables['User'][0].enabled=0
        self.assertFalse(deferred.available_many('a',[('User','7')])['User','7'])
        self.tables['User'][0].enabled=1
        binding.site_scope='other-site'
        self.assertFalse(deferred.available_many('a',[('User','7')])['User','7'])
        binding.site_scope='site'
        self.tables['HERO Object Binding']*=2
        self.assertFalse(deferred.available_many('a',[('User','7')])['User','7'])

    def test_scheduling_snapshot_is_not_used_by_default_write_readiness(self):
        ready=deferred.available_many('a',[('project','1')])
        with patch.object(deferred,'available',return_value=False) as fresh:
            self.assertTrue(deferred.dependencies_ready('a','HeroTasks',[('project','1')],availability=ready))
            self.assertFalse(deferred.dependencies_ready('a','HeroTasks',[('project','1')]))
            fresh.assert_called_once()

    def test_partner_retains_specialized_source_bound_employee_resolution(self):
        with patch.object(deferred,'available',return_value=True) as resolve:
            self.assertTrue(deferred.available_many('a',[('partner','9')]*20)['partner','9'])
        resolve.assert_called_once_with('a','partner','9')

    def test_empty_page_does_not_issue_an_in_query(self):
        self.assertEqual(deferred.available_many('a',[]),{})
        self.fake.get_all.assert_not_called()


class TestRecoveryScheduling(TestCase):
    def test_bulk_page_preserves_success_gate_first_attempts_and_job_deduplication(self):
        rows=[NS(name=name,source_instance='a',operation='HeroTasks',origin_run=run,realization_started=started)
              for name,run,started in [('first','complete',0),('active','running',1),
                  ('ready','complete',1),('duplicate','complete',1),('missing','complete',1)]]
        children=[NS(parent=name,source_type='project',source_id=identity)
                  for name,identity in [('first','missing'),('ready','1'),('duplicate','1'),('missing','2')]]
        def get_all(doctype,**kwargs):
            return rows if doctype==deferred.MAIN else ['complete'] if doctype=='HERO Integration Run' else children
        fake=NS(get_all=Mock(side_effect=get_all),db=NS(set_value=Mock()),
                utils=NS(now_datetime=lambda:'now'),enqueue=Mock(),log_error=Mock())
        with patch.object(deferred,'frappe',fake),patch.object(deferred,'installed',return_value=True),                 patch.object(deferred,'available_many',return_value={('project','1'):True,('project','2'):False}) as bulk,                 patch.object(deferred,'resolve_one',side_effect=AssertionError('scheduler cannot write targets')):
            deferred.recover_pending()
        self.assertEqual(fake.enqueue.call_count,2)
        self.assertEqual(fake.enqueue.call_args_list[0].kwargs['name'],'first')
        self.assertEqual(fake.enqueue.call_args_list[1].kwargs['identity'],'1')
        bulk.assert_called_once_with('a',{('project','1'),('project','2')})
        self.assertEqual(fake.db.set_value.call_count,5)
        fake.log_error.assert_not_called()


class TestReferenceFailureState(TestCase):
    def test_error_after_commit_cannot_reclassify_resolved_or_superseded_rows(self):
        for status in ('Resolved','Conflict',None):
            with self.subTest(status=status):
                db=NS(rollback=Mock(),get_value=Mock(return_value=status),set_value=Mock(),commit=Mock())
                fake=NS(db=db,log_error=Mock())
                with patch.object(deferred,'frappe',fake):
                    deferred._record_failure('reference',RuntimeError('PRIVATE_SOURCE_VALUE'))
                db.set_value.assert_not_called()
                db.commit.assert_not_called()
                db.get_value.assert_called_once_with(deferred.MAIN,'reference','status',for_update=True)
                diagnostic=json.loads(fake.log_error.call_args.kwargs['message'])
                self.assertEqual(diagnostic['committed_status'],status)
                self.assertNotIn('PRIVATE_SOURCE_VALUE',fake.log_error.call_args.kwargs['message'])

    def test_waiting_failure_marks_conflict_without_replacing_observation(self):
        db=NS(rollback=Mock(),get_value=Mock(return_value='Waiting'),set_value=Mock(),commit=Mock())
        fake=NS(db=db,log_error=Mock())
        with patch.object(deferred,'frappe',fake):
            deferred._record_failure('reference',deferred.PendingObservationError('private'))
        changes=db.set_value.call_args.args[2]
        self.assertEqual(changes['status'],'Conflict')
        self.assertIn('source observation',changes['detail'])
        self.assertNotIn('record_json',changes)

    def test_page_retains_committed_success_when_commit_callback_raises(self):
        for page in ('source','dependency'):
            with self.subTest(page=page):
                stored={'status':'Waiting'}
                def commit():
                    stored['status']='Resolved'
                    raise RuntimeError('callback failed after commit')
                db=NS(sql=Mock(return_value=[NS(name='reference')]),commit=Mock(side_effect=commit),
                    rollback=Mock(),get_value=Mock(side_effect=lambda *a,**kw:stored['status']),set_value=Mock())
                fake=NS(db=db,session=NS(user='Administrator'),set_user=Mock(),log_error=Mock())
                with patch.object(deferred,'frappe',fake),patch.object(deferred,'installed',return_value=True), \
                        patch.object(deferred,'resolve_one'):
                    if page=='source':deferred.reconcile_source_page('source')
                    else:deferred.resolve_dependency('source','project','1')
                self.assertEqual(stored['status'],'Resolved')
                db.set_value.assert_not_called()
                self.assertEqual(db.commit.call_count,1)

    def test_invalid_observation_is_explicit_and_never_defaults_to_an_empty_record(self):
        for payload in ('',None,'{broken','[]','null','{}'):
            with self.subTest(payload=payload),self.assertRaises(deferred.PendingObservationError):
                deferred._source_observation(NS(record_json=payload))
        self.assertEqual(deferred._source_observation(NS(record_json='{"id":1}')),{'id':1})


class TestFullReferenceFinalization(TestCase):
    def test_dependency_stages_use_independent_cursors_and_scope_without_early_conflicts(self):
        state={'reconciliation_after':'old-global-cursor'}
        db=NS(set_value=Mock(),commit=Mock())
        with patch.object(deferred,'frappe',NS(db=db)), \
                patch.object(deferred,'reconcile_source_page',return_value=('page-end',False)) as page:
            for operation in deferred.FINAL_RELATION_ORDER:
                self.assertFalse(deferred.reconcile_full_page('source',['child'],state))
                page.assert_called_with('source','',origin_runs=['child'],operations=(operation,))
        self.assertEqual(state['reconciliation_stage'],len(deferred.FINAL_RELATION_ORDER))
        db.set_value.assert_not_called()

    def test_stage_continues_on_its_own_cursor_before_advancing(self):
        state={}
        with patch.object(deferred,'reconcile_source_page',side_effect=[('last',True),('last',False)]) as page:
            deferred.reconcile_full_page('source',['child'],state)
            self.assertEqual(state['reconciliation_stage'],0)
            deferred.reconcile_full_page('source',['child'],state)
        self.assertEqual(page.call_args.args[:2],('source','last'))
        self.assertEqual(state['reconciliation_stage'],1)
        self.assertEqual(state['reconciliation_after'],'')

    def test_only_final_phase_marks_unresolved_observations_and_preserves_payload(self):
        pending=NS(status='Waiting',source_instance='source',origin_run='child',record_json='frozen')
        db=NS(sql=Mock(return_value=[NS(name='ref')]),set_value=Mock(),commit=Mock(),count=Mock(return_value=3))
        state={'reconciliation_stage':len(deferred.FINAL_RELATION_ORDER)}
        with patch.object(deferred,'frappe',NS(db=db,get_doc=Mock(return_value=pending))):
            self.assertTrue(deferred.reconcile_full_page('source',['child'],state))
        self.assertEqual(db.sql.call_args.args[1],('source',('child',),'',deferred.BATCH))
        self.assertEqual(db.set_value.call_args.args[2]['status'],'Conflict')
        self.assertNotIn('record_json',db.set_value.call_args.args[2])
        self.assertEqual(state['reconciliation_conflicts'],3)
        self.assertEqual(pending.record_json,'frozen')

    def test_missing_child_scope_is_not_replaced_with_global_source_scan(self):
        with self.assertRaises(ValueError):deferred.reconcile_full_page('source',[],{})
