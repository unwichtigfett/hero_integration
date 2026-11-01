"""Checkpoint fences reuse the existing run, mapping and lease authorities."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase, mock

from hero_integration.services import migration, mapping_runtime


class TestRuleFingerprint(TestCase):
    def test_effective_rule_content_changes_without_a_root_revision(self):
        row = dict(definition_id='rule-1', status='Active', personal_graphql_mapping='MAP',
                   source_path='nr', target_field='custom_hero_customer_number', priority=1)
        fake = SimpleNamespace(get_all=lambda *a, **kw: [row])
        first = mapping_runtime.effective_rule_fingerprint(('rule-1',), 'MAP', frappe_module=fake)
        row['source_path'] = 'id'
        self.assertNotEqual(first, mapping_runtime.effective_rule_fingerprint(('rule-1',), 'MAP', frappe_module=fake))

    def test_missing_inactive_and_cross_root_rules_fail_closed(self):
        for rows in ([], [dict(definition_id='rule-1', status='Disabled')],
                     [dict(definition_id='rule-1', status='Active', personal_graphql_mapping='OTHER')]):
            with self.subTest(rows=rows), self.assertRaises(mapping_runtime.MappingPlanError):
                mapping_runtime.effective_rule_fingerprint(('rule-1',), 'MAP',
                    frappe_module=SimpleNamespace(get_all=lambda *a, **kw: rows))

    def test_rule_order_and_locked_read_produce_the_same_fingerprint(self):
        rows = [dict(definition_id='rule-1', status='Active', personal_graphql_mapping='MAP', source_path='nr'),
                dict(definition_id='rule-2', status='Active', personal_graphql_mapping='MAP', source_path='id')]
        sql = mock.Mock(return_value=rows)
        fake = SimpleNamespace(get_all=lambda *a, **kw: list(reversed(rows)), db=SimpleNamespace(sql=sql))
        unlocked = mapping_runtime.effective_rule_fingerprint(('rule-2', 'rule-1'), 'MAP', frappe_module=fake)
        locked = mapping_runtime.effective_rule_fingerprint(('rule-1', 'rule-2'), 'MAP', lock=True,
                                                             frappe_module=fake)
        self.assertEqual(unlocked, locked)
        self.assertEqual(sql.call_args.args[1], ('rule-1', 'rule-2'))
        self.assertIn('FOR UPDATE', sql.call_args.args[0])


class TestCheckpointTransaction(TestCase):
    def setUp(self):
        self.started = datetime(2026, 10, 31, 12)
        self.current = SimpleNamespace(state='Running', owner='Administrator', started_on=self.started,
            lease_expires_on=self.started + timedelta(hours=1), checkpoint_contract_json='{"version":1}')
        self.run = SimpleNamespace(name='RUN', checkpoint_contract_json='{"version":1}',
            lease_expires_on=self.current.lease_expires_on, save=mock.Mock())
        self.db = SimpleNamespace(rollback=mock.Mock(), commit=mock.Mock())
        self.fake = SimpleNamespace(db=self.db, get_doc=lambda *a, **kw: self.current,
                                    session=SimpleNamespace(user='Administrator'))
        self.patches = [mock.patch.object(migration, 'frappe', self.fake),
            mock.patch.object(migration, 'now_datetime', return_value=self.started + timedelta(minutes=1)),
            mock.patch.object(migration, '_locked_active_run', return_value='RUN'),
            mock.patch.object(migration, '_assert_checkpoint_contract')]
        self.assert_contract = self.patches[-1].start()
        for p in self.patches[:-1]: p.start()
        for p in self.patches: self.addCleanup(p.stop)

    def test_valid_record_and_checkpoint_commit_together(self):
        with migration._checkpoint_transaction(self.run, self.started):
            self.run.save()
        self.db.commit.assert_called_once()
        self.assertEqual(self.assert_contract.call_count, 2)

    def test_changed_contract_rolls_back_without_a_checkpoint_commit(self):
        self.assert_contract.side_effect = [None, migration.MappingPlanError('changed')]
        with self.assertRaises(migration.MappingPlanError):
            with migration._checkpoint_transaction(self.run, self.started): pass
        self.db.commit.assert_not_called()
        self.assertEqual(self.db.rollback.call_count, 2)
        self.run.save.assert_not_called()

    def test_interruption_rolls_back_inflight_work_without_advancing_checkpoint(self):
        with self.assertRaises(KeyboardInterrupt):
            with migration._checkpoint_transaction(self.run, self.started):
                raise KeyboardInterrupt()
        self.assertEqual(self.db.rollback.call_count, 2)
        self.db.commit.assert_not_called()
        self.run.save.assert_not_called()
        self.assert_contract.assert_called_once_with(self.run, lock=True)

    def test_checkpoint_save_failure_rolls_back_and_never_commits(self):
        self.run.save.side_effect = RuntimeError('synthetic checkpoint save failure')
        with self.assertRaisesRegex(RuntimeError, 'checkpoint save failure'):
            with migration._checkpoint_transaction(self.run, self.started):
                pass
        self.assertEqual(self.assert_contract.call_count, 2)
        self.run.save.assert_called_once()
        self.db.commit.assert_not_called()
        self.assertEqual(self.db.rollback.call_count, 2)

    def test_replaced_cancelled_expired_or_foreign_claim_cannot_commit(self):
        changes = ({'started_on': self.started + timedelta(seconds=1)}, {'state': 'Cancelled'},
                   {'lease_expires_on': self.started}, {'owner': 'Other'})
        for values in changes:
            saved = vars(self.current).copy()
            for key, value in values.items(): setattr(self.current, key, value)
            with self.subTest(values=values), self.assertRaises(migration.MappingPlanError):
                with migration._checkpoint_transaction(self.run, self.started): pass
            vars(self.current).update(saved)
        self.db.commit.assert_not_called()


class TestReaderTransportFence(TestCase):
    def test_automatic_reader_requests_rollback_and_revalidates_around_transport(self):
        from hero_integration.services.read_contract import ReadContract

        events = []
        contract = ReadContract('service', 7, 'mapping', 3, 'HeroCustomers', 'contacts', 'id',
                                ('rule',), {'first': '$page_size', 'offset': '$offset'}, 2)
        service = SimpleNamespace(schema_snapshot='snapshot', query_document='synthetic query')
        snapshot = SimpleNamespace(source_instance='synthetic')
        documents = {'HERO GraphQL Service': service, 'HERO GraphQL Mapping': object(),
                     'HERO GraphQL Schema Snapshot': snapshot}
        fake = SimpleNamespace(get_doc=lambda doctype, name: documents[doctype],
                               db=SimpleNamespace(rollback=lambda: events.append('rollback')))
        client = SimpleNamespace(query=lambda document, variables: events.append('query') or {'contacts': []})
        run = SimpleNamespace(graphql_service='service', graphql_mapping='mapping', service_revision=7,
                              mapping_revision=3, mode='Execute', testing_enabled=0)
        config = SimpleNamespace(endpoint=object(), api_token='synthetic', timeout_seconds=5,
                                 source_instance='synthetic')
        with mock.patch.object(migration, 'frappe', fake), \
             mock.patch.object(migration, '_validate_run_source_binding'), \
             mock.patch.object(migration, '_assert_checkpoint_contract', side_effect=lambda run: events.append('check')), \
             mock.patch.object(migration, '_request_rate', return_value=10000), \
             mock.patch.object(migration, 'HeroGraphQLClient', return_value=client), \
             mock.patch('hero_integration.services.read_contract.from_documents', return_value=contract):
            source = migration._source_for_run(run, config)
            self.assertEqual(source.customers().records, ())
        self.assertEqual(events, ['rollback', 'check', 'rollback', 'query', 'rollback', 'check', 'rollback'])


class TestLegacyCheckpoint(TestCase):
    def test_absent_malformed_or_unsupported_evidence_never_gets_rebound(self):
        for raw in (None, '', '{}', '{not-json', '{"version":0}'):
            with self.subTest(raw=raw), mock.patch.object(migration, '_run_checkpoint_contract') as rebuild:
                with self.assertRaises(migration.MappingPlanError):
                    migration._assert_checkpoint_contract(SimpleNamespace(checkpoint_contract_json=raw))
                rebuild.assert_not_called()

    def test_each_relevant_contract_dimension_invalidates_a_checkpoint(self):
        bound = dict(version=1, site='site', source_instance='a', source_binding_revision=1,
                     actor='Administrator', action='Customer Migration', mode='Execute',
                     readers=[dict(reader='query', schema='snapshot', field_rules='rules')])
        run = SimpleNamespace(checkpoint_contract_json=json.dumps(bound))
        for key in ('site', 'source_instance', 'source_binding_revision', 'actor', 'action', 'mode', 'readers'):
            current = {**bound, key: 'changed'}
            with self.subTest(key=key), mock.patch.object(migration, '_run_checkpoint_contract', return_value=current):
                with self.assertRaises(migration.MappingPlanError): migration._assert_checkpoint_contract(run)

    def test_an_old_site_schema_cannot_silently_drop_the_queued_contract(self):
        from hero_integration import api
        fake = SimpleNamespace(get_meta=lambda _: SimpleNamespace(has_field=lambda _: False),
                               throw=lambda message: (_ for _ in ()).throw(ValueError(message)))
        with mock.patch.object(api, 'frappe', fake), mock.patch.object(api, '_approved_read_service') as reader:
            with self.assertRaisesRegex(ValueError, 'checkpoint schema'):
                api._start_migration('Customer Migration', True, False, 'SERVICE')
            reader.assert_not_called()


class TestReplayAcceptance(TestCase):
    def test_resume_replays_from_zero_without_rebinding_the_acknowledged_contract(self):
        from hero_integration import api
        run = SimpleNamespace(name='RUN', state='Failed', testing_enabled=0, checkpoint_contract_json='original',
                              resume_phase='project', resume_offset=100, resume_record_index=3,
                              error_message='interrupted', save=mock.Mock())
        fake = SimpleNamespace(only_for=lambda _: None, get_doc=lambda *a: run, enqueue=mock.Mock(),
                               local=SimpleNamespace(site='site'))
        with mock.patch.object(api, 'frappe', fake), mock.patch.object(migration, 'validate_run_mappings'), \
             mock.patch.object(migration, '_assert_checkpoint_contract') as verify, \
             mock.patch.object(migration, 'bind_run_checkpoint') as rebind:
            api.resume_run('RUN')
        verify.assert_called_once_with(run, lock=True)
        rebind.assert_not_called()
        self.assertEqual((run.resume_offset, run.resume_record_index, run.resume_phase), (0, 0, ''))
        self.assertEqual(run.checkpoint_contract_json, 'original')
        fake.enqueue.assert_called_once()

    def test_execute_never_turns_a_durably_blocked_phase_into_success(self):
        run = SimpleNamespace(name='RUN', state='Queued', owner='Administrator', started_on=datetime(2026, 1, 1),
                              reload=lambda: None)
        fake = SimpleNamespace(get_doc=lambda *a, **kw: run, set_user=lambda _: None)
        def process(*args, **kwargs): run.state = 'Blocked'
        with mock.patch.object(migration, 'frappe', fake), \
             mock.patch.object(migration, '_claim_run', return_value=True), \
             mock.patch.object(migration, '_assert_checkpoint_contract'), \
             mock.patch.object(migration, 'validate_run_mappings'), \
             mock.patch.object(migration, 'get_configuration'), \
             mock.patch.object(migration, '_source_for_run'), \
             mock.patch.object(migration, '_process_requested_phases', side_effect=process), \
             mock.patch.object(migration, '_locked_active_run', return_value='RUN'), \
             mock.patch.object(migration, '_release_run'), \
             mock.patch.object(migration, '_mark_sync_success') as success:
            migration.execute_run('RUN')
        success.assert_not_called()
        self.assertEqual(run.state, 'Blocked')


class TestRunLeaseStatus(TestCase):
    def test_terminal_running_and_unexpected_queued_slot_states(self):
        now = datetime(2026, 1, 1, 12)
        fake = SimpleNamespace(db=SimpleNamespace(get_value=mock.Mock()))
        cases = (
            (None, False),
            (SimpleNamespace(state='Completed', lease_expires_on=None, started_on=None), False),
            (SimpleNamespace(state='Queued', lease_expires_on=None, started_on=None), True),
            (SimpleNamespace(state='Running', lease_expires_on=now + timedelta(seconds=1), started_on=None), True),
            (SimpleNamespace(state='Running', lease_expires_on=now, started_on=None), False),
            (SimpleNamespace(state='Running', lease_expires_on=None, started_on=None), False),
            (SimpleNamespace(state='Running', lease_expires_on=None,
                             started_on=now - timedelta(seconds=migration.RUN_LEASE_SECONDS - 1)), True),
            (SimpleNamespace(state='Running', lease_expires_on=None,
                             started_on=now - timedelta(seconds=migration.RUN_LEASE_SECONDS)), False),
        )
        with mock.patch.object(migration, 'frappe', fake):
            for row, expected in cases:
                with self.subTest(row=row):
                    fake.db.get_value.return_value = row
                    self.assertEqual(migration.run_lease_alive('RUN', now), expected)


class TestFailureFinalizationOwnership(TestCase):
    def test_stale_worker_cannot_finalize_another_or_newer_claim(self):
        old = datetime(2026, 1, 1, 10, 0)
        newer = datetime(2026, 1, 1, 10, 1)
        run = SimpleNamespace(state='Running', started_on=newer, save=mock.Mock())
        fake = SimpleNamespace(get_doc=mock.Mock(return_value=run), db=SimpleNamespace(commit=mock.Mock()))
        with mock.patch.object(migration, 'frappe', fake), \
             mock.patch.object(migration, '_locked_active_run', return_value='OTHER'), \
             mock.patch.object(migration, '_release_run') as release:
            migration._finalize_failed_run('RUN', old)
            fake.get_doc.assert_not_called()
            release.assert_not_called()
        with mock.patch.object(migration, 'frappe', fake), \
             mock.patch.object(migration, '_locked_active_run', return_value='RUN'), \
             mock.patch.object(migration, '_release_run') as release:
            migration._finalize_failed_run('RUN', old)
            run.save.assert_not_called()
            release.assert_not_called()
        fake.db.commit.assert_not_called()


class TestEnumerationEvidence(TestCase):
    def test_invalid_page_cannot_partially_advance_seen_identity_evidence(self):
        from hero_integration.sources.hero import SourcePage

        cases = (
            SourcePage(({'id': 2}, {'id': 1}), None),
            SourcePage(({'id': 2}, {'id': None}), None),
            SourcePage(({'id': 2}, {'id': '2'}), None),
            SourcePage(({'id': 2},), 0),
        )
        for page in cases:
            seen = {'1'}
            with self.subTest(page=page), self.assertRaises(migration.MappingPlanError):
                migration._validate_enumeration_page(page, 0, seen)
            self.assertEqual(seen, {'1'})

    def test_valid_pages_advance_identity_evidence_after_validation(self):
        from hero_integration.sources.hero import SourcePage

        seen = {'1'}
        migration._validate_enumeration_page(SourcePage(({'id': 2}, {'id': '003'}), 2), 0, seen)
        self.assertEqual(seen, {'1', '2', '003'})
