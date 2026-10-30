"""Rules for HERO data that would otherwise block a whole record."""
from __future__ import annotations

import unittest

from hero_integration.services import migration
from hero_integration.services.mapping_runtime import MappingPlanError
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


if __name__ == '__main__':
    unittest.main()
