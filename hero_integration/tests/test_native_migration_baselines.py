"""Verified native storage equivalence for migration links predating typed field digests."""
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase

from hero_integration.services import employee_migration as employees, product_migration as products


class Native(dict):
    doctype = 'Employee'
    name = 'example'

    def __init__(self, value, fieldtype):
        super().__init__(example=value)
        self.meta = SimpleNamespace(get_field=lambda field: SimpleNamespace(fieldtype=fieldtype, is_virtual=0))


class TestMigrationBaselines(TestCase):
    def test_legacy_product_null_projection_into_native_int(self):
        doc = Native(0, 'Int')
        prior = {'example': products.digest(None)}
        self.assertEqual(products.verified_field_digests(doc, prior, {'example': None}), prior)

    def test_absent_mapping_real_change_and_text_remain_unconfirmed(self):
        prior = {'example': products.digest(None)}
        for doc, incoming in ((Native(0, 'Int'), {}), (Native(1, 'Int'), {'example': None}),
                              (Native('', 'Data'), {'example': None}), (Native(0, 'Int'), {'example': 8})):
            with self.subTest(incoming=incoming, doc=doc):
                self.assertNotEqual(products.verified_field_digests(doc, prior, incoming), prior)

    def test_confirmed_employee_numeric_empty_survives_native_persistence(self):
        import json
        prior = {'example': ''}
        link = SimpleNamespace(target_field_hashes=json.dumps(prior), target_payload_hash=employees._digest(prior))
        employees._assert_unchanged(link, Native(0, 'Int'))
        with self.assertRaises(employees.EmployeeMigrationBlocked):
            employees._assert_unchanged(link, Native(7, 'Int'))
        with self.assertRaises(employees.EmployeeMigrationBlocked):
            employees._assert_unchanged(link, Native(0, 'Data'))

    def test_temporal_representation_keeps_time_and_timezone_distinctions(self):
        import json
        current = Native(datetime(2026, 3, 2, 11, 1), 'Datetime')
        for stamp, ok in [('2026-03-02T11:01:00', True), ('2026-03-02T10:01:00', False),
                          ('2026-03-02T11:01:00+01:00', False)]:
            prior = {'example': stamp}
            link = SimpleNamespace(target_field_hashes=json.dumps(prior), target_payload_hash=employees._digest(prior))
            if ok:
                employees._assert_unchanged(link, current)
            else:
                with self.assertRaises(employees.EmployeeMigrationBlocked):
                    employees._assert_unchanged(link, current)


class TestHistoricalDependencies(TestCase):
    def test_verified_deleted_user_and_partner_have_independent_scopes(self):
        from hero_integration.services.deferred_references import references
        from hero_integration.services.employee_migration import historical_user_scope
        rules = [SimpleNamespace(source_path='assigned_user', transformation='HERO User Link', purpose='Field'),
                 SimpleNamespace(source_path='partner', transformation='User By Partner ID', purpose='Field')]
        plan = SimpleNamespace(definitions=rules, target_operation='HeroProjectMatches', adapter_options={'version': 2})
        row = {'assigned_user': 9, 'partner': 8, '_hero_source_instance': 'source-a'}
        with historical_user_scope('source-a', {'9'}, {'8'}):
            self.assertEqual(references(plan, row), [])
        with historical_user_scope('source-a', {'8'}, {'9'}):
            self.assertEqual(references(plan, row), [('User','9'),('partner','8')])
        with historical_user_scope('other-source', {'9'}, {'8'}):
            self.assertEqual(references(plan, row), [('User','9'),('partner','8')])


class TestEmployeeChildTables(TestCase):
    def test_native_rows_are_retained_and_local_divergence_requires_a_proof(self):
        import json
        from unittest.mock import patch
        child = Native(7, 'Int')
        cols = SimpleNamespace(fields=[SimpleNamespace(fieldname='example')], get_valid_columns=lambda: ['example'])
        table = Native([child], 'Table')
        table.meta.get_field = lambda field: SimpleNamespace(fieldtype='Table', options='Test Child')
        with patch.object(employees.frappe, 'get_meta', return_value=cols):
            old = SimpleNamespace(target_field_hashes=json.dumps({'example': str([child])}))
            self.assertEqual(employees._mapped_updates(table, {'example': [{'example': 7}]}, old), {})
            with self.assertRaises(employees.EmployeeMigrationBlocked):
                employees._mapped_updates(table, {'example': [{'example': 8}]}, old)
            prior = employees._table_baseline(table, 'example')
            confirmed = SimpleNamespace(target_field_hashes=json.dumps({'example': prior}),
                                        target_payload_hash=employees._digest({'example': prior}))
            employees._assert_unchanged(confirmed, table)
            self.assertEqual(employees._mapped_updates(table, {'example': [{'example': 8}]}, confirmed),
                             {'example': [{'example': 8}]})
            child['example'] = 9
            with self.assertRaises(employees.EmployeeMigrationBlocked):
                employees._assert_unchanged(confirmed, table)
            with self.assertRaises(employees.EmployeeMigrationBlocked):
                employees._mapped_updates(table, {'example': [{'example': 8}]}, confirmed)

    def test_product_table_requires_complete_proof_for_legacy_adoption(self):
        from unittest.mock import patch
        child = Native(7, 'Int')
        table = Native([child], 'Table')
        table.meta.get_field = lambda field: SimpleNamespace(fieldtype='Table', options='Test Child')
        meta = SimpleNamespace(fields=[SimpleNamespace(fieldname='example')], get_valid_columns=lambda: ['example'])
        old = {'example': products.digest('old child row representation')}
        with patch.object(products.frappe, 'get_meta', return_value=meta):
            equal = {'example': [{'example': 7}]}
            self.assertEqual(products.verified_field_digests(table, old, equal), old)
            self.assertNotEqual(products.verified_field_digests(table, old, {'example': [{}]}), old)
            self.assertNotEqual(products.verified_field_digests(table, old, {'example': [{'example': 8}]}), old)
            confirmed = products.owned_field_digests(table, old)
            child['example'] = 9
            self.assertNotEqual(products.owned_field_digests(table, old), confirmed)

    def test_decimal_child_compares_at_persisted_column_scale(self):
        from unittest.mock import patch
        from hero_integration.services.change_detection import _same_table_value
        child = Native(0, 'Float')
        with patch.object(products.frappe, 'db', SimpleNamespace(db_type='mariadb', get_column_type=lambda *_: 'decimal(21,9)')):
            self.assertTrue(_same_table_value(child, 'example', 0.123456789, 0.12345678912))
            self.assertTrue(_same_table_value(child, 'example', -0.123456790, -0.12345678988))
            self.assertFalse(_same_table_value(child, 'example', 0.123456788, 0.12345678912))
            self.assertFalse(_same_table_value(child, 'example', 0.123456789, '0.12345678912'))
            self.assertFalse(_same_table_value(child, 'example', 1, False))
