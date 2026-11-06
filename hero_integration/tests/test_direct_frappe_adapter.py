from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration import api
from hero_integration.services.direct_mapping import (
    DirectMappingError,
    MappingSpec,
    source_value,
    transformed_value,
    validate_spec,
)
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mapping_source_fields import source_fields


class TestDirectFrappeAdapter(unittest.TestCase):
    SNAPSHOT_FIXTURE = "kc2tq33u7j"

    def setUp(self):
        self.metadata = _Metadata()

    def test_explicit_custom_mapping_has_a_checked_typed_preview(self):
        spec = MappingSpec("HeroCustomers", "category", "Customer", "territory", "string|null")

        validate_spec(spec, self.metadata)
        value = transformed_value(spec, "Synthetic Territory")

        self.assertEqual(value, "Synthetic Territory")

    def test_source_value_reads_nested_response_paths_without_a_fallback(self):
        record = {"address": {"city": "Synthetic City"}}

        self.assertEqual(source_value(record, "address.city"), "Synthetic City")
        self.assertIsNone(source_value(record, "address.zipcode"))
        self.assertIsNone(source_value(record, "address.city.value"))

    def test_protected_default_and_incompatible_targets_are_rejected(self):
        for spec in (
            MappingSpec("HeroCustomers", "full_name", "Customer", "customer_name", "string|null"),
            MappingSpec("HeroProjectMatches", "project_title", "Project", "company", "string"),
            MappingSpec("HeroCustomers", "category", "Project", "territory", "string|null"),
            MappingSpec("HeroCustomers", "category", "Customer", "territory_link", "string|null"),
            MappingSpec("HeroProjectMatches", "partner_notes", "Project", "notes", "string|null"),
            MappingSpec("HeroProjectMatches", "partner_notes", "Project", "custom_other_notes", "string|null"),
        ):
            with self.assertRaises(DirectMappingError):
                validate_spec(spec, self.metadata)

    def test_partner_notes_require_an_explicit_custom_field_mapping(self):
        self.metadata.extra = [
            SimpleNamespace(fieldname="custom_hero_partner_notes", label="HERO Partner Notes", fieldtype="Small Text", read_only=0, options="", is_custom_field=1),
        ]
        spec = MappingSpec(
            "HeroProjectMatches", "partner_notes", "Project", "custom_hero_partner_notes", "string|null",
            transformation="Trim",
        )

        validate_spec(spec, self.metadata)

    def test_graph_view_is_deterministic_for_a_synthetic_snapshot(self):
        snapshot = {"__schema": {"types": [
            {"kind": "OBJECT", "name": "Query", "fields": [
                {"name": "contacts", "type": {"kind": "OBJECT", "name": "Contact"}},
            ]},
            {"kind": "OBJECT", "name": "Contact", "fields": [
                {"name": "id", "type": {"kind": "SCALAR", "name": "Int"}},
            ]},
            {"kind": "SCALAR", "name": "Int"},
        ]}}

        first, second = analyze_snapshot(snapshot), analyze_snapshot(snapshot)

        self.assertEqual(first, second)
        self.assertEqual(first.nodes[0]["id"], "field:Contact.id")
        self.assertIn({"source": "field:Query.contacts", "target": "type:Contact", "kind": "RETURNS"}, first.edges)

    def test_source_picker_marks_schema_only_fields_as_not_selectable(self):
        schema = {"types": [{"name": "Contact", "fields": [{"name": "email"}, {"name": "unselected"}]}]}
        fields = source_fields("HeroCustomers", schema, "select")

        self.assertEqual(len(fields), 1)
        self.assertFalse(fields[0].selectable)
        self.assertIn("Schema-only", fields[0].reason)

    def test_source_picker_reads_return_type_nullability_and_operation_path(self):
        schema = {"__schema": {"types": [
            {"name": "Query", "fields": [{"name": "contacts", "type": {"kind": "LIST", "ofType": {"kind": "OBJECT", "name": "Contact"}}}]},
            {"name": "Contact", "fields": [
                {"name": "id", "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "Int"}}},
                {"name": "unselected", "type": {"kind": "SCALAR", "name": "String"}},
            ]},
        ]}}

        fields = source_fields("HeroCustomers", schema, "")
        selected = next(field for field in fields if field.path == "id")
        schema_only = next(field for field in fields if field.path == "unselected")

        self.assertEqual((selected.graphql_path, selected.return_type, selected.fieldtype), ("contacts.id", "Contact", "Int"))
        self.assertFalse(selected.nullable)
        self.assertFalse(schema_only.selectable)
        self.assertEqual(schema_only.graphql_path, "contacts.unselected")

    def test_kc2tq33u7j_contacts_list_resolves_all_17_customer_contract_paths(self):
        self.assertEqual(self.SNAPSHOT_FIXTURE, "kc2tq33u7j")
        fields = source_fields("HeroCustomers", _historic_partner_customer_schema())
        selected = [field for field in fields if field.selectable]

        self.assertEqual(
            [field.path for field in selected],
            [
                "id", "nr", "first_name", "last_name", "full_name", "email", "phone_mobile", "phone_home",
                "company_name", "category", "type", "is_contact_person", "is_deleted",
                "address.id", "address.street", "address.zipcode", "address.city",
            ],
        )
        self.assertEqual(len(selected), 17)
        self.assertEqual(
            (next(field for field in selected if field.path == "address.city").graphql_path,
             next(field for field in selected if field.path == "address.city").return_type),
            ("contacts.address.city", "Customer"),
        )





    def test_source_picker_excludes_missing_and_mutation_input_paths(self):
        schema = _customer_schema(contact_fields=[
            {"name": "category", "type": {"kind": "SCALAR", "name": "String"}},
        ])

        selected = [field.path for field in source_fields("HeroCustomers", schema) if field.selectable]

        self.assertEqual(selected, ["category"])
        self.assertNotIn("mutation_category", selected)

    def test_source_picker_keeps_large_schema_suggestions_scoped_to_the_operation_response(self):
        schema = _customer_schema()
        schema["__schema"]["types"].extend(
            {"kind": "OBJECT", "name": f"Unrelated{index}", "fields": [
                {"name": f"field{field_index}", "type": {"kind": "SCALAR", "name": "String"}}
                for field_index in range(9)
            ]}
            for index in range(300)
        )

        fields = source_fields("HeroCustomers", schema, "category")

        self.assertEqual([field.path for field in fields], ["category"])



    def test_stale_snapshot_is_rejected_before_picker_or_preview(self):
        raw = {"__schema": {"types": [{"kind": "OBJECT", "name": "Query", "fields": []}]}}
        snapshot = SimpleNamespace(analysis_status="Validated", snapshot_json=__import__("json").dumps(raw), schema_fingerprint="stale")
        fake_frappe = SimpleNamespace(_=lambda message: message, get_doc=lambda *args: snapshot, throw=lambda message: (_ for _ in ()).throw(ValueError(message)))

        with patch.object(api, "frappe", fake_frappe), self.assertRaisesRegex(ValueError, "stale"):
            api._validated_snapshot("SYNTHETIC-SNAPSHOT")


class _Metadata:
    extra = []

    def fields(self, doctype):
        return list(self.extra)

    def field(self, doctype, fieldname):
        if (doctype, fieldname) == ("Customer", "territory"):
            return SimpleNamespace(fieldtype="Data", read_only=0, options="", is_custom_field=1)
        if (doctype, fieldname) == ("Customer", "territory_link"):
            return SimpleNamespace(fieldtype="Link", read_only=0, options="Territory", is_custom_field=1)
        for field in self.extra:
            if field.fieldname == fieldname:
                return field
        return None

    @staticmethod
    def value_is_valid(doctype, fieldname, value):
        return (doctype, fieldname, value) == ("Customer", "territory", "Synthetic Territory")


def _customer_schema(contact_fields=None):
    return {"__schema": {"types": [
        {"kind": "OBJECT", "name": "Query", "fields": [
            {"name": "contacts", "type": {"kind": "LIST", "ofType": {"kind": "OBJECT", "name": "Contact"}}},
        ]},
        {"kind": "OBJECT", "name": "Mutation", "fields": [
            {"name": "change_contact", "type": {"kind": "OBJECT", "name": "Contact"}},
        ]},
        {"kind": "INPUT_OBJECT", "name": "ContactInput", "inputFields": [
            {"name": "mutation_category", "type": {"kind": "SCALAR", "name": "String"}},
        ]},
        {"kind": "OBJECT", "name": "Contact", "fields": contact_fields or [
            {"name": "category", "type": {"kind": "SCALAR", "name": "String"}},
            {"name": "address", "type": {"kind": "OBJECT", "name": "Address"}},
        ]},
        {"kind": "OBJECT", "name": "Address", "fields": [
            {"name": "city", "type": {"kind": "SCALAR", "name": "String"}},
            {"name": "parent", "type": {"kind": "OBJECT", "name": "Contact"}},
        ]},
    ]}}


def _historic_partner_customer_schema():
    customer_scalar_fields = [
        ("id", "Int"), ("nr", "String"), ("first_name", "String"), ("last_name", "String"),
        ("full_name", "String"), ("email", "String"), ("phone_mobile", "String"), ("phone_home", "String"),
        ("company_name", "String"), ("category", "String"), ("type", "String"),
        ("is_contact_person", "Boolean"), ("is_deleted", "Boolean"),
    ]
    return {"schema": {
        "query_type_name": "PartnerQuery",
        "mutation_type_name": "PartnerMutation",
        "types": [
            {"kind": "OBJECT", "name": "PartnerQuery", "fields": [{
                "name": "contacts",
                "type_ref": {"kind": "LIST", "name": None, "of_type": {"kind": "OBJECT", "name": "Customer", "of_type": None}},
            }]},
            {"kind": "OBJECT", "name": "PartnerMutation", "fields": []},
            {"kind": "OBJECT", "name": "Customer", "fields": [
                *[{"name": name, "type_ref": {"kind": "SCALAR", "name": kind, "of_type": None}} for name, kind in customer_scalar_fields],
                {"name": "address", "type_ref": {"kind": "OBJECT", "name": "Address", "of_type": None}},
            ]},
            {"kind": "OBJECT", "name": "Address", "fields": [
                {"name": "id", "type_ref": {"kind": "SCALAR", "name": "Int", "of_type": None}},
                {"name": "street", "type_ref": {"kind": "SCALAR", "name": "String", "of_type": None}},
                {"name": "zipcode", "type_ref": {"kind": "SCALAR", "name": "String", "of_type": None}},
                {"name": "city", "type_ref": {"kind": "SCALAR", "name": "String", "of_type": None}},
            ]},
        ],
    }}
