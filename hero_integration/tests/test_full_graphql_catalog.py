from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path

from hero_integration.services.frappe_writeback import (
    WritebackContractError,
    build_request_checked,
    normalise_draft_mapping,
)
from hero_integration.services.schema_operations import catalog


SCHEMA = {"__schema": {"queryType": {"name": "PartnerQuery"}, "mutationType": {"name": "PartnerMutation"}, "types": [
    {"kind": "OBJECT", "name": "PartnerQuery", "fields": [
        {"name": "one", "args": [], "type": {"kind": "OBJECT", "name": "One"}},
        {"name": "two", "args": [], "type": {"kind": "OBJECT", "name": "Two"}},
    ]},
    {"kind": "OBJECT", "name": "PartnerMutation", "fields": [
        {"name": "save_one", "args": [{"name": "input", "type": {"kind": "INPUT_OBJECT", "name": "SaveInput"}}], "type": {"kind": "SCALAR", "name": "Boolean"}},
        {"name": "save_two", "args": [{"name": "enabled", "type": {"kind": "SCALAR", "name": "Boolean"}}], "type": {"kind": "SCALAR", "name": "Boolean"}},
        {"name": "bulk", "args": [{"name": "items", "type": {"kind": "LIST", "ofType": {"kind": "SCALAR", "name": "String"}}}], "type": {"kind": "SCALAR", "name": "Boolean"}},
    ]},
    {"kind": "INPUT_OBJECT", "name": "SaveInput", "inputFields": [
        {"name": "title", "type": {"kind": "SCALAR", "name": "String"}},
        {"name": "settings", "type": {"kind": "INPUT_OBJECT", "name": "SettingsInput"}},
    ]},
    {"kind": "INPUT_OBJECT", "name": "SettingsInput", "inputFields": [
        {"name": "enabled", "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "Boolean"}}},
    ]},
    {"kind": "OBJECT", "name": "One", "fields": []}, {"kind": "OBJECT", "name": "Two", "fields": []},
]}}


class TestFullGraphQLCatalog(unittest.TestCase):
    def test_mapping_editor_loads_catalogs_without_operation_allowlists(self):
        source = (Path(__file__).resolve().parents[1] / "public" / "js" / "hero_mapping_editor.js").read_text()
        self.assertIn("loadQueryOperations", source)
        self.assertIn("renderDynamicOperations", source)
        self.assertIn("input_fields", source)
        self.assertIn("Filter snapshot queries", source)
        self.assertIn("Filter snapshot mutations", source)
        self.assertNotIn("READ_OPERATIONS", source)
        self.assertNotIn('["update_contact", "update_project_match"].includes', source)

    def test_catalog_keeps_every_root_operation_and_nested_input_path(self):
        self.assertEqual([item["operation_name"] for item in catalog(SCHEMA, "QUERY")], ["one", "two"])
        mutations = catalog(SCHEMA, "MUTATION")
        self.assertEqual([item["operation_name"] for item in mutations], ["save_one", "save_two", "bulk"])
        fields = mutations[0]["input_fields"]["input"]
        self.assertEqual([item["path"] for item in fields], ["title", "settings", "settings.enabled"])
        self.assertTrue(fields[-1]["selectable"])
        self.assertFalse(mutations[-1]["input_fields"]["items"][0]["selectable"])

    def test_draft_rejects_custom_scalar_without_a_typed_preview_contract(self):
        schema = deepcopy(SCHEMA)
        schema['__schema']['types'].append({'kind':'SCALAR', 'name':'Opaque'})
        input_type = next(item for item in schema['__schema']['types'] if item['name'] == 'SaveInput')
        input_type['inputFields'].append({'name':'opaque', 'type':{'kind':'SCALAR', 'name':'Opaque'}})
        content = {'source':{'doctype':'Customer', 'field':'choice'},
                   'target':{'operation':'save_one', 'argument':'input', 'field':'opaque'},
                   'transformation':'Identity'}
        with self.assertRaisesRegex(WritebackContractError, 'scalar or enum'):
            normalise_draft_mapping(content, schema)
        schema['__schema']['types'].append({'kind':'ENUM', 'name':'Choice',
                                            'enumValues':[{'name':'YES'}, {'name':'NO'}]})
        input_type['inputFields'].append({'name':'choice', 'type':{'kind':'ENUM', 'name':'Choice'}})
        content['target']['field'] = 'choice'
        self.assertEqual(normalise_draft_mapping(content, schema)['target']['graphql_type'], 'Choice')

    def test_generic_draft_is_schema_bound_but_cannot_compile_as_writeback(self):
        mapping = normalise_draft_mapping({
            "source": {"doctype": "Any Permitted DocType", "field": "enabled"},
            "target": {"operation": "save_one", "argument": "input", "field_path": "settings.enabled"},
            "transformation": "Identity",
        }, SCHEMA)
        self.assertTrue(mapping["draft_only"])
        self.assertEqual(mapping["target"]["graphql_type"], "Boolean!")
        with self.assertRaises(WritebackContractError):
            build_request_checked(mapping, SCHEMA, 1, True)


if __name__ == "__main__":
    unittest.main()
