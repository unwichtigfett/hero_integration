from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services import adoption, mapping_exchange, schema_provisioning


SCHEMA = {"__schema": {"types": [{"kind": "OBJECT", "name": "Query", "fields": []}]}}
FINGERPRINT = analyze_snapshot(SCHEMA).fingerprint


def package():
    return {
        "format_version": mapping_exchange.FORMAT_VERSION,
        "schema_fingerprint": FINGERPRINT,
        "schema": {
            "custom_doctypes": [
                {"id": "doctype:detail", "name": "HERO External Detail", "label": "External Detail", "is_child": False, "fields": [
                    {"id": "field:custom_hero_customer", "fieldname": "custom_hero_customer", "label": "Customer", "fieldtype": "Link", "options": "Customer"},
                    {"id": "field:custom_hero_rows", "fieldname": "custom_hero_rows", "label": "Rows", "fieldtype": "Table", "options": "HERO External Detail Row"},
                ]},
                {"id": "doctype:detail-row", "name": "HERO External Detail Row", "label": "External Detail Row", "is_child": True, "fields": [
                    {"id": "field:custom_hero_value", "fieldname": "custom_hero_value", "label": "Value", "fieldtype": "Data", "options": ""},
                ]},
            ],
            "custom_fields": [
                {"id": "field:customer-schema-value", "doctype": "Customer", "fieldname": "custom_hero_schema_value", "label": "Schema Value", "fieldtype": "Data", "options": "", "insert_after": "customer_name"},
            ],
            "relations": [
                {"id": "relation:detail-customer", "doctype": "HERO External Detail", "field_id": "field:custom_hero_customer", "fieldname": "custom_hero_customer", "relation_type": "Link", "target_doctype": "Customer", "dependencies": ["doctype:detail"]},
                {"id": "relation:detail-rows", "doctype": "HERO External Detail", "field_id": "field:custom_hero_rows", "fieldname": "custom_hero_rows", "relation_type": "Table", "target_doctype": "HERO External Detail Row", "dependencies": ["doctype:detail", "doctype:detail-row"]},
            ],
        },
        "mappings": [
            {"definition_id": "customer-schema-value", "operation": "HeroCustomers", "source_path": "category", "source_type": "string|null", "target_doctype": "Customer", "target_field": "custom_hero_schema_value", "purpose": "Field", "transformation": "Trim", "relation": "", "priority": 5, "schema_dependencies": ["field:customer-schema-value"], "mapping_revision": 1},
        ],
    }


class TestSchemaProvisioning(unittest.TestCase):
    def setUp(self):
        self.frappe = _Frappe()
        self.patches = [
            patch.object(mapping_exchange, "frappe", self.frappe),
            patch.object(schema_provisioning, "frappe", self.frappe),
            patch.object(adoption, "frappe", self.frappe),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_preview_is_non_mutating_and_groups_schema_and_mapping_changes(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", json.dumps(package()))

        self.assertEqual(self.frappe.mutations, [])
        self.assertEqual(preview["counts"]["New"], 6)
        self.assertEqual({item["group"] for item in preview["items"]}, {
            "New Custom DocTypes", "New Custom Fields", "Relations / Link Fields", "New Field Mappings",
        })
        with self.assertRaisesRegex(mapping_exchange.MappingExchangeError, "bulk adoption"):
            mapping_exchange.import_json("SYNTHETIC", json.dumps(package()))

    def test_confirmed_bulk_adoption_provisions_schema_before_mapping_activation(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", json.dumps(package()))
        selected = [item["id"] for item in preview["items"] if item["selectable"]]

        result = schema_provisioning.adopt_package("SYNTHETIC", json.dumps(package()), selected, confirmed=True)

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["created"]["custom_doctypes"], ["HERO External Detail Row", "HERO External Detail"])
        self.assertEqual(result["created"]["custom_fields"], ["Customer.custom_hero_schema_value"])
        self.assertEqual(result["created"]["mappings"], ["customer-schema-value"])
        self.assertEqual(self.frappe.migrations, 0)
        self.assertEqual(self.frappe.mappings["customer-schema-value"]["status"], "Active")

    def test_relation_can_be_selected_separately_from_its_custom_doctype(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", json.dumps(package()))
        doctypes = ["doctype:detail", "doctype:detail-row"]
        schema_provisioning.adopt_package("SYNTHETIC", json.dumps(package()), doctypes, confirmed=True)
        self.assertIsNone(self.frappe.get_meta("HERO External Detail").get_field("custom_hero_customer"))

        follow_up = schema_provisioning.preview_package("SYNTHETIC", json.dumps(package()))
        relations = [item["id"] for item in follow_up["items"] if item["group"] == "Relations / Link Fields"]
        result = schema_provisioning.adopt_package("SYNTHETIC", json.dumps(package()), relations, confirmed=True)

        self.assertEqual(len(result["created"]["custom_fields"]), 2)
        self.assertEqual(self.frappe.get_meta("HERO External Detail").get_field("custom_hero_customer").fieldtype, "Link")

    def test_repeated_import_is_unchanged_and_does_not_duplicate_schema(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", json.dumps(package()))
        schema_provisioning.adopt_package("SYNTHETIC", json.dumps(package()), [item["id"] for item in preview["items"] if item["selectable"]], True)

        repeated = schema_provisioning.preview_package("SYNTHETIC", json.dumps(package()))

        self.assertEqual(repeated["counts"]["New"], 0)
        self.assertEqual(len(self.frappe.doctypes), 4)  # Customer, DocType, and two HERO DocTypes
        self.assertEqual(len(self.frappe.custom_fields), 1)

    def test_read_only_custom_field_is_previewed_and_adopted_idempotently(self):
        draft = package()
        draft["schema"]["custom_fields"][0]["read_only"] = 1
        draft["mappings"] = []

        preview = schema_provisioning.preview_package("SYNTHETIC", json.dumps(draft))
        field = next(item for item in preview["items"] if item["id"] == "field:customer-schema-value")
        self.assertEqual(field["state"], "New")
        definitions = schema_provisioning._definitions(draft["schema"])
        self.assertEqual(schema_provisioning._PlannedMetadata(definitions).field("Customer", "custom_hero_schema_value").read_only, 1)

        schema_provisioning.adopt_package("SYNTHETIC", json.dumps(draft), [field["id"]], True)
        repeated = schema_provisioning.preview_package("SYNTHETIC", json.dumps(draft))
        field = next(item for item in repeated["items"] if item["id"] == "field:customer-schema-value")
        self.assertEqual(field["state"], "Unchanged")
        self.assertEqual(self.frappe.custom_fields[("Customer", "custom_hero_schema_value")]["read_only"], 1)

    def test_schema_and_mapping_export_can_fill_a_fresh_site_checklist(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", json.dumps(package()))
        schema_provisioning.adopt_package("SYNTHETIC", json.dumps(package()), [item["id"] for item in preview["items"] if item["selectable"]], True)

        exported = json.loads(mapping_exchange.export_json("SYNTHETIC"))
        fresh = _Frappe()
        with patch.object(mapping_exchange, "frappe", fresh), patch.object(schema_provisioning, "frappe", fresh):
            imported = schema_provisioning.preview_package("SYNTHETIC", json.dumps(exported))

        self.assertEqual(exported["schema"], package()["schema"])
        self.assertEqual(imported["counts"]["New"], 6)

    def test_missing_dependency_and_unconfirmed_adoption_are_rejected_without_mutation(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", json.dumps(package()))
        mapping_id = next(item["id"] for item in preview["items"] if item["kind"] == "mapping")
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "confirmation"):
            schema_provisioning.adopt_package("SYNTHETIC", json.dumps(package()), [mapping_id], False)
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "requires"):
            schema_provisioning.adopt_package("SYNTHETIC", json.dumps(package()), [mapping_id], True)
        self.assertEqual(self.frappe.mutations, [])

    def test_foreign_target_conflict_and_executable_payload_are_rejected(self):
        self.frappe.doctypes["HERO External Detail"] = {"custom": 0, "module": "ERPNext", "istable": 0, "fields": {}}
        preview = schema_provisioning.preview_package("SYNTHETIC", json.dumps(package()))
        self.assertEqual(next(item for item in preview["items"] if item["id"] == "doctype:detail")["state"], "Conflict")
        bad = package()
        bad["schema"]["custom_fields"][0]["script"] = "frappe.delete_doc()"
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "unknown or missing"):
            schema_provisioning.preview_package("SYNTHETIC", json.dumps(bad))

    def test_missing_schema_permission_rejects_before_any_write(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", json.dumps(package()))
        selected = [item["id"] for item in preview["items"] if item["selectable"]]
        self.frappe.has_permission = lambda doctype, permission, throw=False: (_ for _ in ()).throw(PermissionError(doctype))

        with self.assertRaises(PermissionError):
            schema_provisioning.adopt_package("SYNTHETIC", json.dumps(package()), selected, True)

        self.assertEqual(self.frappe.mutations, [])

    def test_failed_adoption_is_recorded_and_can_resume_after_schema_exists(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", json.dumps(package()))
        selected = [item["id"] for item in preview["items"] if item["selectable"]]
        self.frappe.fail_mapping_insert = True

        with self.assertRaises(RuntimeError):
            adoption.start("SYNTHETIC", json.dumps(package()), selected)

        adoption_name = next(iter(self.frappe.adoptions))
        self.assertEqual(self.frappe.adoptions[adoption_name]["status"], "Failed")
        self.assertNotIn("customer-schema-value", self.frappe.mappings)
        self.frappe.fail_mapping_insert = False
        resumed = adoption.execute(adoption_name)
        self.assertEqual(resumed["status"], "COMPLETED")
        self.assertEqual(self.frappe.mappings["customer-schema-value"]["status"], "Active")
        repeated = adoption.execute(adoption_name)
        self.assertEqual(repeated["status"], "COMPLETED")
        self.assertEqual(len(self.frappe.mappings), 1)


class _Frappe:
    def __init__(self):
        self.snapshot = SimpleNamespace(analysis_status="Validated", snapshot_json=json.dumps(SCHEMA), schema_fingerprint=FINGERPRINT)
        self.doctypes = {
            "Customer": {"custom": 0, "module": "Selling", "istable": 0, "fields": {"customer_name": _Field("Data")}},
            "DocType": {"custom": 0, "module": "Core", "istable": 0, "fields": {}},
        }
        self.custom_fields = {}
        self.mappings = {}
        self.adoptions = {}
        self.mutations = []
        self.migrations = 0
        self.fail_mapping_insert = False
        self.db = SimpleNamespace(savepoint=lambda name: None, rollback=lambda save_point: None, exists=self.exists)

    def exists(self, doctype, filters):
        if doctype == "DocType":
            return filters in self.doctypes
        if doctype == "Custom Field":
            return (filters["dt"], filters["fieldname"]) in self.custom_fields
        return False

    def get_doc(self, *args):
        if args == ("HERO GraphQL Schema Snapshot", "SYNTHETIC"):
            return self.snapshot
        if len(args) == 2 and args[0] == "DocType":
            payload = self.doctypes[args[1]]
            return SimpleNamespace(name=args[1], **payload)
        if len(args) == 2 and args[0] == "HERO Frappe Mapping":
            return _Document(self, self.mappings[args[1]])
        if len(args) == 2 and args[0] == "HERO Mapping Adoption":
            return _Document(self, self.adoptions[args[1]])
        if len(args) == 1 and isinstance(args[0], dict):
            return _Document(self, dict(args[0]))
        raise AssertionError(args)

    def get_all(self, doctype, filters=None, fields=None, order_by=None):
        if doctype == "HERO Frappe Mapping":
            rows = list(self.mappings.values())
        elif doctype == "Custom Field":
            rows = list(self.custom_fields.values())
        else:
            raise AssertionError(doctype)
        for key, value in (filters or {}).items():
            rows = [row for row in rows if row.get(key) == value]
        return [_Row({field: row.get(field, "") for field in fields}) for row in rows]

    @staticmethod
    def has_permission(doctype, permission, throw=False):
        return True

    @staticmethod
    def only_for(role):
        return None

    def get_meta(self, doctype):
        if doctype not in self.doctypes:
            raise ValueError(doctype)
        fields = dict(self.doctypes[doctype]["fields"])
        fields.update({fieldname: _Field(value["fieldtype"], value["options"], value.get("read_only", 0)) for (dt, fieldname), value in self.custom_fields.items() if dt == doctype})
        return SimpleNamespace(get_field=lambda fieldname: fields.get(fieldname))


class _Document:
    def __init__(self, frappe_api, row):
        self.frappe_api = frappe_api
        self.row = row

    def update(self, values):
        self.row.update(values)

    def __getattr__(self, name):
        try:
            return self.row[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def save(self):
        if self.row["doctype"] == "HERO Mapping Adoption":
            self.frappe_api.adoptions[self.row["name"]] = self.row
        else:
            self.frappe_api.mappings[self.row["definition_id"]] = self.row

    def insert(self):
        kind = self.row["doctype"]
        self.frappe_api.mutations.append(kind)
        if kind == "DocType":
            self.frappe_api.doctypes[self.row["name"]] = {
                "custom": self.row["custom"], "module": self.row["module"], "istable": self.row["istable"],
                "fields": {field["fieldname"]: _Field(field["fieldtype"], field["options"]) for field in self.row["fields"]},
            }
        elif kind == "Custom Field":
            self.frappe_api.custom_fields[(self.row["dt"], self.row["fieldname"])] = self.row
        elif kind == "HERO Frappe Mapping":
            if self.frappe_api.fail_mapping_insert:
                raise RuntimeError("synthetic mapping insert failure")
            self.row["name"] = self.row["definition_id"]
            self.row["schema_fingerprint"] = FINGERPRINT
            self.frappe_api.mappings[self.row["definition_id"]] = self.row
        elif kind == "HERO Mapping Adoption":
            self.row["name"] = "HERO-ADOPT-00001"
            self.frappe_api.adoptions[self.row["name"]] = self.row
        else:
            raise AssertionError(kind)

    def __setattr__(self, name, value):
        if name in {"frappe_api", "row"}:
            object.__setattr__(self, name, value)
        else:
            self.row[name] = value


class _Row(dict):
    __getattr__ = dict.__getitem__


class _Field:
    def __init__(self, fieldtype, options="", read_only=0):
        self.fieldtype = fieldtype
        self.options = options
        self.read_only = read_only
        self.is_custom_field = fieldtype != "Data" or True
