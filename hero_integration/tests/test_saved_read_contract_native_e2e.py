from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration.services import migration


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
        frappe.db.delete(
            "HERO Integration Link",
            {"source_type": "customer", "source_id": self.source_id, "source_instance": "synthetic"},
        )
        frappe.db.delete("Customer", {"customer_name": ["like", f"{self.token}%"]})
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
        operation = "synthetic_" + self.token.rsplit("-", 1)[-1] + suffix
        snapshot = self._insert(
            {
                "doctype": "HERO GraphQL Schema Snapshot",
                "source_instance": "synthetic",
                "snapshot_json": json.dumps(
                    {
                        "__schema": {
                            "queryType": {"name": "Query"},
                            "types": [
                                {
                                    "kind": "OBJECT",
                                    "name": "Query",
                                    "fields": [
                                        {
                                            "name": operation,
                                            "args": [],
                                            "type": {"kind": "LIST", "ofType": {"kind": "OBJECT", "name": "Contact"}},
                                        }
                                    ],
                                },
                                {"kind": "OBJECT", "name": "Contact", "fields": []},
                            ],
                        }
                    }
                ),
            }
        )
        definitions = [
            ("id", "int", "HERO Integration Link", "custom_hero_source_id", "Source Identity", "Positive Int To Text"),
            ("full_name", "string|null", "Customer", "customer_name", "Record Name", "Trim"),
            ("company_name", "string|null", "Customer", "customer_type", "Customer Type", "Customer Type From Company Name"),
            ("is_deleted", "boolean|null", "HERO Integration Link", "sync_status", "Lifecycle Flag", "Identity"),
        ]
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
                        "variables": {"first": "$page_size", "offset": "$offset"},
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
                "operation_name": "SyntheticContacts",
                "query_document": (
                    f"query SyntheticContacts($first: Int!, $offset: Int!) "
                    f"{{ {operation} {{ id full_name company_name is_deleted nr }} }}"
                ),
                "schema_snapshot": snapshot.name,
                "status": "Validated",
            }
        )
        service.read_mapping = mapping.name
        service.execution_status = "Approved"
        service.approved_service_revision = service.service_revision
        service.approved_mapping_revision = mapping.mapping_revision
        service.save()
        return service, mapping, ignored

    def _run(self, service, mapping):
        return self._insert(
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

    def _execute(self, run, record):
        _Client.calls = []
        _Client.record = record
        with (
            patch.object(
                migration,
                "get_configuration",
                return_value=SimpleNamespace(
                    endpoint=object(), api_token="synthetic", timeout_seconds=1, source_instance="synthetic"
                ),
            ),
            patch.object(migration, "HeroGraphQLClient", _Client),
        ):
            migration.execute_run(run.name)

    def test_saved_synthetic_service_persists_only_its_selected_customer_mapping(self):
        service, mapping, ignored = self._saved_contract()
        run = self._run(service, mapping)
        record = {
            "id": int(self.source_id),
            "nr": "MUST-NOT-PERSIST",
            "full_name": self.token + "-customer",
            "company_name": "",
            "is_deleted": False,
        }

        self._execute(run, record)

        run.reload()
        customer = frappe.get_doc("Customer", {"customer_name": record["full_name"]})
        link = frappe.get_doc(
            "HERO Integration Link",
            {"source_type": "customer", "source_id": self.source_id, "source_instance": "synthetic"},
        )
        self.assertEqual(run.state, "Completed")
        self.assertEqual((run.graphql_service, run.service_revision), (service.name, service.service_revision))
        self.assertEqual((run.graphql_mapping, run.mapping_revision), (mapping.name, mapping.mapping_revision))
        self.assertEqual(_Client.calls, [(service.query_document, {"first": 10, "offset": 0})])
        self.assertEqual(customer.customer_name, record["full_name"])
        self.assertNotEqual(customer.customer_name, record["nr"])
        self.assertEqual((link.target_doctype, link.target_name), ("Customer", customer.name))
        self.assertNotIn(ignored.definition_id, json.loads(mapping.mapping_json)["mapping_definition_ids"])

    def test_unavailable_selected_definition_blocks_before_transport_or_target_write(self):
        service, mapping, _ignored = self._saved_contract(definition_ids=["not-a-selected-definition"], suffix="-invalid")
        run = self._run(service, mapping)
        self._execute(
            run,
            {"id": int(self.source_id), "full_name": self.token + "-blocked", "company_name": "", "is_deleted": False},
        )

        run.reload()
        self.assertEqual(run.state, "Blocked")
        self.assertIn("selected active mapping definitions", run.error_message)
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

        self._execute(
            run,
            {"id": int(self.source_id), "full_name": self.token + "-stale", "company_name": "", "is_deleted": False},
        )

        run.reload()
        self.assertEqual(run.state, "Blocked")
        self.assertIn("selected read mapping is not active", run.error_message)
        self.assertEqual(_Client.calls, [])
        self.assertFalse(frappe.db.exists("Customer", {"customer_name": self.token + "-stale"}))


class _Client:
    calls = []
    record = {}

    def __init__(self, *args, **kwargs):
        pass

    def query(self, document, variables):
        self.__class__.calls.append((document, variables))
        root = document.split("{")[1].strip().split()[0]
        return {root: [dict(self.__class__.record)]}
