import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import schema_provisioning as schema


class TestSchemaDraftBoundary(unittest.TestCase):
    def setUp(self):
        self.frappe = SimpleNamespace(db=SimpleNamespace(transaction_writes=0))
        self.definitions = {'custom_doctypes': [], 'custom_fields': [], 'relations': []}
        self.patches = [
            patch.object(schema, 'frappe', self.frappe),
            patch.object(schema, '_definitions', return_value=self.definitions),
            patch.object(schema, '_schema_items', return_value=[{'id': 'item', 'state': 'New'}]),
            patch.object(schema, '_validate_selection'),
            patch.object(schema, '_require_schema_permissions'),
            patch.object(schema, '_relation_fields', return_value={}),
        ]
        for entry in self.patches:
            entry.start()
            self.addCleanup(entry.stop)

    def test_prior_write_blocks_schema_before_ddl(self):
        self.frappe.db.transaction_writes = 1
        with patch.object(schema, '_ordered_doctypes') as create:
            with self.assertRaisesRegex(schema.SchemaProvisioningError, 'clean transaction'):
                schema.provision_schema({})
            create.assert_not_called()

    def test_partial_creation_failure_reports_uncertain_schema(self):
        self.definitions['custom_fields'] = [
            {'id': 'item', 'doctype': 'Task', 'fieldname': 'custom_hero_example'},
            {'id': 'item2', 'doctype': 'Task', 'fieldname': 'custom_hero_other'},
        ]
        with patch.object(schema, '_schema_items', return_value=[
                {'id': 'item', 'state': 'New'}, {'id': 'item2', 'state': 'New'}]), \
             patch.object(schema, '_ordered_doctypes', return_value=[]), \
             patch.object(schema, '_custom_field_is_new', return_value=True), \
             patch.object(schema, '_create_custom_field', side_effect=[None, RuntimeError('injected')]):
            with self.assertRaisesRegex(schema.SchemaProvisioningError, 'may persist') as raised:
                schema.provision_schema({})
        self.assertIn('Task.custom_hero_example', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
