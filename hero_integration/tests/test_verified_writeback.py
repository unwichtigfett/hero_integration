"""Writeback may acknowledge only the verified value of an approved bound field."""
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration import api
from hero_integration.services.binding_registry import BindingState, state_fingerprint
from hero_integration.services.frappe_writeback import WritebackContractError
from hero_integration.services.sync_engine import (
    Eligibility, OutboundApproval, OutboundIntent, SyncConflict, execute_sync_hero, sync_hero_eligibility,
)
from hero_integration.services.writeback_readback import validate_readback, verify_readback
from hero_integration.tests import test_sync_engine as fixtures
from hero_integration.tests.test_sync_engine import Transport, OutboundAck, FP


def context():
    request = NS(protocol_field_name='contacts', document='query Read { contacts { id full_name } }', variables={},
                 result_bindings=[NS(selection_path=('id',)), NS(selection_path=('full_name',))])
    plan = NS(request=request, source_instance='hero', sealed=NS(contract=NS(identity_path='id', records_path='contacts', definition_ids=('d',), mapping_revision=1)))
    value = 'Expected'
    return {'mapping': {'target': {'operation': 'update_contact', 'argument': 'contact', 'field': 'full_name'}},
            'parent': NS(object_type='Contact', source_instance='hero', external_object_id='42'),
            'binding': NS(target_doctype='Customer', source_field='full_name', acknowledged_source_fingerprint=None, mapping_definition_id='d', mapping_revision=1), 'readback_plan': plan,
            'intent': NS(local_fingerprint=state_fingerprint(value, present=True))}


class TestVerifiedWriteback(TestCase):
    def test_preflight_rejects_wrong_source_type_field_or_missing_selection(self):
        for change in ('type', 'source', 'path', 'selection'):
            c = context()
            if change == 'type': c['parent'].object_type = 'ProjectMatch'
            if change == 'source': c['readback_plan'].source_instance = 'other'
            if change == 'path': c['binding'].source_field = 'email'
            if change == 'selection': c['readback_plan'].request.result_bindings.pop()
            with self.subTest(change=change), self.assertRaises(WritebackContractError):
                validate_readback(c, c['readback_plan'])

    def test_id_only_response_cannot_acknowledge_a_write(self):
        c = context()
        for rows in ([{'id': '42'}], [{'id': '42', 'full_name': 'Old'}], [],
                     [{'id': '7', 'full_name': 'Expected'}],
                     [{'id': '42', 'full_name': 'Expected'}] * 2):
            with self.subTest(rows=rows), self.assertRaises(WritebackContractError):
                verify_readback(c, Mock(query=Mock(return_value={'contacts': rows})),
                                {'contact': {'id': 42, 'full_name': 'Expected'}})

    def test_matching_value_hashes_the_canonical_field_and_uses_exact_approved_request(self):
        c = context()
        reader = Mock(query=Mock(return_value={'contacts': [{'id': '42', 'full_name': 'Expected'}]}))
        result = verify_readback(c, reader, {'contact': {'id': 42, 'full_name': 'Expected'}})
        self.assertEqual(result, state_fingerprint('Expected', present=True))
        reader.query.assert_called_once_with(c['readback_plan'].request.document, {})

    def test_null_false_and_zero_are_distinct_and_errors_are_not_success(self):
        c = context()
        for response in ({'contacts': [{'id': '42', 'full_name': None}]},
                         {'contacts': [{'id': '42', 'full_name': False}]},
                         {'errors': [{'message': 'redacted'}], 'data': {'contacts': []}}):
            with self.subTest(response=response), self.assertRaises(WritebackContractError):
                verify_readback(c, Mock(query=Mock(return_value=response)), {'contact': {'full_name': 0}})

    def test_transport_requires_prepared_readback_before_any_mutation(self):
        c = context(); del c['readback_plan']
        with patch('hero_integration.client.graphql_mutation.HeroMutationClient') as mutation:
            with self.assertRaises(RuntimeError):
                api._SyncHeroTransport(c, NS(correlation_id='audit')).execute_once('mutation', {})
        mutation.assert_not_called()

    def test_transport_preserves_binding_when_readback_fails_and_never_retries_mutation(self):
        c = context()
        config = NS(endpoint='https://example.invalid/graphql', api_token='synthetic', timeout_seconds=1)
        with patch('hero_integration.client.settings.get_configuration', return_value=config), \
                patch('hero_integration.client.graphql_mutation.HeroMutationClient') as mutation, \
                patch('hero_integration.client.graphql.HeroGraphQLClient') as read:
            mutation.return_value.execute_once.return_value = {'update_contact': {'id': '42'}}
            read.return_value.query.return_value = {'contacts': [{'id': '42', 'full_name': 'Old'}]}
            with self.assertRaises(WritebackContractError):
                api._SyncHeroTransport(c, NS(correlation_id='audit')).execute_once('mutation', {'contact': {'full_name': 'Expected'}})
            mutation.return_value.execute_once.assert_called_once()


    def test_transport_checks_remote_baseline_before_mutation_and_hashes_verified_value(self):
        for remote_before, baseline, succeeds in (
            ('Old', state_fingerprint('Different', present=True), False),
            ('Old', state_fingerprint('Old', present=True), True),
        ):
            c = context(); c['binding'].acknowledged_source_fingerprint = baseline
            config = NS(endpoint='https://example.invalid/graphql', api_token='synthetic', timeout_seconds=1)
            with patch('hero_integration.client.settings.get_configuration', return_value=config), \
                    patch('hero_integration.client.graphql_mutation.HeroMutationClient') as mutation, \
                    patch('hero_integration.client.graphql.HeroGraphQLClient') as read:
                mutation.return_value.execute_once.return_value = {'update_contact': {'id': '42'}}
                read.return_value.query.side_effect = [
                    {'contacts': [{'id': '42', 'full_name': remote_before}]},
                    {'contacts': [{'id': '42', 'full_name': 'Expected'}]},
                ]
                transport = api._SyncHeroTransport(c, NS(correlation_id='audit'))
                if succeeds:
                    ack = transport.execute_once('mutation', {'contact': {'full_name': 'Expected'}})
                    self.assertTrue(ack.verified)
                    self.assertEqual(ack.source_fingerprint, state_fingerprint('Expected', present=True))
                    self.assertEqual(ack.target_fingerprint, c['intent'].local_fingerprint)
                    mutation.return_value.execute_once.assert_called_once()
                    self.assertEqual(read.return_value.query.call_count, 2)
                else:
                    with self.assertRaises(RuntimeError):
                        transport.execute_once('mutation', {'contact': {'full_name': 'Expected'}})
                    mutation.assert_not_called()

    def test_missing_bound_record_in_approved_page_blocks_before_mutation(self):
        c = context()
        config = NS(endpoint='https://example.invalid/graphql', api_token='synthetic', timeout_seconds=1)
        with patch('hero_integration.client.settings.get_configuration', return_value=config), \
                patch('hero_integration.client.graphql_mutation.HeroMutationClient') as mutation, \
                patch('hero_integration.client.graphql.HeroGraphQLClient') as read:
            read.return_value.query.return_value = {'contacts': []}
            with self.assertRaises(WritebackContractError):
                api._SyncHeroTransport(c, NS(correlation_id='audit')).execute_once('mutation', {})
            mutation.assert_not_called()


class TestOutboundConflictGate(TestCase):
    def test_conflicts_and_revision_changes_never_reach_transport(self):
        fixture = fixtures.TestSync(); fixture.setUp()
        for state in (BindingState.RECONCILIATION_REQUIRED, BindingState.MAPPING_CHANGED, BindingState.REMOTE_CHANGED):
            binding = replace(fixture.binding, state=state)
            intent = OutboundIntent(binding, 1, FP, 'new', True, 'mutation', {})
            transport = Transport(OutboundAck(True, 'e', 'a'*64, 'b'*64))
            with self.subTest(state=state), self.assertRaises(SyncConflict):
                execute_sync_hero(intent, OutboundApproval(True, True, True, True, True, 1), transport, fixture.bindings)
            self.assertEqual(transport.calls, 0)
            self.assertIsNot(sync_hero_eligibility(intent), Eligibility.LOCAL_CHANGED)

    def test_api_conflict_preview_is_ineligible_and_execute_creates_no_audit(self):
        fixture = fixtures.TestSync(); fixture.setUp()
        for state in (BindingState.RECONCILIATION_REQUIRED, BindingState.MAPPING_CHANGED, BindingState.REMOTE_CHANGED):
            binding = replace(fixture.binding, state=state)
            intent = OutboundIntent(binding, 1, FP, 'new', True, 'mutation', {})
            c = {'binding': binding, 'intent': intent, 'fingerprint': 'f', 'executable': NS(generation=1)}
            fake = NS(only_for=lambda role: None, _=lambda text: text,
                      throw=lambda text: (_ for _ in ()).throw(ValueError(text)))
            with patch.object(api, 'frappe', fake), patch.object(api, '_sync_hero_context', return_value=c), \
                    patch.object(api, '_reserve_mutation') as reserve, patch.object(api, '_SyncHeroTransport') as transport:
                self.assertFalse(api.sync_hero_preview('b', 'm', 1)['eligible'])
                with self.assertRaises(ValueError):
                    api.sync_hero_execute('b', 'm', 1, binding.revision, 'f', 1)
                reserve.assert_not_called()
                transport.assert_not_called()

    def test_acknowledgment_for_a_different_local_state_is_rejected(self):
        fixture = fixtures.TestSync(); fixture.setUp()
        intent = OutboundIntent(fixture.binding, 1, FP, 'a'*64, True, 'mutation', {})
        transport = Transport(OutboundAck(True, 'e', 'c'*64, 'b'*64))
        with self.assertRaises(SyncConflict):
            execute_sync_hero(intent, OutboundApproval(True, True, True, True, True, 1), transport, fixture.bindings)
        self.assertEqual(fixture.bindings.get_field_bindings('site', fixture.binding.object_binding_key), (fixture.binding,))
