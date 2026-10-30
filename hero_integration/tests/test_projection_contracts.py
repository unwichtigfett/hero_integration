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
    snapshot = SimpleNamespace(name="S", schema_fingerprint=contract["fingerprint"], source_instance="instance-a")
    config = {"target_operation": operation, "records_path": contract["root"], "identity_path": "id",
              "mapping_definition_ids": [row.definition_id for row in definitions(operation)],
              "variables": {"first": "$page_size", "offset": "$offset"}, "page_size": 2}
    if native_options(operation):
        config["native_options"] = native_options(operation)
    # The read contract also binds direction, operation kind and source instance (see read_contract._common_contract).
    mapping = SimpleNamespace(name="M", status="Active", mapping_revision=1, schema_snapshot="S",
                              schema_fingerprint=snapshot.schema_fingerprint, mapping_json=json.dumps(config),
                              direction="QUERY_TO_FRAPPE", operation_kind="QUERY", source_instance="instance-a")
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
    def test_native_task_plan_requires_only_explicit_native_rules(self):
        identity = MappingSpec('HeroTasks', 'id', 'HERO Integration Link', 'source_id', 'int', 'Source Identity', 'Positive Int To Text', definition_id='native-id')
        title = MappingSpec('HeroTasks', 'title', 'Task', 'subject', 'string|null', 'Record Name', 'Trim', definition_id='native-title')
        comment = MappingSpec('HeroTasks', 'comment', 'Task', 'description', 'string|null', 'Native Binding', 'Native Adapter', definition_id='native-comment')
        project = MappingSpec('HeroTasks', 'target_project_match_id', 'Task', 'project', 'int|null', 'Native Binding', 'Native Adapter', definition_id='native-project')
        deadline = MappingSpec('HeroTasks', 'due_date', 'Task', 'custom_hero_due_date', 'string|null', 'Native Binding', 'Native Adapter', definition_id='native-deadline')
        assignment = MappingSpec('HeroTasks', 'target_user_id', 'ToDo', 'allocated_to', 'int|null', 'Native Binding', 'Native Adapter', definition_id='native-assignment')
        plan = plan_from_definitions('tasks', [identity, title], target_operation='HeroTasks', native_task=True)
        self.assertEqual(plan.source_identity({'id': 17}), '17')
        self.assertEqual(len(plan_from_definitions('tasks', [identity, title, comment], target_operation='HeroTasks', native_task=True).definitions), 3)
        self.assertEqual(len(plan_from_definitions('tasks', [identity, title, comment, project, deadline, assignment], target_operation='HeroTasks', native_task=True).definitions), 6)
        for rules in ([identity], [title], [identity, title, definitions('HeroTasks')[-1]]):
            with self.assertRaises(ValueError):
                plan_from_definitions('tasks', rules, target_operation='HeroTasks', native_task=True)

    def test_native_task_assignment_rule_is_optional_and_cannot_write_raw_id(self):
        identity = MappingSpec('HeroTasks', 'id', 'HERO Integration Link', 'source_id', 'int', 'Source Identity', 'Positive Int To Text', definition_id='native-id')
        title = MappingSpec('HeroTasks', 'title', 'Task', 'subject', 'string|null', 'Record Name', 'Trim', definition_id='native-title')
        assignment = MappingSpec('HeroTasks', 'target_user_id', 'ToDo', 'allocated_to', 'int|null', 'Native Binding', 'Native Adapter', definition_id='native-assignment')
        plan = plan_from_definitions('tasks', [identity, title, assignment], target_operation='HeroTasks', native_task=True)
        self.assertEqual({item.source_path for item in plan.definitions}, {'id', 'title', 'target_user_id'})
        unsafe = MappingSpec('HeroTasks', 'target_user_id', 'ToDo', 'allocated_to', 'int|null', 'Field', 'Identity', definition_id='unsafe')
        with self.assertRaises(ValueError):
            plan_from_definitions('tasks', [identity, title, unsafe], target_operation='HeroTasks', native_task=True)

    def test_task_onboarding_is_bound_to_reviewed_source_and_type(self):
        service, mapping, snapshot = documents()
        snapshot.source_instance = 'hero-one'
        mapping.source_instance = 'hero-one'
        snapshot.analysis_status = 'Validated'
        service.operation_name = mapping.operation_name = 'tasks'
        mapping.operation_kind = 'QUERY'
        mapping.direction = 'QUERY_TO_FRAPPE'
        config = json.loads(mapping.mapping_json)
        config['native_task_onboarding'] = {'confirmed': True, 'source_instance': 'hero-one', 'source_type': 'Task'}
        mapping.mapping_json = json.dumps(config)
        self.assertEqual(from_documents(service, mapping, snapshot).target_operation, 'HeroTasks')
        mapping.source_instance = 'hero-other'
        with self.assertRaises(ValueError):
            from_documents(service, mapping, snapshot)
        mapping.source_instance = 'hero-one'
        service.operation_name = 'HeroTasks'
        self.assertEqual(from_documents(service, mapping, snapshot).target_operation, 'HeroTasks')
        service.operation_name = 'unreviewed-name'
        with self.assertRaises(ValueError):
            from_documents(service, mapping, snapshot)
        service.operation_name = 'HeroTasks'
        mapping.operation_name = 'HeroTasks'
        with self.assertRaises(ValueError):
            from_documents(service, mapping, snapshot)
        mapping.operation_name = 'tasks'
        service.execution_status = 'Pending Approval'
        with self.assertRaises(ValueError):
            from_documents(service, mapping, snapshot)
        service.execution_status = 'Approved'
        mapping.mapping_revision = 2
        with self.assertRaises(ValueError):
            from_documents(service, mapping, snapshot)
        mapping.mapping_revision = 1
        snapshot.analysis_status = 'Draft'
        with self.assertRaises(ValueError):
            from_documents(service, mapping, snapshot)
        snapshot.analysis_status = 'Validated'
        config['native_task_onboarding']['source_instance'] = 'hero-two'
        mapping.mapping_json = json.dumps(config)
        with self.assertRaises(ValueError):
            from_documents(service, mapping, snapshot)
        config['native_task_onboarding']['source_instance'] = 'hero-one'
        config['native_task_onboarding']['source_type'] = 'Other'
        mapping.mapping_json = json.dumps(config)
        with self.assertRaises(ValueError):
            from_documents(service, mapping, snapshot)

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

    def test_calendar_optional_null_and_omission_do_not_clear_native_values(self):
        from hero_integration.services.native_projections import values as native_values
        plan = plan_from_definitions('HeroCalendarEvents', definitions('HeroCalendarEvents'))
        for absent in (False, True):
            row = record('HeroCalendarEvents')
            for field in ('project_match_id', 'description', 'full_address', 'partners', 'resources'):
                if absent:
                    row.pop(field)
                else:
                    row[field] = None
            validate_projection_record('HeroCalendarEvents', row)
            native = native_values(CONTRACTS['HeroCalendarEvents'], row, '9', 'fixture',
                                   native_options('HeroCalendarEvents'), lookup=lambda *args: None)
            values = plan.values(row, project_link=lambda value: self.fail('unexpected project resolution'),
                                 native_value=lambda field: native[field])
            for field in ('custom_hero_project', 'custom_hero_project_match_id', 'description', 'location',
                          'custom_hero_partners', 'custom_hero_resources'):
                self.assertNotIn(field, values)

    def test_calendar_project_uses_verified_resolver(self):
        from hero_integration.services.native_projections import values as native_values
        plan = plan_from_definitions('HeroCalendarEvents', definitions('HeroCalendarEvents'))
        row = record('HeroCalendarEvents'); row['project_match_id'] = 41
        native = native_values(CONTRACTS['HeroCalendarEvents'], row, '9', 'fixture',
                               native_options('HeroCalendarEvents'), lookup=lambda *args: None)
        values = plan.values(row, project_link=lambda source_id: 'PROJECT-VERIFIED' if source_id == 41 else None,
                             native_value=lambda field: native[field])
        self.assertEqual(values['custom_hero_project'], 'PROJECT-VERIFIED')

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
        source = GraphQLServiceSource(client, service, contract, "instance-a")
        page = source.projection(4)
        self.assertEqual(calls[0][1], {"first": 2, "offset": 4})
        # A short page may be a server page cap: the scan continues until an empty page.
        self.assertEqual(page.next_offset, 5)
        client.query = lambda query, variables: (calls.append((query, variables)) or {"tasks": []})
        self.assertIsNone(source.projection(5).next_offset)
        self.assertEqual(page.records[0]["_hero_source_instance"], "instance-a")
