"""Source proof must fail before clients are constructed or invoked."""
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from hero_integration.client.graphql import HeroEndpoint
from hero_integration.client.settings import HeroConfiguration, HeroConfigurationError, require_confirmed_source_binding
from hero_integration.services.frappe_writeback import WritebackContractError
from hero_integration.services.introspection import endpoint_fingerprint
from hero_integration.services.writeback_source import SourceContext, capture_source, validate_source


class TestWritebackSource(unittest.TestCase):
    def setUp(self):
        self.config = HeroConfiguration(HeroEndpoint('https://example.invalid/graphql', 'example.invalid'),
            'synthetic-not-retained', source_instance='source-a', source_binding_revision=4,
            source_binding_confirmed=True)
        self.snapshot = SimpleNamespace(source_instance='source-a',
            endpoint_fingerprint=endpoint_fingerprint(self.config.endpoint.url, 'source-a'))

    def test_capture_retains_only_nonsecret_exact_source_proof(self):
        context = capture_source(self.snapshot, 'source-a', self.config)
        self.assertEqual(vars(context), {'source_instance': 'source-a', 'binding_revision': 4,
                                        'endpoint_fingerprint': self.snapshot.endpoint_fingerprint})
        self.assertNotIn(self.config.api_token, repr(context))
        self.assertEqual(len(context.fingerprint), 64)
        self.assertIs(validate_source(context, self.config), self.config)
        with self.assertRaises(FrozenInstanceError):
            context.binding_revision = 5

    def test_capture_rejects_foreign_source_even_with_identical_endpoint_and_values(self):
        for config in (replace(self.config, source_instance='source-b'),
                       replace(self.config, source_binding_confirmed=False)):
            with self.subTest(config=config), self.assertRaises(WritebackContractError):
                capture_source(self.snapshot, 'source-a', config)

    def test_snapshot_endpoint_proof_is_required(self):
        for proof in (None, '', 'a' * 64):
            self.snapshot.endpoint_fingerprint = proof
            with self.subTest(proof=proof), self.assertRaises(WritebackContractError):
                capture_source(self.snapshot, 'source-a', self.config)

    def test_same_source_label_cannot_move_endpoint(self):
        config = replace(self.config, endpoint=HeroEndpoint('https://other.invalid/graphql', 'other.invalid'))
        with self.assertRaises(WritebackContractError):
            capture_source(self.snapshot, 'source-a', config)

    def test_every_changed_or_unconfirmed_context_fails_revalidation(self):
        context = capture_source(self.snapshot, 'source-a', self.config)
        changes = ({'source_instance': 'source-b'}, {'source_binding_confirmed': False},
                   {'source_binding_revision': 5},
                   {'endpoint': HeroEndpoint('https://other.invalid/graphql', 'other.invalid')})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(WritebackContractError):
                validate_source(context, replace(self.config, **change))

    def test_token_change_reconfirmation_invalidates_old_preview(self):
        original = capture_source(self.snapshot, 'source-a', self.config)
        changed = replace(self.config, api_token='different-synthetic', source_binding_revision=5)
        with self.assertRaises(WritebackContractError):
            validate_source(original, changed)
        renewed = capture_source(self.snapshot, 'source-a', changed)
        self.assertNotEqual(original.fingerprint, renewed.fingerprint)
        self.assertIs(validate_source(renewed, changed), changed)

    def test_configuration_and_context_absence_fail_closed(self):
        for config in (SimpleNamespace(), replace(self.config, source_binding_revision=True),
                       replace(self.config, source_binding_revision=-1),
                       replace(self.config, source_binding_revision='4')):
            with self.subTest(config=config), self.assertRaises(WritebackContractError):
                capture_source(self.snapshot, 'source-a', config)
        for context in (None, {}, 'source-a'):
            with self.subTest(context=context), self.assertRaises(WritebackContractError):
                validate_source(context, self.config)

    def test_helpers_load_current_configuration_each_time(self):
        with patch('hero_integration.client.settings.get_configuration', return_value=self.config) as load:
            context = capture_source(self.snapshot, 'source-a')
            self.assertIs(validate_source(context), self.config)
        self.assertEqual(load.call_args_list, [call(fresh=True), call(fresh=True)])

    def test_shared_confirmation_rule_preserves_legacy_zero_and_rejects_unconfirmed_revision(self):
        require_confirmed_source_binding(0, 0)
        for revision, confirmed in ((4, 3), (True, True), (-1, -1), (4, None)):
            with self.subTest(revision=revision, confirmed=confirmed), self.assertRaises(HeroConfigurationError):
                require_confirmed_source_binding(revision, confirmed)


if __name__ == '__main__':
    unittest.main()
