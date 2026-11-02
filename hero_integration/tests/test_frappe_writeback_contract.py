from __future__ import annotations

import json
import unittest

from hero_integration.services.frappe_writeback import (
    WritebackContractError,
    build_request_checked,
    normalise_mapping,
    reviewed_input_target,
)


def schema():
    return {"__schema": {"mutationType": {"name": "Mutation"}, "types": [
        {"kind": "OBJECT", "name": "Mutation", "fields": [
            {"name": "update_contact", "args": [{"name": "contact", "type": {"kind": "INPUT_OBJECT", "name": "CustomerInput"}}]},
            {"name": "update_project_match", "args": [{"name": "project_match", "type": {"kind": "INPUT_OBJECT", "name": "ProjectMatchInput"}}]},
        ]},
        {"kind": "INPUT_OBJECT", "name": "CustomerInput", "inputFields": [
            {"name": "id", "type": {"kind": "SCALAR", "name": "Int"}},
            {"name": "email", "type": {"kind": "SCALAR", "name": "String"}},
            {"name": "phone_mobile", "type": {"kind": "SCALAR", "name": "String"}},
        ]},
        {"kind": "INPUT_OBJECT", "name": "ProjectMatchInput", "inputFields": [
            {"name": "id", "type": {"kind": "SCALAR", "name": "Int"}},
            {"name": "partner_notes", "type": {"kind": "SCALAR", "name": "String"}},
            {"name": "project_title", "type": {"kind": "SCALAR", "name": "String"}},
        ]},
    ]}}


class TestFrappeWritebackContract(unittest.TestCase):
    def test_writeback_normalization_rejects_unreviewed_group_and_unknown_parameters(self):
        for payload in ({**self.customer_mapping(), 'additional_mappings':[{'id':'not-bound'}]},
                        {**self.customer_mapping(), 'unknown_option':'do-not-discard'}):
            with self.assertRaises(WritebackContractError):
                normalise_mapping(payload, schema())
            with self.assertRaises(WritebackContractError):
                build_request_checked(payload, schema(), 42, 'value')
    def customer_mapping(self):
        return {"source": {"doctype": "Customer", "field": "email_id"}, "target": {"operation": "update_contact", "argument": "contact", "input_type": "CustomerInput", "field": "email"}, "transformation": "Trim"}

    def project_mapping(self):
        return {"source": {"doctype": "Project", "field": "custom_hero_partner_notes"}, "target": {"operation": "update_project_match", "argument": "project_match", "input_type": "ProjectMatchInput", "field": "partner_notes"}, "transformation": "Identity"}

    def test_customer_mapping_is_narrowed_and_compiles_current_value_only(self):
        mapping = normalise_mapping(self.customer_mapping(), schema())
        document, variables = build_request_checked(mapping, schema(), "42", "  a@example.test  ")
        self.assertIn("update_contact(contact: $contact)", document)
        self.assertEqual(variables, {"contact": {"id": 42, "email": "a@example.test"}})
        self.assertNotIn("administrator_value", json.dumps(mapping))

    def test_project_mapping_uses_project_match_identity_not_project_relation(self):
        document, variables = build_request_checked(self.project_mapping(), schema(), 7, "Approved")
        self.assertIn("update_project_match(project_match: $project_match)", document)
        self.assertEqual(variables, {"project_match": {"id": 7, "partner_notes": "Approved"}})
        self.assertNotIn("project_id", json.dumps(variables))

    def test_unreviewed_or_relation_target_is_rejected(self):
        mapping = self.project_mapping()
        mapping["target"]["field"] = "project_id"
        with self.assertRaisesRegex(WritebackContractError, "reviewed target"):
            normalise_mapping(mapping, schema())

    def test_picker_exposes_only_the_schema_proven_target_for_each_source_field(self):
        self.assertEqual(
            reviewed_input_target("Customer", "mobile_no", schema()),
            {"operation": "update_contact", "argument": "contact", "input_type": "CustomerInput", "field": "phone_mobile", "graphql_type": "String"},
        )
        self.assertEqual(
            reviewed_input_target("Project", "custom_hero_partner_notes", schema()),
            {"operation": "update_project_match", "argument": "project_match", "input_type": "ProjectMatchInput", "field": "partner_notes", "graphql_type": "String"},
        )

    def test_project_title_number_and_type_are_never_written_back(self):
        # per-field Project writeback is limited to Notes.
        for field in ("project_name", "custom_hero_project_number", "custom_hero_project_type", "custom_hero_display_id"):
            with self.subTest(field=field), self.assertRaisesRegex(WritebackContractError, "not approved"):
                reviewed_input_target("Project", field, schema())

    def test_picker_rejects_unapproved_or_missing_schema_input(self):
        with self.assertRaisesRegex(WritebackContractError, "not approved"):
            reviewed_input_target("Customer", "name", schema())
        incomplete = schema()
        incomplete["__schema"]["types"][1]["inputFields"] = [{"name": "id", "type": {"kind": "SCALAR", "name": "Int"}}]
        with self.assertRaisesRegex(WritebackContractError, "saved schema"):
            reviewed_input_target("Customer", "email_id", incomplete)

    def test_reviewed_field_rejects_collection_object_and_custom_scalar_schema(self):
        for reference in (
            {"kind": "LIST", "ofType": {"kind": "SCALAR", "name": "String"}},
            {"kind": "INPUT_OBJECT", "name": "AddressInput"},
            {"kind": "SCALAR", "name": "Opaque"},
        ):
            bad = schema()
            bad["__schema"]["types"][1]["inputFields"][1]["type"] = reference
            with self.subTest(reference=reference):
                with self.assertRaises(WritebackContractError):
                    reviewed_input_target("Customer", "email_id", bad)
                with self.assertRaises(WritebackContractError):
                    build_request_checked(self.customer_mapping(), bad, 42, "value")

    def test_missing_schema_contract_or_invalid_link_identity_is_rejected(self):
        no_mutation = {"__schema": {"types": []}}
        with self.assertRaisesRegex(WritebackContractError, "saved schema"):
            normalise_mapping(self.customer_mapping(), no_mutation)
        with self.assertRaisesRegex(WritebackContractError, "external identity"):
            build_request_checked(self.customer_mapping(), schema(), "", "a@example.test")


if __name__ == "__main__":
    unittest.main()
