"""Employee field proposals preserve identities and avoid account writes."""
import unittest

from hero_integration.services.employee_mapping import mapping_contract, preview, OBSERVATIONS


class TestEmployeeMapping(unittest.TestCase):
    def setUp(self):
        self.record = {
            'id': 101, 'user_id': 202, 'first_name': 'Synthetic', 'last_name': 'Employee',
            'full_name': 'Synthetic Employee', 'email': 'contact@example.invalid',
            'phone': '+490000000', 'mobile': '+490000001', 'role': None,
            'status': 'active', 'account_type': 'full',
            'user': {'id': 202, 'email': 'login@example.invalid', 'role': 'partner',
                     'locale': 'de_DE', 'last_login': '2026-01-02T10:00:00+00:00',
                     'created': '2020-01-01T10:00:00+00:00', 'modified': '2026-01-02T10:00:00+00:00'},
        }

    def test_visible_field_labels_have_no_integration_prefix(self):
        for fields in OBSERVATIONS.values():
            for fieldname, label in fields:
                self.assertTrue(fieldname.startswith('custom_hero_'))
                self.assertNotIn('hero', label.lower())
                self.assertNotIn('custom_', label.lower())

    def test_all_eighteen_source_fields_have_explicit_targets(self):
        result = preview(self.record, languages={'de'})
        self.assertEqual(len(result['fields']), 18)
        self.assertEqual(result['blockers'], [])
        fields = {f['source_path']: f for f in result['fields']}
        self.assertEqual(fields['email']['value'], 'contact@example.invalid')
        self.assertEqual(fields['user.email']['value'], 'login@example.invalid')
        self.assertEqual(fields['user.locale']['value'], 'de')
        self.assertEqual(fields['status']['value'], 'Active')
        self.assertEqual(fields['full_name']['target_field'], 'employee_name')
        self.assertIsNone(fields['role']['value'])

    def test_no_duplicate_contact_or_name_fields_or_security_targets(self):
        fields = mapping_contract()['fields']
        for f in fields:
            self.assertNotIn(f['target_field'], {'user_id', 'creation', 'modified', 'last_login', 'enabled', 'roles', 'user_type'})
            if f['source_path'] in {'first_name', 'last_name', 'full_name', 'email', 'phone', 'mobile', 'user.email', 'user.locale'}:
                self.assertFalse(f['target_field'].startswith('custom_'))
        self.assertTrue(preview(self.record, languages={'de'})['requires_user_binding'])

    def test_user_identity_disagreement_blocks(self):
        self.record['user']['id'] = 303
        self.assertIn('HERO employee user_id differs from user.id.', preview(self.record, languages={'de'})['blockers'])

    def test_missing_fields_are_omitted_and_null_remains_explicit(self):
        fields = preview({'id': 1, 'role': None})['fields']
        self.assertEqual([f['source_path'] for f in fields], ['id', 'role'])
        self.assertIsNone(fields[1]['value'])
        self.assertFalse(preview({'id': 1})['requires_user_binding'])

    def test_unknown_status_language_and_inconsistent_name_require_review(self):
        self.record.update(status='unknown', full_name='Different name')
        result = preview(self.record, languages={'en'})
        self.assertEqual(len(result['blockers']), 3)

    def test_invalid_identity_and_text_types_are_rejected(self):
        for invalid in [True, 0, -1, '101', None]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                preview({'id': invalid})
        with self.assertRaises(ValueError):
            preview({'id': 1, 'email': []})
