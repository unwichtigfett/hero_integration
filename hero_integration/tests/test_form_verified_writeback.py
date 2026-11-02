"""Form writeback uses the real mutation response shape and verified transport."""
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration import api
from hero_integration.services.binding_registry import state_fingerprint, BindingRegistryService, InMemoryBindingStorage
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.sync_engine import OutboundIntent
from hero_integration.tests import test_sync_engine as fixtures
from hero_integration.tests.test_verified_writeback import configuration, context


class TestFormVerifiedWriteback(TestCase):
    def make_context(self):
        bindings = BindingRegistryService(InMemoryBindingStorage())
        parent = bindings.create_or_resolve_object_binding(site_scope='site', source_instance='hero',
            object_type='Contact', external_object_id='42', target_app='erpnext', target_doctype='Customer',
            target_name='C1', mapping_definition_id='d', mapping_revision=1)
        binding = bindings.create_or_resolve_field_binding(parent, source_field='full_name', target_field='customer_name',
            mapping_definition_id='d', mapping_revision=1, direction=MappingDirection.HERO_TO_FRAPPE)
        fixture = NS(bindings=bindings, binding=binding)
        verified = context()
        verified['binding'] = fixture.binding
        verified['bindings'] = fixture.bindings
        verified['intent'] = OutboundIntent(fixture.binding, 1, fixtures.FP,
            state_fingerprint('Expected', present=True), True, 'mutation', {'contact': {'full_name': 'Expected'}})
        verified['fingerprint'] = 'shared'
        form = {'verified_context': verified, 'fingerprint': 'preview', 'mapping': verified['mapping'],
                'mapping_row': NS(mapping_revision=1), 'snapshot': NS(schema_fingerprint=fixtures.FP)}
        return form, fixture

    def execute(self, form, *, configuration_values=None, read_values=None, mutation=None):
        fake = NS(only_for=lambda role: None, has_permission=lambda *a, **kw: True,
                  get_doc=lambda *a: NS(), _=lambda text: text,
                  throw=lambda text: (_ for _ in ()).throw(ValueError(text)))
        self.reserve, self.finalize = Mock(return_value=NS(correlation_id='audit')), Mock()
        self.mutation, self.read = Mock(), Mock()
        # HeroMutationClient returns the already-unwrapped data object.
        self.mutation.return_value.execute_once.return_value = {'update_contact': {'id': '42'}}
        if mutation: self.mutation.return_value.execute_once.side_effect = mutation
        self.read.return_value.query.side_effect = read_values or [
            {'contacts': [{'id': '42', 'full_name': 'Old'}]},
            {'contacts': [{'id': '42', 'full_name': 'Expected'}]},
        ]
        with ExitStack() as stack:
            for name, value in (('frappe', fake), ('_writeback_context', Mock(return_value=form)),
                                ('_assert_mutation_execution_released', Mock()),
                                ('_reserve_mutation', self.reserve), ('_finalize_mutation', self.finalize)):
                stack.enter_context(patch.object(api, name, value))
            stack.enter_context(patch('hero_integration.client.settings.get_configuration',
                                      side_effect=configuration_values or [configuration()] * 3))
            stack.enter_context(patch('hero_integration.client.graphql_mutation.HeroMutationClient', self.mutation))
            stack.enter_context(patch('hero_integration.client.graphql.HeroGraphQLClient', self.read))
            return api.writeback_execute('m', 'Customer', 'C1', 'preview', 1)

    def test_success_requires_matching_field_readback_and_marks_audit_succeeded(self):
        form, fixture = self.make_context()
        result = self.execute(form)
        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertEqual(self.finalize.call_args.args[1], 'Succeeded')
        self.mutation.return_value.execute_once.assert_called_once()
        self.assertEqual(self.read.return_value.query.call_count, 2)
        updated = fixture.bindings.get_field_bindings('site', fixture.binding.object_binding_key)[0]
        self.assertEqual(updated.acknowledged_source_fingerprint, state_fingerprint('Expected', present=True))
        self.assertEqual(updated.revision, 2)

    def test_wrong_or_unconfirmed_source_blocks_all_network_access(self):
        for config in (configuration(source_instance='other'), configuration(source_binding_confirmed=False),
                       configuration(source_binding_revision=8)):
            form, fixture = self.make_context()
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.execute(form, configuration_values=[config])
            self.mutation.assert_not_called(); self.read.assert_not_called()
            self.assertEqual(self.finalize.call_args.args[1], 'Failed')
            self.assertEqual(fixture.bindings.get_field_bindings('site', fixture.binding.object_binding_key), (fixture.binding,))

    def test_connection_switch_between_pre_read_and_mutation_blocks_write(self):
        form, _ = self.make_context()
        with self.assertRaises(ValueError):
            self.execute(form, configuration_values=[configuration(), configuration(source_binding_revision=8)])
        self.assertEqual(self.read.return_value.query.call_count, 1)
        self.mutation.assert_not_called()

    def test_unstored_value_and_uncertain_timeout_never_acknowledge_or_retry(self):
        for timeout in (False, True):
            form, fixture = self.make_context()
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.execute(form, read_values=[{'contacts': [{'id': '42', 'full_name': 'Old'}]}] * 2,
                             mutation=TimeoutError() if timeout else None)
            self.mutation.return_value.execute_once.assert_called_once()
            self.assertEqual(self.finalize.call_args.args[1], 'Timeout' if timeout else 'Failed')
            self.assertEqual(fixture.bindings.get_field_bindings('site', fixture.binding.object_binding_key), (fixture.binding,))

    def test_missing_runtime_plan_is_blocked_without_transport(self):
        fake = NS(local=NS(site='site'), _=lambda text: text,
                  throw=lambda text: (_ for _ in ()).throw(ValueError(text)))
        with patch.object(api, 'frappe', fake), \
                patch('hero_integration.services.binding_registry.FrappeBindingStorage') as storage, \
                patch('hero_integration.services.runtime_plan.FrappeRuntimePlanStorage') as plans, \
                patch.object(api, '_SyncHeroTransport') as transport:
            storage.return_value.fields_for_source.return_value = []
            plans.return_value.read_scope.return_value = None
            with self.assertRaisesRegex(ValueError, 'field binding'):
                api._form_sync_context('m', NS(doctype='Customer', name='C1'), NS(source_instance='hero', source_id='42'), 'customer_name')
        transport.assert_not_called()
