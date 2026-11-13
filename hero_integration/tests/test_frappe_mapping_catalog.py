from __future__ import annotations

import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services.frappe_mapping_catalog import doctypes, fields, validate_draft_target, writeback_fields, validate_draft_writeback_source


class TestFrappeMappingCatalog(unittest.TestCase):
    def setUp(self):
        self.metadata = _Metadata()

    def test_both_picker_adapters_check_denied_doctypes_without_throwing(self):
        from hero_integration import api
        from hero_integration.services.frappe_mapping_catalog import FrappePickerMetadata

        calls, messages = [], []
        def permission(doctype, ptype, throw=False):
            calls.append((doctype, ptype, throw))
            if doctype == 'Allowed':
                return True
            if throw:
                messages.append(doctype)
                raise PermissionError(doctype)
            return False
        frappe = SimpleNamespace(get_meta=lambda _name: SimpleNamespace(istable=False),
                                 has_permission=permission, PermissionError=PermissionError)
        with patch.object(api, 'frappe', frappe), patch.dict(sys.modules, {'frappe': frappe}):
            for adapter in (api._FrappePickerMetadata(), FrappePickerMetadata()):
                self.assertTrue(adapter.can_write('Allowed'))
                self.assertFalse(adapter.can_write('Denied'))
        self.assertEqual(messages, [])
        self.assertTrue(all(not throw for _, _, throw in calls))

    def test_metadata_scans_doctypes_once_and_keeps_app_and_search_filters(self):
        class Counting(_Metadata):
            def __init__(self):
                super().__init__()
                self.scans = 0
            def doctypes(self):
                self.scans += 1
                return super().doctypes()
        metadata = Counting()
        result = doctypes(metadata, app_name='acme_app', search='case')
        self.assertEqual(metadata.scans, 1)
        self.assertEqual([entry.doctype for entry in result], ['Acme Case'])

    def test_field_catalog_skips_unwritable_and_non_target_doctypes(self):
        class Strict(_Metadata):
            def fields(self, name):
                if name in {'Denied Record', 'Acme Row', 'Acme Settings'}:
                    raise AssertionError('disabled DocType must not be enumerated')
                return super().fields(name)
        result = fields(Strict())
        self.assertTrue(result)
        self.assertTrue(all(item.doctype not in {'Denied Record', 'Acme Row', 'Acme Settings'} for item in result))
        with self.assertRaisesRegex(ValueError, 'No write permission'):
            validate_draft_target('HeroCustomers', 'string|null', 'Denied Record', 'subject', Strict())

    def test_visible_catalog_reasons_have_german_translations(self):
        import csv
        from pathlib import Path

        translation_file = Path(__file__).resolve().parents[1] / 'translations' / 'de.csv'
        with translation_file.open(encoding='utf-8', newline='') as stream:
            messages = dict(csv.reader(stream))
        entries = [item.__dict__ for item in doctypes(self.metadata)]
        reasons = [entry['reason'] for entry in entries if entry['reason']]
        for entry in entries:
            if entry['selectable']:
                reasons.extend(item.reason for item in fields(self.metadata, doctype=entry['doctype']) if item.reason)
        self.assertTrue(reasons)
        self.assertEqual([reason for reason in reasons if reason not in messages], [])

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

    def test_hidden_fields_are_selectable_for_inbound_writeback_and_send(self):
        from hero_integration.services.frappe_mapping_catalog import send_source_fields

        self.metadata._fields['Customer'] = [
            _field('customer_name', 'Customer Name', 'Data', hidden=1),
            _field('alias', 'Alias', 'Data', hidden=1),
        ]
        self.assertTrue(next(item for item in fields(self.metadata, doctype='Customer') if item.path == 'alias').selectable)
        self.assertTrue(validate_draft_writeback_source('Customer', 'customer_name', self.metadata).selectable)
        self.assertTrue(next(item for item in send_source_fields(self.metadata, 'Customer') if item.path == 'alias').selectable)

    def test_reviewed_source_requires_accessible_real_field_and_doctype(self):
        for flag in ({'permlevel': 1}, {'read_only': 1}, {'is_virtual': 1}):
            self.metadata._fields['Customer'] = [_field('customer_name', 'Customer Name', 'Data', **flag)]
            self.assertFalse(writeback_fields(self.metadata, doctype='Customer')[0].selectable)
            with self.assertRaises(ValueError):
                validate_draft_writeback_source('Customer', 'customer_name', self.metadata)
        self.metadata._fields['Customer'] = []
        with self.assertRaises(ValueError):
            validate_draft_writeback_source('Customer', 'customer_name', self.metadata)



    def test_installed_apps_and_doctypes_are_runtime_metadata_not_an_erpnext_list(self):
        result = [item.__dict__ for item in doctypes(self.metadata)]

        self.assertEqual(sorted({item["app_name"] for item in result}), ["acme_app", "erpnext"])
        cases = next(item for item in result if item["doctype"] == "Acme Case")
        self.assertTrue(cases["selectable"])
        self.assertFalse(next(item for item in result if item["doctype"] == "Acme Settings")["selectable"])
        self.assertFalse(next(item for item in result if item["doctype"] == "Denied Record")["selectable"])


    def test_draft_validation_uses_type_and_permission_without_opening_an_active_write_path(self):
        target = validate_draft_target("HeroCustomers", "string|null", "Acme Case", "subject", self.metadata)

        self.assertEqual((target.app_name, target.doctype, target.path), ("acme_app", "Acme Case", "subject"))
        self.assertFalse(target.activation_allowed)
        with self.assertRaisesRegex(ValueError, "incompatible"):
            validate_draft_target("HeroCustomers", "boolean|null", "Acme Case", "subject", self.metadata)
        with self.assertRaisesRegex(ValueError, "No write permission"):
            validate_draft_target("HeroCustomers", "string|null", "Denied Record", "subject", self.metadata)


    def test_field_search_includes_type_and_description_within_the_selected_doctype(self):
        self.metadata._fields["Acme Case"].append(_field(
            "custom_reference", "Reference", "Data", is_custom_field=1, description="External partner key",
        ))

        results = fields(self.metadata, app_name="acme_app", doctype="Acme Case", search="partner")

        self.assertEqual([item.path for item in results], ["custom_reference"])
        self.assertEqual(fields(self.metadata, app_name="acme_app", doctype="Acme Case", search="Data")[0].doctype, "Acme Case")



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
