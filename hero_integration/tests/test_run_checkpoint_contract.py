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
        self.assertTrue(self.db.rollback.called)

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


class TestLegacyCheckpoint(TestCase):
    def test_absent_malformed_or_unsupported_evidence_never_gets_rebound(self):
        for raw in (None, '', '{}', '{not-json', '{"version":0}'):
            with self.subTest(raw=raw), mock.patch.object(migration, '_run_checkpoint_contract') as rebuild:
                with self.assertRaises(migration.MappingPlanError):
                    migration._assert_checkpoint_contract(SimpleNamespace(checkpoint_contract_json=raw))
                rebuild.assert_not_called()

    def test_each_relevant_contract_dimension_invalidates_a_checkpoint(self):
        bound = dict(version=1, site='site', source_instance='a', source_binding_revision=1,
                     readers=[dict(reader='query', schema='snapshot', field_rules='rules')])
        run = SimpleNamespace(checkpoint_contract_json=json.dumps(bound))
        for key in ('site', 'source_instance', 'source_binding_revision', 'readers'):
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
