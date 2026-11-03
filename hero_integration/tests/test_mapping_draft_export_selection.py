"""The editor's draft export excludes unrelated active definitions and foreign drafts."""
import json
import unittest
from unittest.mock import patch

from hero_integration import api
from hero_integration.services import mapping_draft_exchange as exchange
from hero_integration.tests import test_portable_draft_exchange as portable

Fake, IDENTITY = portable.Fake, portable.IDENTITY


class TestDraftOnlyExport(unittest.TestCase):
    def setUp(self):
        self.fake = Fake('site', 'source', 'snapshot')
        self.content = {'records_path': 'customers', 'identity_path': 'id', 'mapping_definition_ids': []}
        self.fake.active.append({'name': 'ACTIVE', 'definition_id': 'legacy', 'portable_definition_id': '',
            'source_instance': 'source', 'schema_snapshot': 'snapshot',
            'schema_fingerprint': self.fake.snapshot.schema_fingerprint, 'direction': 'QUERY_TO_FRAPPE',
            'operation_kind': 'QUERY', 'operation_name': 'customers', 'status': 'Active',
            'mapping_revision': 2, 'mapping_json': json.dumps(self.content)})

    def export(self, **kwargs):
        return exchange.export_package('source', 'snapshot', 'QUERY_TO_FRAPPE',
                                       format_version=exchange.PORTABLE_FORMAT_VERSION, **kwargs)

    def test_drafts_only_skips_unrelated_active_parent_but_default_preserves_validation(self):
        with portable.TestPortableDraftExchange().context(self.fake):
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'No portable parentless'):
                self.export(drafts_only=True)
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'Enroll a portable UUID'):
                self.export()

    def test_current_actor_parentless_draft_exports_despite_unrelated_active_parent(self):
        definition = {'site_scope': 'site', 'definition_id': IDENTITY, 'source_instance': 'source',
            'snapshot_name': 'snapshot', 'schema_fingerprint': self.fake.snapshot.schema_fingerprint,
            'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers'}
        with portable.TestPortableDraftExchange().context(self.fake):
            exchange.FrappeMappingDraftStorage().service().open_new_draft(definition, self.fake.session.user, self.content)
            result = json.loads(self.export(drafts_only=True))
            self.assertEqual([item['definition_id'] for item in result['mappings']], [IDENTITY])
            self.fake.session.user = 'another-admin@example.invalid'
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'No portable parentless'):
                self.export(drafts_only=True)

    def test_own_existing_parent_is_included_only_when_open_draft_exists(self):
        self.fake.active[0]['portable_definition_id'] = IDENTITY
        with portable.TestPortableDraftExchange().context(self.fake):
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'No portable parentless'):
                self.export(drafts_only=True)
            exchange.FrappeMappingDraftStorage().service().open_draft('legacy', self.fake.session.user)
            result = json.loads(self.export(drafts_only=True))
            self.assertEqual([item['definition_id'] for item in result['mappings']], [IDENTITY])

    def test_service_rejects_non_boolean_selection(self):
        for invalid in (None, 'false', 0, 1, [], {}):
            with self.subTest(value=invalid), self.assertRaises(exchange.MappingDraftExchangeError):
                self.export(drafts_only=invalid)

    def test_api_boolean_is_explicit_and_forwarded(self):
        def fail(message, *args):
            raise ValueError(message)
        self.fake.throw = fail
        self.fake.form_dict = {}
        with patch.object(api, 'frappe', self.fake), patch.object(exchange, 'export_package', return_value='{}') as export:
            for value, expected in ((False, False), (True, True), (0, False), (1, True), ('false', False), ('true', True)):
                api.mapping_draft_exchange_export('source', 'snapshot', 'QUERY_TO_FRAPPE', drafts_only=value)
                self.assertIs(export.call_args.kwargs['drafts_only'], expected)
            export.reset_mock()
            for invalid in ('yes', '', None, 2, 1.0, [], {}):
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    api.mapping_draft_exchange_export('source', 'snapshot', 'QUERY_TO_FRAPPE', drafts_only=invalid)
            export.assert_not_called()


if __name__ == '__main__':
    unittest.main()
