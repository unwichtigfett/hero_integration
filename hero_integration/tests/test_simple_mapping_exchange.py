"""JSON-only Mutation import retains review, ownership, CAS and atomic publication."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration import api
from hero_integration.services import simple_mapping_exchange as simple
from hero_integration.services.mapping_draft_service import MappingDraftError


class TestSimpleMappingExchange(unittest.TestCase):
    def setUp(self):
        self.fake = SimpleNamespace(local=SimpleNamespace(site='site'), session=SimpleNamespace(user='actor'),
            db=SimpleNamespace(savepoint=Mock(), rollback=Mock()), get_all=Mock(return_value=[]),
            get_doc=Mock(return_value=SimpleNamespace(state='DISCARDED')))
        self.fake.get_all.side_effect = lambda doctype, **kwargs: ['contract'] if doctype == 'HERO Mutation Contract' else []
        self.package = {'mappings': [{'definition_id': 'new', 'operation_name': 'update_contact', 'content': {'source': {'field': 'name'},
            'target': {'field': 'full_name'}, 'transformation': 'Identity'}}], 'schema': {}}
        self.raw = {'can_save': True, 'source_instance': 'configured', 'snapshot_name': 'snapshot', 'preview_fingerprint': 'draft-proof',
            'items': [{'definition_id': 'new', 'state': 'NEW', 'expected': {'generation': 0}}]}
        self.storage = Mock()
        self.storage.service.return_value.open_draft.return_value = SimpleNamespace(generation=1,
            content_revision=1, base_active_revision=0, reconciliation_revision=0)
        self.storage.publish.return_value = {'name': 'ACTIVE', 'revision': 1, 'unchanged': False}
        from contextlib import nullcontext
        patches = [patch.object(simple, '_target_mutations', side_effect=lambda *a, **kw:nullcontext()),
            patch('hero_integration.services.mapping_snapshot_rebase.prepare', return_value={'needs_rebase':False}),
            patch('hero_integration.services.mapping_import_context.resolve', side_effect=lambda name,source,payload=None: {'snapshot_name':name,'payload':payload,'validated_against_snapshot':name}),patch.object(simple, 'frappe', self.fake),
            patch.object(simple, '_context', return_value=(SimpleNamespace(source_instance='configured'), {})),
            patch.object(simple.exchange, '_parse_portable', return_value=self.package),
            patch.object(simple.exchange, 'preview_import', side_effect=lambda *args: deepcopy(self.raw)),
            patch.object(simple.exchange, '_preview_binding', return_value='activation-proof'),
            patch.object(simple.exchange, 'save_import'),
            patch.object(simple, 'FrappeMappingDraftStorage', return_value=self.storage),
            patch('hero_integration.services.frappe_writeback.normalise_mapping', side_effect=lambda value, schema: value)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def preview(self):
        return simple.preview('payload', 'snapshot', simple.DIRECTION)

    def apply(self, **kwargs):
        return simple.apply('payload', 'snapshot', simple.DIRECTION, [],
                            kwargs.pop('fingerprint', 'activation-proof'), **kwargs)

    def test_preview_is_nonwriting_and_carries_explicit_activation_and_no_execution(self):
        result = self.preview()
        self.assertTrue(result['can_apply'])
        self.assertTrue(result['activation'])
        self.assertFalse(result['execution'])
        self.assertEqual(result['draft_preview_fingerprint'], 'draft-proof')
        self.assertEqual(result['preview_fingerprint'], 'activation-proof')
        self.fake.db.savepoint.assert_not_called()
        simple.exchange.save_import.assert_not_called()
        self.storage.publish.assert_not_called()
        simple.exchange.preview_import.assert_called_once_with('payload', 'configured', 'snapshot', simple.DIRECTION)

    def test_unreviewed_target_blocks_before_any_draft_write(self):
        with patch('hero_integration.services.frappe_writeback.normalise_mapping', side_effect=ValueError('unreviewed target')):
            result = self.preview()
            self.assertFalse(result['can_apply'])
            self.assertEqual(result['blockers'][0]['reason'], 'unreviewed target')
            with self.assertRaises(MappingDraftError):
                self.apply(confirmed=True)
        simple.exchange.save_import.assert_not_called()
        self.fake.db.savepoint.assert_not_called()

    def test_absent_or_ambiguous_active_mutation_contract_blocks_before_writes(self):
        for contracts in ([], ['first', 'second']):
            self.fake.get_all.side_effect = lambda doctype, **kwargs: contracts if doctype == 'HERO Mutation Contract' else []
            with self.subTest(contracts=contracts):
                result = self.preview()
                self.assertFalse(result['can_apply'])
                self.assertIn('Mutation Contract', result['blockers'][0]['reason'])
                with self.assertRaises(MappingDraftError):
                    self.apply(confirmed=True)
        simple.exchange.save_import.assert_not_called()
        self.fake.db.savepoint.assert_not_called()
        self.fake.get_all.assert_called_with('HERO Mutation Contract', filters={
            'schema_snapshot': 'snapshot', 'operation_name': 'update_contact', 'status': 'Active'}, pluck='name')

    def test_pending_warning_cannot_be_implicitly_acknowledged(self):
        self.fake.get_all.side_effect = lambda doctype, **kwargs: ['contract'] if doctype == 'HERO Mutation Contract' else [{'warning_fingerprint': 'warning-proof'}]
        self.assertFalse(self.preview()['can_apply'])
        with self.assertRaises(MappingDraftError):
            self.apply(confirmed=True)
        self.storage.publish.assert_not_called()
        self.fake.db.savepoint.assert_not_called()

    def test_schema_creation_is_rejected_before_directional_save(self):
        self.package['schema'] = {'custom_fields': [{'id': 'injected'}]}
        with self.assertRaises(MappingDraftError):
            self.preview()
        simple.exchange.save_import.assert_not_called()

    def test_first_multiple_assignment_publication_is_blocked(self):
        self.package['mappings'][0]['content']['additional_mappings'] = [{'id': 'child'}]
        self.assertFalse(self.preview()['can_apply'])
        self.fake.db.savepoint.assert_not_called()

    def test_confirmation_and_preview_proof_are_required_before_writes(self):
        for kwargs in ({'confirmed': False}, {'confirmed': 1}, {'confirmed': True, 'fingerprint': 'stale'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MappingDraftError):
                self.apply(**kwargs)
        simple.exchange.save_import.assert_not_called()
        self.fake.db.savepoint.assert_not_called()

    def test_publish_uses_imported_revision_normal_publisher_and_no_warning_override(self):
        result = self.apply(confirmed=True)
        self.assertEqual(result['status'], 'IMPORTED_MAPPINGS')
        self.assertFalse(result['execution'])
        simple.exchange.save_import.assert_called_once_with('payload', 'configured', 'snapshot', simple.DIRECTION,
            [], confirmed=True, preview_fingerprint='draft-proof')
        self.storage.publish.assert_called_once_with('new', 'actor', 1, 1, 0, 0, confirmed=True)
        self.fake.db.rollback.assert_not_called()
        self.storage.service.return_value.discard.assert_not_called()

    def test_existing_portable_identity_uses_local_definition_and_discards_only_published_own_draft(self):
        self.raw['items'][0]['local_definition_id'] = 'local-definition'
        self.fake.get_doc.return_value = SimpleNamespace(state='OPEN', generation=3, content_revision=4)
        self.apply(confirmed=True)
        self.assertEqual(self.storage.publish.call_args.args[0], 'local-definition')
        self.storage.service.return_value.discard.assert_called_once_with('local-definition', 'actor', 3, 4, confirmed=True)

    def test_unchanged_active_zero_revision_is_retained_without_publication(self):
        from contextlib import nullcontext
        active=SimpleNamespace(name='ACTIVE',revision=3,content_hash='hash',content={'source':{}})
        draft=SimpleNamespace(generation=2,content_revision=0,base_active_name='ACTIVE',
            base_active_revision=3,base_active_hash='hash',content={'source':{}})
        self.storage.service.return_value.open_draft.return_value=draft
        self.storage.transaction.return_value=nullcontext()
        self.storage.active.return_value=active
        self.storage.initial_content.return_value=active.content
        result=self.apply(confirmed=True)
        self.storage.publish.assert_not_called()
        self.assertTrue(result['mappings'][0]['unchanged'])
        self.storage.service.return_value.discard.assert_called_once_with('new','actor',2,0,confirmed=True)

    def test_any_publication_failure_rolls_back_import_and_earlier_publications(self):
        self.package['mappings'].append({'definition_id': 'other', 'operation_name': 'update_contact', 'content': self.package['mappings'][0]['content']})
        self.raw['items'].append({'definition_id': 'other', 'state': 'NEW', 'expected': {'generation': 0}})
        self.storage.publish.side_effect = [{'name': 'FIRST', 'revision': 1}, MappingDraftError('second fails')]
        with self.assertRaisesRegex(MappingDraftError, 'second fails'):
            self.apply(confirmed=True)
        self.assertEqual(self.storage.publish.call_count, 2)
        self.assertEqual(self.fake.db.rollback.call_count, 2)


class TestSimpleMappingExchangeAPI(unittest.TestCase):
    def test_api_rejects_source_and_actor_spoofing_before_import(self):
        def fail(message, *args):
            raise ValueError(message)
        for extra in ('source_instance', 'actor', 'warning_fingerprint'):
            fake = SimpleNamespace(form_dict={extra: 'spoof'}, throw=fail, _=lambda value: value)
            with patch.object(api, 'frappe', fake), patch.object(simple, 'preview') as preview:
                with self.assertRaisesRegex(ValueError, 'Unsupported request field'):
                    api.simple_mapping_import_preview('{}', 'snapshot', simple.DIRECTION)
                preview.assert_not_called()

    def test_api_translates_exchange_and_schema_failures(self):
        from hero_integration.services.mapping_draft_exchange import MappingDraftExchangeError
        from hero_integration.services.schema_provisioning import SchemaProvisioningError
        def fail(message, *args):
            raise RuntimeError(message)
        fake = SimpleNamespace(form_dict={}, throw=fail, _=lambda value: value)
        for error in (MappingDraftExchangeError('Wrong JSON format.'), SchemaProvisioningError('Invalid schema.')):
            with patch.object(api, 'frappe', fake), patch.object(simple, 'preview', side_effect=error):
                with self.subTest(error=error), self.assertRaisesRegex(RuntimeError, str(error)):
                    api.simple_mapping_import_preview('{}', 'snapshot', simple.DIRECTION)

    def test_query_direction_is_rejected_before_snapshot_access(self):
        with patch.object(api, '_new_mapping_snapshot') as snapshot:
            with self.assertRaisesRegex(MappingDraftError, 'configuration package'):
                simple._context('snapshot', 'QUERY_TO_FRAPPE')
            snapshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()


class TestMutationTargetBinding(unittest.TestCase):
    def setUp(self):
        import json
        self.json = json
        self.events = []
        self.rows = {'mapping': {'name': 'mapping', 'source_instance': 'source', 'schema_snapshot': 'old',
            'direction': simple.DIRECTION, 'operation_kind': 'MUTATION', 'operation_name': 'update_task',
            'mapping_json': '{}', 'status': 'Active', 'mapping_revision': 4},
            'unselected': {'name': 'unselected', 'schema_snapshot': 'old'}}
        self.before = deepcopy(self.rows)
        outer = self
        class Parent(SimpleNamespace):
            def check_permission(self, permission):
                outer.events.append(('permission', permission))
            def save(self):
                outer.events.append(('save', self.schema_snapshot, self.status))
                if outer.reject_activation and self.status == 'Active':
                    raise ValueError('target schema field is unavailable')
                self.mapping_revision += 1
                outer.rows[self.name] = deepcopy(vars(self))
        self.reject_activation = False
        self.points = {}
        def savepoint(name): self.points[name] = deepcopy(self.rows)
        def rollback(*, save_point): self.rows = deepcopy(self.points[save_point])
        self.fake = SimpleNamespace(only_for=Mock(), clear_document_cache=Mock(),
            db=SimpleNamespace(savepoint=savepoint, rollback=rollback), get_all=Mock(return_value=['mapping']),
            get_doc=lambda _dt, name, **kw: Parent(**deepcopy(self.rows[name])))
        self.snapshot = SimpleNamespace(name='current', source_instance='source')
        self.package = {'schema': {}, 'mappings': [{'definition_id': 'portable', 'operation_kind': 'MUTATION',
            'operation_name': 'update_task', 'content': {'read_service': 'foreign-reader'}}]}
        def bind(package, *args): package['mappings'][0]['content']['read_service'] = 'local-reader'
        for p in (patch.object(simple, 'frappe', self.fake),
                  patch.object(simple.exchange, '_parse_portable', return_value=self.package),
                  patch.object(simple.exchange, '_lock_context'),
                  patch.object(simple.exchange, '_bind_mapped_update_readbacks', side_effect=bind)):
            p.start(); self.addCleanup(p.stop)

    def test_preview_revalidates_in_target_and_restores_original_active_definition(self):
        with simple._target_mutations('file', self.snapshot, simple.DIRECTION):
            row = self.rows['mapping']
            self.assertEqual(row['schema_snapshot'], 'current')
            self.assertEqual(row['status'], 'Active')
            self.assertEqual(self.json.loads(row['mapping_json'])['read_service'], 'local-reader')
            self.assertEqual(row['mapping_revision'], 6)
        self.assertEqual(self.rows, self.before)
        self.assertEqual(self.events, [('permission', 'write'), ('save', 'current', 'Draft'), ('save', 'current', 'Active')])

    def test_confirmed_target_binding_keeps_identity_and_unselected_definitions(self):
        with simple._target_mutations('file', self.snapshot, simple.DIRECTION, persist=True):
            pass
        self.assertEqual(self.rows['mapping']['name'], 'mapping')
        self.assertEqual(self.rows['mapping']['schema_snapshot'], 'current')
        self.assertEqual(self.rows['unselected'], self.before['unselected'])

    def test_invalid_target_or_failed_following_preview_restores_old_definition(self):
        self.reject_activation = True
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            with simple._target_mutations('file', self.snapshot, simple.DIRECTION):
                self.fail('must not expose an invalid target')
        self.assertEqual(self.rows, self.before)
        self.reject_activation = False
        with self.assertRaisesRegex(ValueError, 'publication blocker'):
            with simple._target_mutations('file', self.snapshot, simple.DIRECTION):
                raise ValueError('publication blocker')
        self.assertEqual(self.rows, self.before)

    def test_foreign_local_source_is_never_overwritten_by_file(self):
        self.rows['mapping']['source_instance'] = 'another-source'
        with self.assertRaisesRegex(MappingDraftError, 'different target context'):
            with simple._target_mutations('file', self.snapshot, simple.DIRECTION):
                pass
        self.assertEqual(self.events, [])

    def test_configuration_rebinding_preserves_active_content_for_separate_draft_import(self):
        self.rows['mapping']['mapping_json'] = self.json.dumps({'field': 'approved-old-field'})
        def keep_existing(parent, item):
            self.assertEqual(item['content']['read_service'], 'foreign-reader')
            return self.json.loads(parent.mapping_json)
        with simple._target_mutations('file', self.snapshot, simple.DIRECTION, persist=True,
                                      existing_content=keep_existing):
            current = self.json.loads(self.rows['mapping']['mapping_json'])
            self.assertEqual(current['field'], 'approved-old-field')
            self.assertEqual(current['read_service'], 'local-reader')
        self.assertNotIn('field', self.before['mapping']['mapping_json'])
