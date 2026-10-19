from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration import api
from hero_integration.services.direct_mapping import (
    DirectMappingError,
    MappingSpec,
    transformed_value,
    validate_spec,
)
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mapping_suggestions import (
    source_fields,
    suggest_from_source,
    suggest_from_target,
    target_fields,
)


class TestDirectFrappeAdapter(unittest.TestCase):
    def setUp(self):
        self.metadata = _Metadata()

    def test_explicit_custom_mapping_has_a_checked_typed_preview(self):
        spec = MappingSpec("HeroCustomers", "category", "Customer", "territory", "string|null")

        validate_spec(spec, self.metadata)
        value = transformed_value(spec, "Synthetic Territory")

        self.assertEqual(value, "Synthetic Territory")

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

    def test_bidirectional_suggestions_filter_protected_and_incompatible_fields(self):
        schema = {"types": []}
        self.metadata.extra = [
            SimpleNamespace(fieldname="territory", label="Customer Category", fieldtype="Data", read_only=0, options="", is_custom_field=1),
            SimpleNamespace(fieldname="status", label="Status", fieldtype="Data", read_only=0, options="", is_custom_field=1),
            SimpleNamespace(fieldname="credit_limit", label="Credit Limit", fieldtype="Float", read_only=0, options="", is_custom_field=0),
            SimpleNamespace(fieldname="custom_score", label="Custom Score", fieldtype="Float", read_only=0, options="", is_custom_field=1),
            SimpleNamespace(fieldname="territory_link", label="Territory Link", fieldtype="Link", read_only=0, options="Territory", is_custom_field=1),
        ]

        targets = target_fields("HeroCustomers", self.metadata)
        forward = suggest_from_source("HeroCustomers", "category", schema, self.metadata)
        reverse = suggest_from_target("HeroCustomers", "territory", schema, self.metadata)

        self.assertTrue(next(item for item in targets if item.path == "territory").selectable)
        self.assertFalse(next(item for item in targets if item.path == "status").selectable)
        self.assertFalse(next(item for item in targets if item.path == "territory_link").selectable)
        self.assertEqual(forward[0]["target_field"], "territory")
        self.assertNotIn("custom_score", {item["target_field"] for item in forward})
        self.assertEqual(reverse[0]["source_path"], "category")
        self.assertTrue(all(item["requires_approval"] for item in forward + reverse))

    def test_target_picker_keeps_child_relations_visible_but_unselectable(self):
        self.metadata.extra = [
            SimpleNamespace(fieldname="contacts", label="Contacts", fieldtype="Table", read_only=0, options="Contact", is_custom_field=1),
        ]

        field = target_fields("HeroCustomers", self.metadata)[0]

        self.assertFalse(field.selectable)
        self.assertEqual(field.relation, "Contact")
        self.assertIn("resolver", field.reason)

    def test_stale_snapshot_is_rejected_before_picker_or_preview(self):
        raw = {"__schema": {"types": [{"kind": "OBJECT", "name": "Query", "fields": []}]}}
        snapshot = SimpleNamespace(analysis_status="Validated", snapshot_json=__import__("json").dumps(raw), schema_fingerprint="stale")
        fake_frappe = SimpleNamespace(get_doc=lambda *args: snapshot, throw=lambda message: (_ for _ in ()).throw(ValueError(message)))

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
