"""Regression checks stop before mapping writes; no mapping is activated."""
import json
import unittest
from unittest.mock import patch

from hero_integration.services import mapping_exchange, schema_provisioning
from hero_integration.tests.test_schema_provisioning import _Frappe, package


class TestAdoptionSchemaClosure(unittest.TestCase):
    def setUp(self):
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping
        self.f = _Frappe()
        p = patch.object(hero_frappe_mapping, '_lock_active_target')
        p.start()
        self.addCleanup(p.stop)
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
            self.f.custom_fields[("Customer", field["fieldname"])]["options"] = "Different"
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

    def test_current_locking_read_detects_drift_when_snapshot_view_is_stale(self):
        payload = json.dumps(package())
        field = package()["schema"]["custom_fields"][0]
        self.f.custom_fields[("Customer", field["fieldname"])] = {**field, "dt": "Customer"}
        preview = schema_provisioning.preview_package("SYNTHETIC", payload)
        selected = ["doctype:detail-row", "mapping:customer-schema-value"]
        create = schema_provisioning._create_custom_doctype
        sql = self.f.db.sql
        current = {"drift": False}
        def concurrent_schema_drift(definition, selected_relations):
            create(definition, selected_relations)
            current["drift"] = True
        def current_read(query, values, as_dict=False):
            result = sql(query, values, as_dict=as_dict)
            if current["drift"] and "`tabCustom Field`" in query and tuple(values) == ("Customer", field["fieldname"]):
                return [type(result[0])({**result[0], "options": "Different"})]
            return result
        with patch.object(self.f.db, "sql", side_effect=current_read), \
             patch.object(schema_provisioning, "_create_custom_doctype", side_effect=concurrent_schema_drift), \
             patch.object(schema_provisioning, "_activate_selected_mappings", side_effect=AssertionError("MAPPING DML REACHED")) as mapping_dml:
            with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "reconciliation"):
                schema_provisioning.adopt_package("SYNTHETIC", payload, selected, True, preview["preview_revision"])
            mapping_dml.assert_not_called()

    def test_relevant_property_setter_fails_closed(self):
        from types import SimpleNamespace
        field = package()["schema"]["custom_fields"][0]
        definitions = {"custom_doctypes": [], "custom_fields": [field], "relations": []}
        setter = SimpleNamespace(doctype_or_field="DocField", field_name=field["fieldname"], property="label")
        with patch.object(self.f.db, "sql", return_value=[setter]):
            with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "reconciliation"):
                schema_provisioning._recheck_required_schema_current(definitions, {field["id"]})

    def test_unrelated_property_setters_do_not_block_required_field(self):
        from types import SimpleNamespace
        field = package()["schema"]["custom_fields"][0]
        self.f.custom_fields[(field["doctype"], field["fieldname"])] = {**field, "dt": field["doctype"]}
        definitions = {"custom_doctypes": [], "custom_fields": [field], "relations": []}
        sql = self.f.db.sql
        irrelevant = [
            SimpleNamespace(doctype_or_field="DocField", field_name=field["fieldname"], property="description"),
            SimpleNamespace(doctype_or_field="DocField", field_name="other", property="label"),
            SimpleNamespace(doctype_or_field="DocType", field_name="", property="module"),
        ]
        def current(query, args, as_dict=False):
            return irrelevant if "`tabProperty Setter`" in query else sql(query, args, as_dict=as_dict)
        with patch.object(self.f.db, "sql", side_effect=current):
            schema_provisioning._recheck_required_schema_current(definitions, {field["id"]})

    def test_doctype_and_relation_are_verified_from_current_rows(self):
        from hero_integration.tests.test_schema_provisioning import _Field
        self.f.doctypes["HERO Detail"] = {"custom": 1, "module": "HERO Integration", "istable": 1,
            "fields": {"custom_hero_value": _Field("Data"), "custom_hero_link": _Field("Link", "Customer")}}
        definition = {"id": "doctype:detail", "name": "HERO Detail", "is_child": True, "fields": [
            {"id": "field:value", "fieldname": "custom_hero_value", "fieldtype": "Data", "options": ""},
            {"id": "field:link", "fieldname": "custom_hero_link", "fieldtype": "Link", "options": "Customer"},
        ]}
        relation = {"id": "relation:link", "doctype": "HERO Detail", "field_id": "field:link",
                    "fieldname": "custom_hero_link", "relation_type": "Link", "target_doctype": "Customer"}
        definitions = {"custom_doctypes": [definition], "custom_fields": [], "relations": [relation]}
        schema_provisioning._recheck_required_schema_current(definitions, {"doctype:detail", "relation:link"})
        self.f.doctypes["HERO Detail"]["fields"]["custom_hero_link"].options = "Other"
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "reconciliation"):
            schema_provisioning._recheck_required_schema_current(definitions, {"doctype:detail", "relation:link"})
