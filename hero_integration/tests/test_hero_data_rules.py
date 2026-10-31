"""Rules for HERO data that would otherwise block a whole record."""
from __future__ import annotations

import unittest
from unittest import mock

import frappe

from hero_integration.services import migration
from hero_integration.services.mapping_runtime import MappedValues, MappingPlanError
from hero_integration.services.native_projections import EVENT_SUBJECT_FALLBACK, _event_subject


class TestEventSubject(unittest.TestCase):
    def test_a_new_event_without_title_is_named_after_its_type(self):
        self.assertEqual(_event_subject({'title': ' Aufmaß '}, creating=True), 'Aufmaß')
        self.assertEqual(_event_subject({'title': '', 'localized_type': 'Baustellentermin', 'type': 'site'},
                                        creating=True), 'Baustellentermin')
        self.assertEqual(_event_subject({'title': None, 'type': 'site'}, creating=True), 'site')
        self.assertEqual(_event_subject({'title': '  '}, creating=True), EVENT_SUBJECT_FALLBACK)

    def test_an_existing_event_keeps_its_subject_when_the_title_is_empty(self):
        self.assertIsNone(_event_subject({'title': None, 'localized_type': 'Baustellentermin'}, creating=False))


class TestProjectMatchZero(unittest.TestCase):
    def test_only_zero_means_no_project(self):
        self.assertTrue(migration._no_project_match(0))
        for value in (None, 1, -1, False, '0'):
            with self.subTest(value=value):
                self.assertFalse(migration._no_project_match(value))

    def test_zero_resolves_to_no_link_and_negative_still_blocks(self):
        self.assertIsNone(migration._project_name_for_source(0, 'default'))
        with self.assertRaisesRegex(MappingPlanError, 'positive integer'):
            migration._project_name_for_source(-3, 'default')


class TestIncompleteCustomerAddress(unittest.TestCase):
    def test_incomplete_address_is_its_own_skippable_error(self):
        record = {'address': {'id': 5, 'street': 'Hauptstr. 1', 'zipcode': '', 'city': 'Berlin'}}
        with self.assertRaises(migration.IncompleteAddress) as raised:
            migration._customer_primary_address(record)
        self.assertIsInstance(raised.exception, MappingPlanError)

    def test_a_malformed_address_still_blocks(self):
        with self.assertRaises(MappingPlanError) as raised:
            migration._customer_primary_address({'address': {'id': 0, 'street': 'x', 'zipcode': '1', 'city': 'y'}})
        self.assertNotIsInstance(raised.exception, migration.IncompleteAddress)


class TestInvalidCustomerEmail(unittest.TestCase):
    def test_an_email_erpnext_rejects_is_dropped_with_a_note(self):
        values = MappedValues({'customer_name': 'Jörg Schempf', 'email_id': 'Jörg.Schempf@example.com'})
        self.assertIn('not imported', migration._drop_invalid_email(values))
        self.assertNotIn('email_id', values)
        self.assertIn('email_id', values.missing)
        self.assertEqual(values['customer_name'], 'Jörg Schempf')

    def test_valid_or_absent_email_is_kept(self):
        for email in ('joerg.schempf@example.com', None, ''):
            with self.subTest(email=email):
                values = MappedValues({'customer_name': 'Jörg', 'email_id': email})
                self.assertEqual(migration._drop_invalid_email(values), '')
                self.assertIn('email_id', values)


class TestPhoneText(unittest.TestCase):
    def test_the_first_valid_number_is_kept_with_the_full_text(self):
        cases = {
            '+4915500000001 Mieterin': '+4915500000001',
            '07000 11111 ( Architekt)': '07000 11111',
            '015200000001 Frau Muster / 015700000002 Dieter Muster': '015200000001',
            '07000/11111': '07000 11111',
        }
        for text, number in cases.items():
            with self.subTest(text=text):
                self.assertEqual(migration._phone_number(text), (number, text))

    def test_valid_empty_or_numberless_text(self):
        self.assertEqual(migration._phone_number('+49 151 1234567'), ('+49 151 1234567', ''))
        self.assertEqual(migration._phone_number(None), (None, ''))
        self.assertEqual(migration._phone_number('Haus 2, bitte klingeln'), (None, 'Haus 2, bitte klingeln'))

    def test_customer_phones_are_cleaned_and_originals_kept(self):
        values = MappedValues({'customer_name': 'Muster', 'mobile_no': 'nur abends'})
        home, originals, note = migration._clean_customer_phones(values, '07000 11111 (Architekt)')
        self.assertEqual(home, '07000 11111')
        self.assertNotIn('mobile_no', values)
        self.assertIn('mobile_no', values.missing)
        self.assertEqual(originals, {'mobile_no': 'nur abends', 'hero_phone_home': '07000 11111 (Architekt)'})
        self.assertIn('comment', note)

    def test_clean_phones_leave_no_note(self):
        values = MappedValues({'mobile_no': '+49 151 1234567'})
        self.assertEqual(migration._clean_customer_phones(values, None), (None, {}, ''))

    def test_originals_become_customer_comments(self):
        customer = mock.Mock()
        migration._keep_phone_originals(customer, {'mobile_no': '+4915500000001 Mieterin'})
        customer.add_comment.assert_called_once_with(
            'Comment', 'HERO mobile phone as entered in HERO: +4915500000001 Mieterin')


class TestCheckFieldHashes(unittest.TestCase):
    def test_a_check_field_hashes_alike_before_and_after_reload(self):
        from hero_integration.services.change_detection import field_hashes
        self.assertEqual(field_hashes({'flag': False}), field_hashes({'flag': 0}))
        self.assertEqual(field_hashes({'flag': True}), field_hashes({'flag': 1}))
        self.assertNotEqual(field_hashes({'flag': 0}), field_hashes({'flag': None}))


class TestProjectTrades(unittest.TestCase):
    def record(self, project_type):
        return {'project_type': project_type, 'project_nr': '1042', 'customer': {'title': 'Herr', 'full_name': 'Max Muster'},
                'address': {'full_address': 'Hauptstr. 1, 12345 Berlin'}}

    def test_every_confirmed_code_and_name_builds_a_title(self):
        from hero_integration.services.project_naming import TRADE_CATALOG, build_project_title
        for code, name in TRADE_CATALOG.items():
            for value in (code, code.lower(), name):
                with self.subTest(value=value):
                    title = build_project_title(self.record(value))
                    self.assertTrue(title.startswith(f'{code}-1042 | {name}, Herr Max Muster'))

    def test_the_hero_measure_wins_over_project_type(self):
        from hero_integration.services.project_naming import build_project_title
        record = {**self.record('UNMANAGED'), 'measure': {'short': 'KLIM', 'name': 'Klimatechnik'}}
        self.assertTrue(build_project_title(record).startswith('KLIM-1042 | Klimatechnik,'))
        record['measure'] = {'short': 'X1', 'name': 'Dachsanierung'}
        self.assertTrue(build_project_title(record).startswith('DS-1042 | Dachsanierung,'))

    def test_conflicting_confirmed_measure_values_block_the_title(self):
        from hero_integration.services.project_naming import ProjectTitleValidationError, build_project_title
        record = {**self.record('UNMANAGED'), 'measure': {'short': 'PV', 'name': 'Klimatechnik'}}
        with self.assertRaises(ProjectTitleValidationError):
            build_project_title(record)
        record['measure'] = {'short': 'X1', 'name': 'Y2'}
        with self.assertRaises(ProjectTitleValidationError):
            build_project_title(record)

    def test_project_query_v2_reads_the_measure_and_v1_is_unchanged(self):
        from hero_integration.sources import hero
        self.assertEqual(hero.project_query('v1'), hero.PROJECT_QUERY_V1)
        self.assertNotIn('measure', hero.PROJECT_QUERY_V1)
        self.assertIn('measure { short name }', hero.project_query('v2'))
        with self.assertRaises(ValueError):
            hero.project_query('v9')

    def test_an_unknown_trade_names_its_value(self):
        from hero_integration.services.project_naming import ProjectTitleValidationError, build_project_title
        with self.assertRaisesRegex(ProjectTitleValidationError, "'XYZ' has no confirmed"):
            build_project_title(self.record('XYZ'))


class TestRecordIsolation(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(frappe.db, 'savepoint'), mock.patch.object(frappe.db, 'rollback'),
                   mock.patch.object(frappe, 'log_error'), mock.patch.object(frappe, 'clear_messages')]
        self.savepoint, self.rollback, self.log_error, _ = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)

    def test_a_validation_error_blocks_only_this_record(self):
        def rejected():
            raise frappe.InvalidEmailAddressError('x is not a valid email')
        result = migration._migrate_isolated(rejected)
        self.assertEqual(result.code, 'BLOCKED_RECORD')
        self.assertIn('InvalidEmailAddressError', result.detail)
        self.assertNotIn('x is not', result.detail)
        self.rollback.assert_called_once_with(save_point='hero_record')
        self.log_error.assert_called_once()

    def test_a_database_data_error_blocks_only_this_record(self):
        result = migration._migrate_isolated(mock.Mock(side_effect=frappe.db.DataError(1406, 'Data too long')))
        self.assertEqual(result.code, 'BLOCKED_RECORD')
        self.assertIn('DataError', result.detail)

    def test_other_errors_still_stop_the_run(self):
        for error in (MappingPlanError('plan'), RuntimeError('boom')):
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                migration._migrate_isolated(mock.Mock(side_effect=error))
        self.rollback.assert_not_called()

    def test_a_lost_savepoint_fails_the_run_with_the_original_error(self):
        self.rollback.side_effect = RuntimeError('SAVEPOINT hero_record does not exist')
        with self.assertRaises(frappe.ValidationError):
            migration._migrate_isolated(mock.Mock(side_effect=frappe.ValidationError('bad')))

    def test_a_successful_record_is_returned_unchanged(self):
        result = migration.Result('CREATED', 'ok')
        self.assertIs(migration._migrate_isolated(lambda: result), result)


if __name__ == '__main__':
    unittest.main()
