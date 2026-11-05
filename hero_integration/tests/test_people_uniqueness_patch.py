"""Upgrade source-local User observations without altering identities or user data."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration import install
from hero_integration.patches.v0_3 import remove_global_people_uniqueness as upgrade


class Field(dict):
    def __getattr__(self, name):
        return self.get(name)


class TestPeopleUniquenessPatch(unittest.TestCase):
    def setUp(self):
        self.fields = {name: Field(name='User-' + name, dt='User', fieldname=name,
                                  fieldtype='Data', unique=1, read_only=1, no_copy=1,
                                  label='Existing label', modified='unchanged') for name in upgrade.FIELDS}
        self.indexes = set(upgrade.FIELDS) | {'email', 'source_and_partner'}
        self.users = {'alice': {'roles': ['Employee'], 'custom_hero_partner_id': '7'},
                      'bob': {'roles': ['System Manager'], 'custom_hero_partner_id': '8'}}
        self.before_users = copy.deepcopy(self.users)

        def set_value(doctype, name, key, value, **kwargs):
            self.assertEqual(doctype, 'Custom Field')
            self.assertEqual((key, value, kwargs), ('unique', 0, {'update_modified': False}))
            self.fields[name.removeprefix('User-')][key] = value

        def sync(doctype):
            self.assertEqual(doctype, 'User')
            for name, field in self.fields.items():
                if not field['unique']:
                    self.indexes.discard(name)

        self.fake = SimpleNamespace(db=SimpleNamespace(table_exists=Mock(return_value=True),
            exists=lambda dt, filters: ('User-' + filters['fieldname']) if filters['fieldname'] in self.fields else None,
            set_value=Mock(side_effect=set_value), updatedb=Mock(side_effect=sync)),
            get_doc=lambda dt, name: self.fields[name.removeprefix('User-')], clear_cache=Mock())
        context = patch.object(upgrade, 'frappe', self.fake)
        context.start()
        self.addCleanup(context.stop)

    def test_upgrade_is_idempotent_preserving_user_data_labels_and_other_indexes(self):
        before = copy.deepcopy(self.fields)
        upgrade.execute()
        upgrade.execute()
        self.assertEqual(self.fake.db.set_value.call_count, 2)
        self.assertEqual(self.fake.db.updatedb.call_count, 2)
        self.assertEqual(self.indexes, {'email', 'source_and_partner'})
        self.assertEqual(self.users, self.before_users)
        for name, field in self.fields.items():
            self.assertEqual(field, {**before[name], 'unique': 0})
        self.fake.clear_cache.assert_called_with(doctype='User')

    def test_retry_repairs_index_when_metadata_was_already_updated(self):
        for field in self.fields.values():
            field['unique'] = 0
        upgrade.execute()
        self.fake.db.set_value.assert_not_called()
        self.fake.db.updatedb.assert_called_once_with('User')
        self.assertEqual(self.indexes, {'email', 'source_and_partner'})

    def test_absent_fields_need_no_schema_sync(self):
        self.fields.clear()
        upgrade.execute()
        self.fake.db.updatedb.assert_not_called()
        self.fake.db.set_value.assert_not_called()

    def test_foreign_field_shape_blocks_before_any_metadata_change(self):
        self.fields[upgrade.FIELDS[1]]['fieldtype'] = 'Int'
        with self.assertRaises(ValueError):
            upgrade.execute()
        self.fake.db.updatedb.assert_not_called()
        self.fake.db.set_value.assert_not_called()

    def test_fresh_install_and_upgrade_share_nonunique_observations(self):
        definitions = {field['fieldname']: field for field in install.HERO_CUSTOM_FIELDS['User']}
        for name in upgrade.FIELDS:
            self.assertEqual(definitions[name]['unique'], 0)
            self.assertEqual(definitions[name]['read_only'], 1)
        root = Path(install.__file__).parent
        registration = (root / 'patches.txt').read_text()
        self.assertIn('hero_integration.patches.v0_3.remove_global_people_uniqueness',
                      registration.split('[post_model_sync]', 1)[1])
        link = json.loads((root / 'hero_integration/doctype/hero_integration_link/hero_integration_link.json').read_text())
        source_key = next(field for field in link['fields'] if field['fieldname'] == 'source_key')
        self.assertEqual(source_key['unique'], 1)

    def test_native_index_lookup_excludes_composite_indexes(self):
        try:
            from frappe.database.mariadb.database import MariaDBDatabase
        except ImportError:
            self.skipTest('Native Frappe MariaDB driver is not installed in this test environment.')
        composite = SimpleNamespace(Key_name='source_and_partner')
        single = SimpleNamespace(Key_name='custom_hero_partner_id')
        database = SimpleNamespace(sql=Mock(side_effect=[[composite, single], [composite], []]))
        found = MariaDBDatabase.get_column_index(database, 'tabUser', 'custom_hero_partner_id', unique=True)
        self.assertIs(found, single)
        self.assertEqual(database.sql.call_count, 3)


if __name__ == '__main__':
    unittest.main()
