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
