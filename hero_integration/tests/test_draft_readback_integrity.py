"""Offline readback tests for the personal draft API boundary."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hero_integration import api
from hero_integration.services.mapping_draft_service import _hash, draft_key


class Document(SimpleNamespace):
    def get(self, name):
        return getattr(self, name, None)


class TestDraftReadbackIntegrity(unittest.TestCase):
    def setUp(self):
        self.key = draft_key('site', 'definition', 'alice')
        content = {'source': {'field': 'name'}}
        self.doc = Document(draft_key=self.key, site_scope='site', definition_id='definition', owner_user='alice',
                            generation=1, state='OPEN', content_json=json.dumps(content), content_hash=_hash(content),
                            content_revision=1, base_active_mapping='active', base_active_revision=2,
                            base_active_hash='base', base_schema_fingerprint='schema', base_content_json='',
                            new_definition_json='', reconciliation_revision=0, reconciliation_json='',
                            warning_fingerprint='', diagnostics_json='')
        self.doc.check_permission = Mock()
        self.frappe = SimpleNamespace(_=lambda message: message, local=SimpleNamespace(site='site'), session=SimpleNamespace(user='alice'),
                                      form_dict={}, db=SimpleNamespace(exists=Mock(return_value=True)),
                                      only_for=Mock(), get_doc=Mock(return_value=self.doc),
                                      PermissionError=PermissionError, TimestampMismatchError=RuntimeError,
                                      throw=lambda message, exc=ValueError: (_ for _ in ()).throw(exc(message)))

    def read(self):
        with patch.object(api, 'frappe', self.frappe):
            return api.mapping_draft_read('definition')

    def test_read_returns_owned_unchanged_content_without_writes(self):
        result = self.read()['draft']
        self.assertEqual(result['content'], {'source': {'field': 'name'}})
        self.assertEqual(result['content_hash'], self.doc.content_hash)
        self.assertEqual(result['content_revision'], 1)
        self.doc.check_permission.assert_called_once_with('read')

    def test_loaded_foreign_or_wrong_context_does_not_leak_content(self):
        for field, value in [('owner_user', 'bob'), ('site_scope', 'other'),
                             ('definition_id', 'other'), ('draft_key', 'other')]:
            old = getattr(self.doc, field)
            setattr(self.doc, field, value)
            with self.subTest(field=field), self.assertRaises(PermissionError):
                self.read()
            setattr(self.doc, field, old)

    def test_changed_content_or_hash_is_rejected_before_readback(self):
        self.doc.content_json = '{"source":{"field":"tampered"}}'
        with self.assertRaisesRegex(RuntimeError, 'MAPPING_DRAFT_CONFLICT'):
            self.read()

    def test_duplicate_content_keys_are_rejected(self):
        self.doc.content_json = '{"source":{},"source":{}}'
        with self.assertRaises(ValueError):
            self.read()
