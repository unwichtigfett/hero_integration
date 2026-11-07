"""Editable employee rules retain identity, account and query-contract safeguards."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from graphql import build_schema, introspection_from_schema

from hero_integration.services import direct_runtime as runtime
from hero_integration.services import query_mapping
from hero_integration.services.employee_mapping import preview
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mapping_runtime import plan_from_definitions


RECORD = {
    "id": 101, "user_id": 202, "first_name": "Synthetic", "last_name": "Employee",
    "full_name": "Synthetic Employee", "email": "contact@example.invalid",
    "phone": "+490000000", "mobile": "+490000001", "role": None,
    "status": "active", "account_type": "full",
    "user": {"id": 202, "email": "login@example.invalid", "role": "partner",
             "locale": "de_DE", "last_login": "2026-01-02T10:00:00+00:00",
             "created": "2020-01-01T10:00:00+00:00", "modified": "2026-01-02T10:00:00+00:00"},
}

EMPLOYEE_TYPES = """
    type Partner { id: Int! user_id: Int first_name: String last_name: String full_name: String
        email: String phone: String mobile: String role: String status: String account_type: String user: User }
    type User { id: Int! email: String role: String locale: String last_login: String created: String modified: String }
"""


def edited_plan(*, change=None, remove=None, options=None):
    rules = runtime.builtin_plan("HeroEmployees").definitions
    rules = [change(rule) if change else rule for rule in rules if not (remove and remove(rule))]
    return plan_from_definitions("HeroEmployees", rules, target_operation="HeroEmployees",
                                 adapter_options=options or {"version": 2})


def targets(result):
    return {(row["target_doctype"], row["target_field"]): row["value"] for row in result["fields"]}


class TestConfigurableEmployeeValues(unittest.TestCase):
    def setUp(self):
        self.record = deepcopy(RECORD)
        self.plan = runtime.builtin_plan("HeroEmployees")

    def test_default_rules_preserve_separate_contact_and_login_email(self):
        result = preview(self.record, languages={"de"}, plan=self.plan)
        values = targets(result)
        self.assertEqual(result["blockers"], [])
        self.assertEqual(values[("Employee", "company_email")], "contact@example.invalid")
        self.assertEqual(values[("User", "email")], "login@example.invalid")
        self.assertEqual(values[("Employee", "status")], "Active")
        self.assertEqual(values[("User", "language")], "de")

    def test_changed_source_and_target_take_effect(self):
        plan = edited_plan(change=lambda rule: replace(rule, source_path="user.email", target_field="personal_email")
                           if rule.target_field == "company_email" else rule)
        values = targets(preview(self.record, languages={"de"}, plan=plan))
        self.assertNotIn(("Employee", "company_email"), values)
        self.assertEqual(values[("Employee", "personal_email")], "login@example.invalid")

    def test_removed_optional_rule_is_not_restored_by_builtin_manifest(self):
        plan = edited_plan(remove=lambda rule: rule.target_field == "phone")
        self.assertNotIn(("User", "phone"), targets(preview(self.record, languages={"de"}, plan=plan)))

    def test_computed_name_check_uses_configured_name_sources(self):
        self.record.update(preferred_name="Chosen", preferred_full_name="Chosen Employee")
        paths = {"first_name": "preferred_name", "employee_name": "preferred_full_name"}
        plan = edited_plan(change=lambda rule: replace(rule, source_path=paths[rule.target_field])
                           if rule.target_doctype == "Employee" and rule.target_field in paths else rule)
        result = preview(self.record, languages={"de"}, plan=plan)
        self.assertFalse(result["blockers"])
        self.assertEqual(targets(result)[("Employee", "first_name")], "Chosen")
        self.record["preferred_full_name"] = "Synthetic Employee"
        self.assertTrue(preview(self.record, languages={"de"}, plan=plan)["blockers"])

    def test_unknown_status_locale_and_inconsistent_full_name_block(self):
        for change in ({"status": "unknown"}, {"full_name": "Another person"},
                       {"user": {**self.record["user"], "locale": "missing"}}):
            with self.subTest(change=change):
                result = preview({**self.record, **change}, languages={"de"}, plan=self.plan)
                self.assertTrue(result["blockers"])

    def test_empty_locale_does_not_overwrite_local_language(self):
        self.record["user"]["locale"] = ""
        result = preview(self.record, languages={"de"}, plan=self.plan)
        self.assertFalse(result["blockers"])
        self.assertNotIn(("User", "language"), targets(result))

    def test_identity_disagreement_still_blocks_configured_mapping(self):
        self.record["user"]["id"] = 303
        result = preview(self.record, languages={"de"}, plan=self.plan)
        self.assertTrue(result["blockers"])

    def test_explicit_status_value_map_is_used(self):
        original = next(rule for rule in self.plan.definitions if rule.target_field == "status")
        plan = edited_plan(change=lambda rule: replace(rule, transformation="Value Map")
                           if rule.definition_id == original.definition_id else rule,
                           options={"version": 2, "transform_options": {
                               original.definition_id: {"map": {"available": "Active"}}}})
        result = preview({**self.record, "status": "available"}, languages={"de"}, plan=plan)
        self.assertFalse(result["blockers"])
        self.assertEqual(targets(result)[("Employee", "status")], "Active")

    def test_account_access_and_native_relationship_targets_are_protected(self):
        for doctype, field in (("User", "roles"), ("User", "enabled"), ("User", "user_type"),
                               ("Employee", "company"), ("Employee", "user_id"),
                               ("Employee", "date_of_joining")):
            with self.subTest(doctype=doctype, field=field), self.assertRaises(ValueError):
                edited_plan(change=lambda rule: replace(rule, target_doctype=doctype, target_field=field)
                            if rule.target_field == "phone" else rule)

    def test_required_employee_fields_cannot_be_removed(self):
        for doctype, field in (("Employee", "first_name"), ("Employee", "status"), ("User", "email")):
            with self.subTest(doctype=doctype, field=field), self.assertRaises(ValueError):
                edited_plan(remove=lambda rule: (rule.target_doctype, rule.target_field) == (doctype, field))


class TestEmployeeReadSeal(unittest.TestCase):
    def setUp(self):
        schema = build_schema("type Query { partners(first: Int, offset: Int): [Partner!]! }" + EMPLOYEE_TYPES)
        raw = introspection_from_schema(schema)
        self.snapshot = NS(name="employees-snapshot", snapshot_json=raw,
                           schema_fingerprint=analyze_snapshot(raw).fingerprint,
                           source_instance="synthetic", analysis_status="Validated")
        self.read = runtime.builtin_read("HeroEmployees")

    def seal(self, query=None, variables=None):
        service = NS(name="employee-read", query_document=query or self.read.document, operation_name="partners")
        config = {"target_operation": "HeroEmployees", "records_path": "partners", "identity_path": "id",
                  "variables": variables or {}, "page_size": 100}
        return query_mapping.seal_query(service, self.snapshot, config, self.read.plan)

    def test_complete_employee_list_can_be_sealed_without_pagination(self):
        self.assertEqual(self.read.variables, {})
        self.assertEqual(self.seal()["version"], 2)

    def test_missing_user_identity_paths_block_sealing(self):
        for path in ("user_id", "user.id"):
            with self.subTest(path=path):
                fields = runtime.selection(runtime.selected_paths(self.read.plan) - {path})
                with self.assertRaises(ValueError):
                    self.seal("query Employees { partners { " + fields + " } }")

    def test_employee_queries_cannot_filter_or_paginate_the_inventory(self):
        fields = runtime.selection(runtime.selected_paths(self.read.plan))
        with self.assertRaises(ValueError):
            self.seal("query Employees($n: Int!) { partners(first: $n) { " + fields + " } }", {"n": "$page_size"})

    def test_other_direct_areas_still_require_bounded_pagination(self):
        schema = build_schema("""
            type Query { supply_product_versions(first: Int, offset: Int, orderBy: String): [Product!]! }
            type Product { product_id: String! is_deleted: Boolean base_data: BaseData }
            type BaseData { name: String }
        """)
        raw = introspection_from_schema(schema)
        snapshot = NS(name="product-snapshot", snapshot_json=raw, schema_fingerprint=analyze_snapshot(raw).fingerprint,
                      source_instance="synthetic", analysis_status="Validated")
        rules = [rule for rule in runtime.builtin_plan("HeroProducts").definitions
                 if rule.purpose in {"Source Identity", "Record Name"}]
        plan = plan_from_definitions("HeroProducts", rules, target_operation="HeroProducts", adapter_options={"version": 2})
        service = NS(name="products-read", operation_name="supply_product_versions",
                     query_document="query Products { supply_product_versions { product_id is_deleted base_data { name } } }")
        with self.assertRaisesRegex(ValueError, "first/offset"):
            query_mapping.seal_query(service, snapshot, {"records_path": "supply_product_versions", "variables": {}}, plan)


class TestCompanyEmployeeReadSeal(unittest.TestCase):
    def setUp(self):
        schema = build_schema("""
            type Query { company: Company! }
            type Company { partners(first: Int, offset: Int): [Partner!]! }
        """ + EMPLOYEE_TYPES)
        raw = introspection_from_schema(schema)
        self.snapshot = NS(name="company-employees", snapshot_json=raw,
                           schema_fingerprint=analyze_snapshot(raw).fingerprint,
                           source_instance="synthetic", analysis_status="Validated")
        self.read = runtime.builtin_read("HeroEmployees", records_path="company.partners")

    def seal(self, query=None):
        service = NS(name="employee-read", query_document=query or self.read.document, operation_name="company")
        config = {"target_operation": "HeroEmployees", "records_path": "company.partners", "identity_path": "id",
                  "variables": {}, "page_size": 100}
        return query_mapping.seal_query(service, self.snapshot, config, self.read.plan)

    def test_nested_complete_inventory_seals_and_unwraps(self):
        self.assertEqual(self.read.variables, {})
        self.assertEqual(self.seal()["version"], 2)
        self.assertEqual(runtime.records(self.read, {"company": {"partners": [RECORD]}}), [RECORD])

    def test_source_types_follow_company_partners_when_direct_root_is_absent(self):
        self.assertEqual(query_mapping.source_type(self.snapshot.snapshot_json, "HeroEmployees", "user.email"),
                         "string|null")
        self.assertEqual(query_mapping.source_type(self.snapshot.snapshot_json, "HeroEmployees", "id"), "int")

    def test_nested_partner_arguments_cannot_silently_truncate_the_inventory(self):
        query = self.read.document.replace("partners {", "partners(first: 1) {")
        self.assertNotEqual(query, self.read.document)
        with self.assertRaisesRegex(ValueError, "complete source list"):
            self.seal(query)

    def test_nested_inventory_still_requires_both_user_identity_paths(self):
        for path in ("user_id", "user.id"):
            fields = runtime.selection(runtime.selected_paths(self.read.plan) - {path})
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.seal("query Employees { company { partners { " + fields + " } } }")


class TestEmployeeMappingVersion(unittest.TestCase):
    def test_missing_approved_mapping_blocks_before_schema_or_remote_access(self):
        from hero_integration.services import employee_migration as migration

        fake = NS(only_for=Mock())
        client = Mock()
        with patch.object(migration, "frappe", fake), \
                patch.object(runtime, "approved_read", return_value=None), \
                patch.object(migration, "preflight") as preflight:
            with self.assertRaisesRegex(ValueError, "create and approve"):
                migration.sync_employees(client, source_instance="synthetic", run_name="new-run")
        preflight.assert_not_called()
        client.query.assert_not_called()

    def test_resume_rejects_changed_rules_before_reading_or_writing(self):
        from hero_integration.services import employee_migration as migration

        original = runtime.builtin_read("HeroEmployees")
        changed = replace(original, plan=edited_plan(remove=lambda rule: rule.target_field == "phone"))
        self.assertNotEqual(original.mapping_version, changed.mapping_version)
        old_stamp = migration._digest(["Synthetic Company", migration.JOINING_DATE, "snapshot",
                                       original.document, original.mapping_version, migration.VERSION])
        db = NS(get_default=Mock(return_value=None),
                get_value=Mock(return_value=json.dumps({"stamp": old_stamp, "done_ids": [101]})),
                set_default=Mock(), commit=Mock())
        fake = NS(only_for=Mock(), db=db)
        client = Mock()
        with patch.object(migration, "frappe", fake), \
                patch.object(migration.runtime, "resolve_read", return_value=changed), \
                patch.object(migration, "preflight", return_value=("Synthetic Company", "snapshot", original.document, "partners")):
            with self.assertRaisesRegex(migration.EmployeeMigrationBlocked, "mapping changed"):
                migration.sync_employees(client, source_instance="synthetic", run_name="existing-run")
        client.query.assert_not_called()
        db.set_default.assert_not_called()
        db.commit.assert_not_called()

    def test_saved_binding_records_the_actual_mapping_version(self):
        from hero_integration.services import employee_migration as migration

        link = NS(target_field_hashes="{}", update=Mock(), save=Mock())
        employee = {"first_name": "Synthetic"}
        doc = NS(doctype="Employee", name="EMP-1", get=employee.get)
        fake = NS(utils=NS(now_datetime=lambda: "2026-11-03 12:00:00"))
        version = runtime.builtin_read("HeroEmployees").mapping_version
        with patch.object(migration, "frappe", fake):
            migration._write_link(link, "synthetic", "partner", 101, doc, employee, RECORD, version)
        self.assertEqual(link.update.call_args.args[0]["mapping_version"], version)
        link.save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
