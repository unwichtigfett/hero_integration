"""Regression checks stop before mapping writes; no mapping is activated."""
import json
import unittest
from unittest.mock import patch

from hero_integration.services import mapping_exchange, schema_provisioning
from hero_integration.tests.test_schema_provisioning import _Frappe, package


class TestAdoptionSchemaClosure(unittest.TestCase):
    def setUp(self):
        self.f = _Frappe()
        for module in (mapping_exchange, schema_provisioning):
            p = patch.object(module, "frappe", self.f)
            p.start()
            self.addCleanup(p.stop)

    def test_unchanged_unselected_dependency_drift_blocks_mapping_dml(self):
        payload = json.dumps(package())
        field = package()["schema"]["custom_fields"][0]
        self.f.custom_fields[("Customer", field["fieldname"])] = {**field, "dt": "Customer"}
        preview = schema_provisioning.preview_package("SYNTHETIC", payload)
        by_id = {item["id"]: item for item in preview["items"]}
        self.assertEqual(by_id["field:customer-schema-value"]["state"], "Unchanged")
        selected = ["doctype:detail-row", "mapping:customer-schema-value"]
        create = schema_provisioning._create_custom_doctype
        def concurrent_schema_drift(definition, selected_relations):
            create(definition, selected_relations)
            self.f.custom_fields[("Customer", field["fieldname"])]["label"] = "Different"
        with patch.object(schema_provisioning, "_create_custom_doctype", side_effect=concurrent_schema_drift), \
             patch.object(schema_provisioning, "_activate_selected_mappings", side_effect=AssertionError("MAPPING DML REACHED")) as mapping_dml:
            with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "reconciliation"):
                schema_provisioning.adopt_package("SYNTHETIC", payload, selected, True, preview["preview_revision"])
            mapping_dml.assert_not_called()

    def test_dependency_closure_includes_transitive_unchanged_items(self):
        items = {
            "mapping:x": {"kind": "mapping", "dependencies": ["relation:a"]},
            "relation:a": {"kind": "schema", "dependencies": ["field:b"]},
            "field:b": {"kind": "schema", "dependencies": ["doctype:c"]},
            "doctype:c": {"kind": "schema", "dependencies": []},
            "unrelated": {"kind": "schema", "dependencies": []},
        }
        self.assertEqual(schema_provisioning._required_schema_closure(items, {"mapping:x"}),
                         {"relation:a", "field:b", "doctype:c"})

    def test_unknown_dependency_fails_closed(self):
        items = {"mapping:x": {"kind": "mapping", "dependencies": ["unknown"]}}
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "unknown"):
            schema_provisioning._required_schema_closure(items, {"mapping:x"})
