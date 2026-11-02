from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services.frappe_mapping_catalog import catalog, fields, validate_draft_target, writeback_fields, validate_draft_writeback_source
from hero_integration.services.mapping_suggestions import PickerField, picker_page
from hero_integration.services.mapping_suggestions import suggest_from_source


class TestFrappeMappingCatalog(unittest.TestCase):
    def setUp(self):
        self.metadata = _Metadata()

    def test_reviewed_writeback_source_does_not_unprotect_inbound_target(self):
        self.metadata._fields['Customer'] = [
            _field('customer_name', 'Customer Name', 'Data'),
            _field('customer_type', 'Customer Type', 'Data'),
            _field('name', 'Name', 'Data'),
        ]
        inbound = {item.path: item for item in fields(self.metadata, doctype='Customer')}
        outbound = {item.path: item for item in writeback_fields(self.metadata, doctype='Customer')}
        self.assertFalse(inbound['customer_name'].selectable)
        self.assertTrue(outbound['customer_name'].selectable)
        self.assertFalse(outbound['customer_type'].selectable)
        self.assertFalse(outbound['name'].selectable)
        self.assertEqual(validate_draft_writeback_source('Customer', 'customer_name', self.metadata).path, 'customer_name')

    def test_reviewed_source_requires_accessible_real_field_and_doctype(self):
        for flag in ({'hidden': 1}, {'permlevel': 1}, {'read_only': 1}, {'is_virtual': 1}):
            self.metadata._fields['Customer'] = [_field('customer_name', 'Customer Name', 'Data', **flag)]
            self.assertFalse(writeback_fields(self.metadata, doctype='Customer')[0].selectable)
            with self.assertRaises(ValueError):
                validate_draft_writeback_source('Customer', 'customer_name', self.metadata)
        self.metadata._fields['Customer'] = []
        with self.assertRaises(ValueError):
            validate_draft_writeback_source('Customer', 'customer_name', self.metadata)

    def test_writeback_catalog_filters_before_metadata_and_preserves_all_allowed_pages(self):
        from hero_integration import api

        class Mixed(_Metadata):
            def doctypes(self):
                return [dict(name=name, module='Acme', app_name='secret_app' if name.startswith('Denied') else 'acme_app')
                        for name in ('Denied A', 'Allowed A', 'Denied B', 'Allowed B')]

            def can_write(self, name):
                return name.startswith('Allowed')

            def fields(self, name):
                if not self.can_write(name):
                    raise PermissionError('denied candidate must not be enumerated')
                return [_field(f'field_{index:03}', f'Field {index}', 'Data') for index in range(60)]

        actor = SimpleNamespace(only_for=lambda role: None, get_all=lambda *args, **kwargs: [], _=lambda text: text)
        snapshot = SimpleNamespace(name='synthetic')
        def call(metadata):
            with patch.object(api, 'frappe', actor), patch.object(api, '_FrappePickerMetadata', return_value=metadata), patch.object(api, '_validated_snapshot', return_value=(snapshot, {})):
                return api.writeback_mapper_catalog('synthetic')

        result = call(Mixed())
        self.assertEqual(result['apps'], ['acme_app'])
        self.assertEqual([item['doctype'] for item in result['doctypes']], ['Allowed A', 'Allowed B'])
        paths = [(item['doctype'], item['path']) for item in result['fields']]
        self.assertEqual(len(paths), 120)
        self.assertEqual(len(set(paths)), 120)
        self.assertEqual(paths[0], ('Allowed A', 'field_000'))
        self.assertEqual(paths[50], ('Allowed A', 'field_050'))
        self.assertEqual(paths[100], ('Allowed B', 'field_040'))
        self.assertEqual(paths[-1], ('Allowed B', 'field_059'))

        class Denied(Mixed):
            def can_write(self, name):
                return False
        empty = call(Denied())
        self.assertEqual((empty['apps'], empty['doctypes'], empty['fields']), ([], [], []))

        class Broken(Mixed):
            def fields(self, name):
                raise RuntimeError('unexpected metadata failure')
        with self.assertRaisesRegex(RuntimeError, 'unexpected metadata failure'):
            call(Broken())

    def test_contract_blockers_follow_user_language_without_changing_gate(self):
        from hero_integration import api
        from hero_integration.services.frappe_writeback import DEFINITIONS

        operation = next(iter(DEFINITIONS.values())).operation
        contract = SimpleNamespace(name="CONTRACT-001", operation_name=operation, status="Draft")
        source = "Technical contract {0} is {1}; it must be approved before this mapping can be activated."
        translated = "Technischer Vertrag {0}: Status {1}. Mapping benötigt eine Freigabe."
        messages = {source: translated, "Draft": "Entwurf"}
        actor = SimpleNamespace(only_for=lambda role: None, get_all=lambda *args, **kwargs: [contract],
                                _=lambda text: messages.get(text, text))
        with patch.object(api, 'frappe', actor), patch.object(api, '_FrappePickerMetadata', return_value=self.metadata), \
             patch.object(api, '_validated_snapshot', return_value=(SimpleNamespace(name='synthetic'), {})):
            result = api.writeback_mapper_catalog('synthetic')
        state = next(item for item in result['contracts'] if item['operation'] == operation)
        self.assertFalse(state['activation_ready'])
        self.assertEqual(state['blocker'], translated.format('CONTRACT-001', 'Entwurf'))
        self.assertIs(state['contracts'][0], contract)

    def test_installed_apps_and_doctypes_are_runtime_metadata_not_an_erpnext_list(self):
        result = catalog(self.metadata)

        self.assertEqual(result["apps"], ["acme_app", "erpnext"])
        cases = next(item for item in result["doctypes"] if item["doctype"] == "Acme Case")
        self.assertTrue(cases["selectable"])
        self.assertFalse(next(item for item in result["doctypes"] if item["doctype"] == "Acme Settings")["selectable"])
        self.assertFalse(next(item for item in result["doctypes"] if item["doctype"] == "Denied Record")["selectable"])

    def test_foreign_standard_and_custom_fields_are_draft_only_and_links_stay_blocked(self):
        target_fields = fields(self.metadata, app_name="acme_app", doctype="Acme Case")

        subject = next(item for item in target_fields if item.path == "subject")
        custom = next(item for item in target_fields if item.path == "custom_hero_category")
        link = next(item for item in target_fields if item.path == "customer")
        children = next(item for item in target_fields if item.path == "items")
        self.assertTrue(subject.selectable)
        self.assertTrue(custom.selectable)
        self.assertFalse(subject.activation_allowed)
        self.assertIn("Draft proposal", subject.reason)
        self.assertFalse(link.selectable)
        self.assertIn("resolver", link.reason)
        self.assertFalse(children.selectable)

    def test_draft_validation_uses_type_and_permission_without_opening_an_active_write_path(self):
        target = validate_draft_target("HeroCustomers", "string|null", "Acme Case", "subject", self.metadata)

        self.assertEqual((target.app_name, target.doctype, target.path), ("acme_app", "Acme Case", "subject"))
        self.assertFalse(target.activation_allowed)
        with self.assertRaisesRegex(ValueError, "incompatible"):
            validate_draft_target("HeroCustomers", "boolean|null", "Acme Case", "subject", self.metadata)
        with self.assertRaisesRegex(ValueError, "No write permission"):
            validate_draft_target("HeroCustomers", "string|null", "Denied Record", "subject", self.metadata)

    def test_suggestions_cover_a_foreign_app_but_mark_them_draft_only(self):
        schema = {"__schema": {"types": [
            {"kind": "OBJECT", "name": "Query", "fields": [
                {"name": "contacts", "type": {"kind": "LIST", "ofType": {"kind": "OBJECT", "name": "Contact"}}},
            ]},
            {"kind": "OBJECT", "name": "Contact", "fields": [
                {"name": "category", "type": {"kind": "SCALAR", "name": "String"}},
            ]},
        ]}}
        matches = suggest_from_source("HeroCustomers", "category", schema, self.metadata, "Acme Case")

        match = next(item for item in matches if item["target_field"] == "custom_hero_category")
        self.assertEqual((match["target_app"], match["target_doctype"]), ("acme_app", "Acme Case"))
        self.assertTrue(match["draft_only"])
        self.assertIn(match["confidence"], {"high", "medium", "low"})
        self.assertTrue(all(item["requires_approval"] for item in matches))
        self.assertTrue({"customer", "items"}.isdisjoint(item["target_field"] for item in matches))

    def test_field_search_includes_type_and_description_within_the_selected_doctype(self):
        self.metadata._fields["Acme Case"].append(_field(
            "custom_reference", "Reference", "Data", is_custom_field=1, description="External partner key",
        ))

        results = fields(self.metadata, app_name="acme_app", doctype="Acme Case", search="partner")

        self.assertEqual([item.path for item in results], ["custom_reference"])
        self.assertEqual(fields(self.metadata, app_name="acme_app", doctype="Acme Case", search="Data")[0].doctype, "Acme Case")

    def test_picker_page_caps_visible_results_at_fifty_and_keeps_more_results_reachable(self):
        many = [PickerField(f"custom_field_{index}", f"Field {index}", "Data", True, "", True, "Draft") for index in range(123)]

        first = picker_page(many, start=0, page_size=999)
        second = picker_page(many, start=50, page_size=50)
        last = picker_page(many, start=100, page_size=50)

        self.assertEqual((len(first["items"]), first["total"], first["has_more"]), (50, 123, True))
        self.assertEqual((second["items"][0]["path"], len(second["items"])), ("custom_field_50", 50))
        self.assertEqual((last["items"][-1]["path"], last["has_more"]), ("custom_field_122", False))


class _Metadata:
    _doctypes = (
        {"name": "Customer", "module": "Selling", "app_name": "erpnext"},
        {"name": "Acme Case", "module": "Acme", "app_name": "acme_app"},
        {"name": "Acme Settings", "module": "Acme", "app_name": "acme_app", "issingle": 1},
        {"name": "Acme Row", "module": "Acme", "app_name": "acme_app", "istable": 1},
        {"name": "Denied Record", "module": "Acme", "app_name": "acme_app"},
    )

    def __init__(self):
        self._fields = {
            "Acme Case": [
                _field("subject", "Subject", "Data"),
                _field("custom_hero_category", "HERO Category", "Data", is_custom_field=1),
                _field("customer", "Customer", "Link", options="Customer"),
                _field("items", "Items", "Table", options="Acme Row"),
            ],
        }

    def doctypes(self):
        return self._doctypes

    @staticmethod
    def can_write(doctype):
        return doctype != "Denied Record"

    def fields(self, doctype):
        if doctype in self._fields:
            return self._fields[doctype]
        return [_field("subject", "Subject", "Data")]


def _field(fieldname, label, fieldtype, **kwargs):
    values = {"fieldname": fieldname, "label": label, "fieldtype": fieldtype, "read_only": 0, "reqd": 0, "options": "", "is_custom_field": 0, "description": ""}
    values.update(kwargs)
    return SimpleNamespace(**values)
