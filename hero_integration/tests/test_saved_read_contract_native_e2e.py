from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from graphql import build_schema, introspection_from_schema, parse, print_ast

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration.services import migration
from hero_integration.services.introspection import endpoint_fingerprint
from hero_integration.services.read_contract import ReadContractError
from hero_integration.sources.hero import CUSTOMER_QUERY_V1


class TestSavedReadContractNativeE2E(IntegrationTestCase):
    """The external transport is synthetic; service, mapper, adapter and DB writes are real."""

    def setUp(self):
        super().setUp()
        self.user = frappe.session.user
        self.token = "rc16-e2e-" + frappe.generate_hash(length=10)
        self.created: dict[str, list[str]] = {}
        self.source_id = str(int(frappe.generate_hash(length=7), 16))
        frappe.set_user("Administrator")
        self.addCleanup(frappe.set_user, self.user)

    def tearDown(self):
        runs = self.created.get("HERO Integration Run", [])
        if runs:
            frappe.db.delete("HERO Integration Record", {"run": ["in", runs]})
        parents = frappe.get_all("HERO Object Binding", filters={"source_instance": "synthetic",
            "external_object_id": self.source_id}, pluck="name")
        if parents:
            frappe.db.delete("HERO Field Binding", {"object_binding": ["in", parents]})
            frappe.db.delete("HERO Object Binding", {"name": ["in", parents]})
        frappe.db.delete(
            "HERO Integration Link",
            {"source_type": "customer", "source_id": self.source_id, "source_instance": "synthetic"},
        )
        frappe.db.delete("Customer", {"customer_name": ["like", f"{self.token}%"]})
        for key in self.created.get("HERO Runtime Plan", []):
            frappe.db.delete("HERO Runtime Plan Audit", {"runtime_plan": key})
            frappe.db.delete("HERO Runtime Plan", {"plan_key": key})
        frappe.db.delete("HERO Sync Change Event", {"source_instance": "synthetic", "external_id": self.source_id})
        for doctype in (
            "HERO Integration Run",
            "HERO GraphQL Service",
            "HERO GraphQL Mapping",
            "HERO Frappe Mapping",
            "HERO GraphQL Schema Snapshot",
        ):
            names = self.created.get(doctype, [])
            if names:
                frappe.db.delete(doctype, {"name": ["in", names]})
        frappe.db.commit()
        super().tearDown()

    def _insert(self, payload: dict[str, object]):
        doc = frappe.get_doc(payload).insert()
        self.created.setdefault(doc.doctype, []).append(doc.name)
        return doc

    def _saved_contract(self, *, definition_ids: list[str] | None = None, suffix: str = ""):
        operation = "contacts"
        snapshot = self._insert(
            {
                "doctype": "HERO GraphQL Schema Snapshot",
                "source_instance": "synthetic",
                "endpoint_fingerprint": endpoint_fingerprint("https://synthetic.invalid/graphql", "synthetic"),
                "snapshot_json": json.dumps(introspection_from_schema(build_schema("""
                    type Query {
                        contacts(first: Int!, offset: Int!, orderBy: String!, show_deleted: Boolean!, ids: [Int!]): [Contact]
                    }
                    type Contact {
                        id: Int! nr: String first_name: String last_name: String full_name: String
                        email: String phone_mobile: String phone_home: String company_name: String
                        category: String type: String is_contact_person: Boolean is_deleted: Boolean
                        address: Address
                    }
                    type Address { id: Int street: String zipcode: String city: String country: Country }
                    type Country { code: String }
                """))),
            }
        )
        definitions = [
            ("id", "int", "Customer", "custom_hero_source_id", "Source Identity", "Positive Int To Text"),
            ("full_name", "string|null", "Customer", "customer_name", "Record Name", "Trim"),
            ("company_name", "string|null", "Customer", "customer_type", "Customer Type", "Customer Type From Company Name"),
            ("is_deleted", "boolean|null", "HERO Integration Link", "sync_status", "Lifecycle Flag", "Identity"),
        ]
        definitions.append(("category", "string|null", "Customer", "customer_details", "Field", "Trim"))
        ids = []
        for index, (path, source_type, doctype, field, purpose, transform) in enumerate(definitions):
            item = self._insert(
                {
                    "doctype": "HERO Frappe Mapping",
                    "operation": "HeroCustomers",
                    "definition_id": f"{self.token}{suffix}-{index}",
                    "source_path": path,
                    "source_type": source_type,
                    "target_doctype": doctype,
                    "target_field": field,
                    "purpose": purpose,
                    "transformation": transform,
                    "schema_snapshot": snapshot.name,
                    "status": "Active",
                }
            )
            ids.append(item.definition_id)
        ignored = self._insert(
            {
                "doctype": "HERO Frappe Mapping",
                "operation": "HeroCustomers",
                "definition_id": f"{self.token}{suffix}-ignored",
                "source_path": "nr",
                "source_type": "string|null",
                "target_doctype": "Customer",
                "target_field": "customer_name",
                "purpose": "Record Name",
                "transformation": "Trim",
                "priority": 0,
                "schema_snapshot": snapshot.name,
                "status": "Active",
            }
        )
        selected = definition_ids if definition_ids is not None else ids
        mapping = self._insert(
            {
                "doctype": "HERO GraphQL Mapping",
                "schema_snapshot": snapshot.name,
                "operation_kind": "QUERY",
                "operation_name": operation,
                "direction": "QUERY_TO_FRAPPE",
                "mapping_json": json.dumps(
                    {
                        "target_operation": "HeroCustomers",
                        "records_path": operation,
                        "identity_path": "id",
                        "mapping_definition_ids": selected,
                        "variables": {"first": "$page_size", "offset": "$offset", "orderBy": "id", "showDeleted": True},
                        "page_size": 10,
                    }
                ),
                "status": "Active",
            }
        )
        service = self._insert(
            {
                "doctype": "HERO GraphQL Service",
                "service_name": self.token + suffix,
                "operation_name": "HeroCustomers",
                "query_document": CUSTOMER_QUERY_V1,
                "schema_snapshot": snapshot.name,
                "status": "Validated",
            }
        )
        service.read_mapping = mapping.name
        service.save()
        frappe.db.set_value("HERO GraphQL Service", service.name, {
            "execution_status": "Approved", "approved_service_revision": service.service_revision,
            "approved_mapping_revision": mapping.mapping_revision,
        })
        service.reload()
        return service, mapping, ignored

    def _run(self, service, mapping):
        run = self._insert(
            {
                "doctype": "HERO Integration Run",
                "action": "Customer Migration",
                "mode": "Execute",
                "graphql_service": service.name,
                "service_revision": service.service_revision,
                "graphql_mapping": mapping.name,
                "mapping_revision": mapping.mapping_revision,
            }
        )
        with patch.object(migration, "get_configuration", return_value=self._configuration()):
            migration.bind_run_checkpoint(run)
        run.save()
        return run

    @staticmethod
    def _configuration():
        return SimpleNamespace(endpoint=SimpleNamespace(url="https://synthetic.invalid/graphql"), api_token="synthetic",
                               timeout_seconds=1, source_instance="synthetic", source_binding_revision=0,
                               source_binding_confirmed=True)

    def _execute(self, run, record):
        _Client.calls = []
        _Client.record = record
        with (
            patch.object(
                migration,
                "get_configuration",
                return_value=self._configuration(),
            ),
            patch.object(migration, "HeroGraphQLClient", _Client),
        ):
            migration.execute_run(run.name)

    def test_saved_synthetic_service_persists_only_its_selected_customer_mapping(self):
        service, mapping, ignored = self._saved_contract()
        run = self._run(service, mapping)
        record = {
            "id": int(self.source_id),
            "nr": self.token + "-number",
            "full_name": self.token + "-customer",
            "company_name": "",
            "is_deleted": False, "is_contact_person": False,
        }

        self._execute(run, record)

        run.reload()
        self.assertEqual(run.state, "Completed", (run.error_message, frappe.get_all("HERO Integration Record",
            filters={"run": run.name}, fields=["result", "detail"])))
        customer = frappe.get_doc("Customer", {"customer_name": record["full_name"]})
        link = frappe.get_doc(
            "HERO Integration Link",
            {"source_type": "customer", "source_id": self.source_id, "source_instance": "synthetic"},
        )
        self.assertEqual((run.graphql_service, run.service_revision), (service.name, service.service_revision))
        self.assertEqual((run.graphql_mapping, run.mapping_revision), (mapping.name, mapping.mapping_revision))
        self.assertEqual(_Client.calls, [
            (print_ast(parse(service.query_document)), {"first": 10, "offset": 0, "orderBy": "id", "showDeleted": True}),
            (print_ast(parse(service.query_document)), {"first": 10, "offset": 1, "orderBy": "id", "showDeleted": True}),
        ])
        self.assertEqual(customer.customer_name, record["full_name"])
        self.assertNotEqual(customer.customer_name, record["nr"])
        self.assertEqual((link.target_doctype, link.target_name), ("Customer", customer.name))
        claims = frappe.get_all("HERO Object Binding", filters={"source_instance": "synthetic",
            "external_object_id": self.source_id}, fields=["object_type", "target_name", "mapping_definition_id"])
        self.assertEqual([(row.object_type, row.target_name, row.mapping_definition_id) for row in claims],
                         [("Contact", customer.name, mapping.definition_id)])
        self.assertNotIn(ignored.definition_id, json.loads(mapping.mapping_json)["mapping_definition_ids"])

    def test_inventory_intake_reuses_the_saved_customer_adapter_and_existing_bindings(self):
        from hero_integration.services.execution_runtime import InventoryIntake
        from hero_integration.services.runtime_plan import RuntimePlanSelection
        from hero_integration.services.read_contract import from_documents

        service, mapping, _ = self._saved_contract()
        contract = from_documents(service, mapping, frappe.get_doc("HERO GraphQL Schema Snapshot", service.schema_snapshot))
        scope = RuntimePlanSelection(frappe.local.site, "synthetic", service.name, service.service_revision,
            service.service_revision, mapping.name, mapping.mapping_revision, service.schema_snapshot,
            service.schema_fingerprint, service.operation_name, "AUTO_SYNC")
        row = dict(id=int(self.source_id), nr=self.token, full_name=self.token + "-customer",
                   company_name="", category="first", is_contact_person=False, is_deleted=False)
        with patch.object(migration, "get_configuration", return_value=self._configuration()):
            intake = InventoryIntake(scope, contract, 0, allow_new=True)
            try:
                self.assertIs(intake.reconcile(row, self.source_id, ()), True)
                new_origin = intake.origin.name
                self.assertIs(intake.reconcile(row, self.source_id, ()), True)
                self.assertEqual(intake.origin.name, new_origin)
                self.assertEqual(intake.origin.processed, 1)
                parent = frappe.db.get_value("HERO Object Binding", {"source_instance": "synthetic",
                    "external_object_id": self.source_id}, "name")
                missing = frappe.db.get_value("HERO Field Binding", {"object_binding": parent,
                    "target_field": "customer_details"}, "name")
                self.assertTrue(missing)
                frappe.db.delete("HERO Field Binding", {"name": missing})
                intake.allow_new = False
                self.assertIs(intake.reconcile(row, self.source_id, ()), True)
                self.assertTrue(frappe.db.exists("HERO Field Binding", missing))
                self.assertEqual(intake.origin.processed, 2)
                self.assertIs(intake.reconcile(dict(row, category="second"), self.source_id, ()), True)
                intake.finish()
            finally:
                if intake.origin:
                    self.created.setdefault("HERO Integration Run", []).append(intake.origin.name)
        self.assertEqual(intake.origin.reload().state, "Completed")
        self.assertEqual(frappe.db.count("HERO Object Binding", {"source_instance": "synthetic",
                         "external_object_id": self.source_id}), 1)
        self.assertEqual(frappe.db.get_value("Customer", {"customer_name": row["full_name"]},
                                            "customer_details"), "second")

    def test_migrated_customer_can_use_its_approved_saved_reader_for_bound_polling(self):
        from hero_integration.services.b8_runtime import ApprovedBoundReader, reconstruct_materialized_read_plan
        from hero_integration.services.runtime_plan import RuntimePlanSelection
        from hero_integration.services.sync_engine import FrappeBoundIndex, FrappeBoundProcessor
        from hero_integration.services.sync_runtime_adapter import _runtime_plan_service

        service, mapping, _ = self._saved_contract()
        run = self._run(service, mapping)
        row = dict.fromkeys("first_name last_name email phone_mobile phone_home address".split())
        row.update(id=int(self.source_id), nr=self.token, full_name=self.token + "-customer", company_name="",
                   category="initial", type="commercial", is_contact_person=False, is_deleted=False)
        self._execute(run, row)
        self.assertEqual(run.reload().state, "Completed", run.error_message)
        site = frappe.local.site
        selection = RuntimePlanSelection(site, "synthetic", service.name, service.service_revision,
            service.service_revision, mapping.name, mapping.mapping_revision, service.schema_snapshot,
            service.schema_fingerprint, service.operation_name, "AUTO_SYNC")
        plans = _runtime_plan_service(frappe)
        draft = plans.create_draft(selection, actor="Administrator")
        self.created.setdefault("HERO Runtime Plan", []).append(draft.plan_key)
        approved = plans.approve(draft.plan_key, expected_revision=draft.revision, actor="Administrator")
        materialized = reconstruct_materialized_read_plan(approved.selection)
        reader = ApprovedBoundReader(materialized, frappe.get_doc("HERO GraphQL Schema Snapshot",
            service.schema_snapshot).snapshot_json, 0, 10)
        with patch("hero_integration.client.settings.get_configuration", return_value=self._configuration()), \
                patch("hero_integration.client.graphql.HeroGraphQLClient.query",
                      return_value={"contacts": [dict(row, category="updated")]}) as transport:
            observed = reader.read_bound(self.source_id)
        self.assertIn("ids:", transport.call_args.args[0])
        contract = materialized.sealed.contract
        fields = FrappeBoundIndex(object_type="Contact", definition_ids=contract.definition_ids).fields(site, "synthetic")
        processor = FrappeBoundProcessor(mapping.mapping_revision, contract.definition_ids, run_id=run.name,
                                         object_type="Contact", mapping_name=mapping.name)
        self.assertTrue(processor.process_event(observed, fields, "snapshot-2", ""))
        self.assertEqual(frappe.db.get_value("Customer", self.token, "customer_details"), "updated")

    def test_unavailable_selected_definition_blocks_before_transport_or_target_write(self):
        service, mapping, _ignored = self._saved_contract(suffix="-invalid")
        run = self._run(service, mapping)
        selected = json.loads(mapping.mapping_json)["mapping_definition_ids"][0]
        frappe.db.set_value("HERO Frappe Mapping", {"definition_id": selected}, "status", "Disabled")
        with self.assertRaisesRegex(ReadContractError, "lineage cannot be verified"):
            self._execute(
                run,
                {"id": int(self.source_id), "full_name": self.token + "-blocked", "company_name": "", "is_deleted": False},
            )

        run.reload()
        self.assertEqual(run.state, "Failed")
        self.assertEqual(_Client.calls, [])
        self.assertFalse(frappe.db.exists("Customer", {"customer_name": self.token + "-blocked"}))
        self.assertFalse(
            frappe.db.exists(
                "HERO Integration Link",
                {"source_type": "customer", "source_id": self.source_id, "source_instance": "synthetic"},
            )
        )

    def test_mapping_revision_change_revokes_approved_run_before_transport_or_target_write(self):
        service, mapping, _ignored = self._saved_contract(suffix="-stale")
        run = self._run(service, mapping)
        mapping.mapping_json = mapping.mapping_json + " "
        mapping.save()

        with self.assertRaisesRegex(ReadContractError, "read mapping is not active"):
            self._execute(
                run,
                {"id": int(self.source_id), "full_name": self.token + "-stale", "company_name": "", "is_deleted": False},
            )

        run.reload()
        self.assertEqual(run.state, "Failed")
        self.assertEqual(_Client.calls, [])
        self.assertFalse(frappe.db.exists("Customer", {"customer_name": self.token + "-stale"}))


class _Client:
    calls = []
    record = {}

    def __init__(self, *args, **kwargs):
        pass

    def query(self, document, variables):
        self.__class__.calls.append((document, variables))
        return {"contacts": [dict(self.__class__.record)] if variables.get("offset") == 0 else []}
