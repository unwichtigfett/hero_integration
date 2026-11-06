"""Mapping extensions cannot silently adopt occupied local fields."""
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import unittest
from unittest.mock import Mock

from hero_integration.services.change_detection import new_mapped_field_conflicts
from hero_integration.services import employee_migration as employees
from hero_integration.tests import test_employee_migration as fixtures


class TestNewMappedFieldConflicts(unittest.TestCase):
    def test_empty_and_equal_values_allow_new_ownership(self):
        for current, incoming in [(None, 'new'), ('', 'new'), ('same', 'same'),
                                  (0, False), (False, 0), (1, True),
                                  (Decimal('1.00'), 1), (1.0, Decimal('1.00'))]:
            with self.subTest(current=current, incoming=incoming):
                self.assertEqual(new_mapped_field_conflicts({'field': current}, {'field': incoming}, set()), set())

    def test_zero_false_and_whitespace_are_occupied_values(self):
        for current, incoming in [(0, 1), (False, True), (0, None), (False, ''), (' ', 'new'),
                                  ('001', '1'), ('local', None), (Decimal('1.20'), '1.3'),
                                  (Decimal('1.20'), '1.2'), ('1.00', Decimal('1')),
                                  ('001', 1), (1, '001'), (Decimal('1'), '1'),
                                  (Decimal('NaN'), 'NaN'), (float('inf'), 'inf')]:
            with self.subTest(current=current, incoming=incoming):
                self.assertEqual(new_mapped_field_conflicts({'field': current}, {'field': incoming}, ()), {'field'})

    def test_dates_compare_native_storage_to_iso_without_ignoring_changed_time(self):
        for current, incoming in [(date(2026, 1, 1), '2026-01-01'),
                                  (datetime(2026, 1, 1, 10), '2026-01-01T10:00:00'),
                                  (datetime(2026, 1, 1, 10, tzinfo=timezone.utc), '2026-01-01T10:00:00Z')]:
            with self.subTest(current=current):
                self.assertFalse(new_mapped_field_conflicts({'date': current}, {'date': incoming}, []))
        self.assertEqual(new_mapped_field_conflicts({'date': datetime(2026, 1, 1, 10)},
                                                   {'date': '2026-01-01T11:00:00'}, []), {'date'})

    def test_known_fields_are_left_to_existing_baseline_comparison(self):
        doc = {'known': 'local edit', 'new': 'local new field'}
        wanted = {'known': 'remote edit', 'new': 'remote new field'}
        self.assertEqual(new_mapped_field_conflicts(doc, wanted, {'known': 'stored baseline'}), {'new'})
        self.assertEqual(doc, {'known': 'local edit', 'new': 'local new field'})


class TestEmployeeMappingExtension(unittest.TestCase):
    def fixture(self, doctype, field, local_value):
        fixture = fixtures.TestEmployeeWrites()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        read = employees.runtime.builtin_read('HeroEmployees')
        proposed = employees.preview(fixture.row, languages={'de'}, plan=read.plan)
        values = {'Employee': {}, 'User': {}}
        for item in proposed['fields']:
            if item['target_field'] not in {'employee_name', 'email'}:
                values[item['target_doctype']][item['target_field']] = item['value']
        values['Employee'].update(company='Synthetic Company', date_of_joining=employees.JOINING_DATE,
                                  user_id=fixture.user.name)
        employee = fixtures.Doc(doctype='Employee', name='employee-1', **values['Employee'])
        fixture.user.update(values['User'])
        fixture.docs[('Employee', employee.name)] = employee
        targets = {'Employee': employee, 'User': fixture.user}
        for target_doctype, kind in [('Employee', 'partner'), ('User', 'User')]:
            fields = set(values[target_doctype])
            if target_doctype == doctype:
                fields.remove(field)
            baseline = employees._values(targets[target_doctype], fields)
            fixture.links[kind] = fixtures.Doc(name='link-' + kind, target_name=targets[target_doctype].name,
                target_field_hashes=json.dumps(baseline), target_payload_hash=employees._digest(baseline),
                mapping_version='older-selection')
        expected = values[doctype][field]
        targets[doctype][field] = expected if local_value == '__same__' else local_value
        fixture.user.save = Mock()
        employee.save = Mock()
        return fixture, employee

    def test_employee_new_field_conflict_blocks_both_saves_and_binding_updates(self):
        fixture, employee = self.fixture('Employee', 'company_email', 'local@example.invalid')
        with self.assertRaisesRegex(employees.EmployeeMigrationBlocked, 'Newly mapped'):
            fixture.run_import()
        fixture.user.save.assert_not_called()
        employee.save.assert_not_called()
        fixture.write_link.assert_not_called()
        self.assertEqual(employee.company_email, 'local@example.invalid')

    def test_user_new_field_conflict_blocks_both_saves_and_binding_updates(self):
        fixture, employee = self.fixture('User', 'phone', 'local-phone')
        with self.assertRaisesRegex(employees.EmployeeMigrationBlocked, 'Newly mapped'):
            fixture.run_import()
        fixture.user.save.assert_not_called()
        employee.save.assert_not_called()
        fixture.write_link.assert_not_called()
        self.assertEqual(fixture.user.phone, 'local-phone')

    def test_empty_or_equal_new_fields_can_join_existing_mapping(self):
        for doctype, field in [('Employee', 'company_email'), ('User', 'phone')]:
            for value in (None, '', '__same__'):
                with self.subTest(doctype=doctype, value=value):
                    fixture, employee = self.fixture(doctype, field, value)
                    try:
                        self.assertEqual(fixture.run_import(), 'updated')
                        fixture.user.save.assert_called_once()
                        if doctype == 'Employee' and value != '__same__':
                            employee.save.assert_called_once()
                        else:
                            employee.save.assert_not_called()
                        self.assertEqual(fixture.write_link.call_count, 2)
                    finally:
                        fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
