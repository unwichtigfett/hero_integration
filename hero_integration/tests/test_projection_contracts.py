"""Offline contracts for the four-domain mapping extension."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from hero_integration.services.direct_mapping import MappingSpec, validate_spec
from hero_integration.services.mapping_runtime import plan_from_definitions
from hero_integration.services.projection_contracts import CONTRACTS, approved_query, validate_projection_record
from hero_integration.services.read_contract import from_documents
from hero_integration.sources.hero import GraphQLServiceSource


def definitions(operation):
    contract = CONTRACTS[operation]
    rows = [
        MappingSpec(operation, path, contract["target_doctype"], binding["field"], contract["paths"][path],
                    binding["purpose"], binding["transformation"], definition_id=f"{operation}:{binding['field']}")
        for path, binding in contract["bindings"].items()
    ]
    for field, binding in contract.get("native_bindings", {}).items():
        rows.append(MappingSpec(operation, binding["source_path"], contract["target_doctype"], field,
                                contract["paths"][binding["source_path"]], "Native Binding", "Native Adapter",
                                definition_id=f"{operation}:{field}"))
    if contract["project_path"]:
        rows.append(MappingSpec(operation, contract["project_path"], contract["target_doctype"], "custom_hero_project",
                                contract["paths"][contract["project_path"]], "Project Relation", "HERO Project Link",
                                "HERO Project Link", definition_id=f"{operation}:project"))
    return rows


def native_options(operation):
    if operation == "HeroCalendarEvents":
        return {"confirmed": True, "source_timezone": "Europe/Berlin",
                "target_timezone": "Europe/Berlin", "all_day_end": "inclusive"}
    return None


def documents(operation="HeroTasks"):
    contract = CONTRACTS[operation]
    snapshot = SimpleNamespace(name="S", schema_fingerprint=contract["fingerprint"])
    config = {"target_operation": operation, "records_path": contract["root"], "identity_path": "id",
              "mapping_definition_ids": [row.definition_id for row in definitions(operation)],
              "variables": {"first": "$page_size", "offset": "$offset"}, "page_size": 2}
    if native_options(operation):
        config["native_options"] = native_options(operation)
    mapping = SimpleNamespace(name="M", status="Active", mapping_revision=1, schema_snapshot="S",
                              schema_fingerprint=snapshot.schema_fingerprint, mapping_json=json.dumps(config))
    service = SimpleNamespace(name="R", status="Validated", execution_status="Approved", service_revision=1,
                              approved_service_revision=1, read_mapping="M", approved_mapping_revision=1,
                              schema_snapshot="S", schema_fingerprint=snapshot.schema_fingerprint,
                              query_document=approved_query(operation))
    return service, mapping, snapshot


def record(operation="HeroTasks"):
    result = {path: None for path in CONTRACTS[operation]["paths"]}
    for path, declared in CONTRACTS[operation]["paths"].items():
        if "|null" not in declared:
            result[path] = "Synthetic"
    result["id"] = 9
    if operation == "HeroCalendarEvents":
        result.update(title="Synthetic event", start="2026-06-01T10:00:00+02:00",
                      end="2026-06-01T11:00:00+02:00", all_day=False, is_recurring=False,
                      resources=[], partners=[])
    return result


class TestProjectionContracts(unittest.TestCase):
    def test_only_calendar_and_tasks_are_registered(self):
        self.assertEqual(set(CONTRACTS), {"HeroCalendarEvents", "HeroTasks"})

    def test_registered_bindings_match_target_metadata(self):
        for operation, contract in CONTRACTS.items():
            fields = {binding["field"]: SimpleNamespace(fieldtype=binding["fieldtype"],
                      options=binding.get("options", ""), read_only=0) for binding in contract["bindings"].values()}
            fields.update({field: SimpleNamespace(fieldtype=binding["fieldtype"],
                          options=binding.get("options", ""), read_only=0)
                          for field, binding in contract.get("native_bindings", {}).items()})
            fields["custom_hero_project"] = SimpleNamespace(fieldtype="Link", options="Project", read_only=0)
            metadata = SimpleNamespace(ensure_write=lambda doctype: None,
                                       field=lambda doctype, name: fields.get(name))
            for spec in definitions(operation):
                validate_spec(spec, metadata)

    def test_false_zero_and_null_stay_distinct(self):
        plan = plan_from_definitions("HeroTasks", definitions("HeroTasks"))
        values = plan.values(record())
        self.assertIsNone(values["custom_hero_author_user_id"])
        row = record()
        row.update(author_user_id=0, is_deleted=False)
        values = plan.values(row)
        self.assertEqual(values["custom_hero_author_user_id"], "0")
        self.assertEqual(values["custom_hero_is_deleted"], "false")

    def test_identity_and_source_shape_are_strict(self):
        plan = plan_from_definitions("HeroTasks", definitions("HeroTasks"))
        for value in (None, True, False, 0, -1, "1"):
            with self.assertRaises(ValueError):
                plan.source_identity({"id": value})
        row = record()
        del row["title"]
        with self.assertRaises(ValueError):
            validate_projection_record("HeroTasks", row)
        row = record()
        row["title"] = 12
        with self.assertRaises(ValueError):
            validate_projection_record("HeroTasks", row)

    def test_calendar_nested_relations_are_exact_and_deduplicated(self):
        row = record("HeroCalendarEvents")
        row["partners"] = [{"id": 1, "full_name": "A"}, {"id": 1, "full_name": "B"}]
        with self.assertRaises(ValueError):
            validate_projection_record("HeroCalendarEvents", row)

    def test_read_contract_requires_current_approval_and_exact_query(self):
        service, mapping, snapshot = documents()
        self.assertEqual(from_documents(service, mapping, snapshot).target_operation, "HeroTasks")
        service.query_document += " # changed"
        with self.assertRaises(ValueError):
            from_documents(service, mapping, snapshot)

    def test_calendar_requires_confirmed_timezone_configuration(self):
        service, mapping, snapshot = documents("HeroCalendarEvents")
        config = json.loads(mapping.mapping_json)
        config["native_options"]["confirmed"] = False
        mapping.mapping_json = json.dumps(config)
        with self.assertRaises(ValueError):
            from_documents(service, mapping, snapshot)

    def test_reader_is_bounded_and_uses_saved_variables(self):
        service, mapping, snapshot = documents()
        contract = from_documents(service, mapping, snapshot)
        calls = []
        client = SimpleNamespace(query=lambda query, variables: (calls.append((query, variables)) or {"tasks": [record()]}))
        page = GraphQLServiceSource(client, service, contract, "instance-a").projection(4)
        self.assertEqual(calls[0][1], {"first": 2, "offset": 4})
        self.assertIsNone(page.next_offset)
        self.assertEqual(page.records[0]["_hero_source_instance"], "instance-a")
