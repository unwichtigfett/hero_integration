"""Direct CLI deployment: no drafts, complete replacement, stable runtime IDs."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import config_exchange as exchange
from hero_integration.services import direct_config_import as direct


class _Document(dict):
    def __init__(self, database, values):
        super().__init__(values)
        self.database = database

    def __getattr__(self, key):
        return self.get(key)

    def __setattr__(self, key, value):
        if key == "database":
            object.__setattr__(self, key, value)
        else:
            self[key] = value

    def insert(self):
        self.name = self.get("name") or f"{self.doctype}-{len(self.database.writes) + 1}"
        return self.save()

    def save(self):
        if not direct.accepts_document(self):
            raise AssertionError("Configuration write escaped its exact object scope.")
        self.database.rows[self.doctype, self.name] = deepcopy(dict(self))
        self.database.writes.append((self.doctype, self.name))
        return self

    def as_dict(self):
        return dict(self)


class _Site:
    def __init__(self):
        self.rows, self.writes, self.points = {}, [], {}
        self.current_reads = []
        self.session = SimpleNamespace(user="Administrator")
        self.local = SimpleNamespace(site="unit-test.invalid")
        self.db = SimpleNamespace(savepoint=self.savepoint, rollback=self.rollback, get_values=self.get_values)
        self.only_for = Mock()
        self.seed(direct.SNAPSHOT, "snapshot", source_instance="source-a")

    def seed(self, doctype, name, **values):
        self.rows[doctype, name] = {"doctype": doctype, "name": name, **values}
        return self.get_doc(doctype, name)

    def savepoint(self, name):
        self.points[name] = deepcopy(self.rows)

    def rollback(self, *, save_point):
        self.rows = self.points[save_point]

    def get_doc(self, doctype, name=None, **kwargs):
        values = doctype if isinstance(doctype, dict) else self.rows[doctype, name]
        return _Document(self, deepcopy(values))

    def get_all(self, doctype, *, filters=None, pluck=None, fields=None, **kwargs):
        return self._select(doctype, filters, pluck)

    def get_values(self, doctype, filters, fieldname, *, pluck, for_update, **kwargs):
        if not for_update:
            raise AssertionError("Expected a current locking read")
        self.current_reads.append(doctype)
        return self._select(doctype, filters, fieldname if pluck else None)

    def _select(self, doctype, filters, pluck):
        rows = []
        for (kind, _name), row in self.rows.items():
            if kind != doctype:
                continue
            selected = True
            for key, expected in (filters or {}).items():
                if isinstance(expected, list):
                    selected &= row.get(key) in expected[1]
                else:
                    selected &= row.get(key) == expected
            if selected:
                rows.append(_Document(self, deepcopy(row)))
        if pluck:
            return [getattr(row, pluck) for row in rows]
        return rows

    def active(self, doctype):
        return [row for (kind, _name), row in self.rows.items()
                if kind == doctype and row.get("status") in ("Active", "Validated")]


def _package():
    return {
        "format_version": exchange.FORMAT_VERSION, "replace_mappings": True, "settings": {},
        "schema_snapshot": {"schema_fingerprint": "fingerprint", "schema": {}},
        "mapping_package": {
            "schema": {"custom_doctypes": [], "custom_fields": [], "relations": []},
            "mappings": [{"definition_id": "incoming-rule", "operation": "HeroTasks",
                          "source_path": "name", "source_type": "string|null", "target_doctype": "Task",
                          "target_field": "subject", "purpose": "Field", "transformation": "Trim",
                          "relation": "", "priority": 5, "schema_dependencies": [], "mapping_revision": 1}],
        },
        "read_services": [{
            "target_operation": "HeroTasks", "service_name": "Tasks", "operation_name": "tasks",
            "mapping_operation_name": "tasks", "query": "query { tasks { id name } }",
            "mapping": {"target_operation": "HeroTasks", "records_path": "tasks", "identity_path": "id",
                        "mapping_definition_ids": ["incoming-rule"], "page_size": 100,
                        "query_adapter": {"version": 2, "read_service": "$read_service:HeroTasks",
                                          "transform_options": {"incoming-rule": {"target_type": "Data"}}}},
        }],
        "mutation_mappings": [{
            "definition_id": "incoming-mutation", "operation_kind": "MUTATION",
            "operation_name": "update_task", "direction": "FRAPPE_TO_MUTATION", "dependencies": [],
            "content": {"area": "HeroTasks", "usage": "MAPPED_UPDATE_V2", "read_service": "foreign-reader",
                        "assignments": [], "identity": {}},
        }],
        "mutation_contracts": [{
            "operation_name": "update_task", "service_name": "Update task",
            "query": "mutation Update($task: TaskInput) { update_task(task: $task) { id } }",
            "status": "Active", "mapping": {"areas": ["HeroTasks"], "mode": "VALIDATED_RESOLVER",
                "resolver": "MAPPED_UPDATE_V2", "mapping_definition_ids": ["incoming-mutation"]},
        }],
    }


class TestDirectConfigurationImport(unittest.TestCase):
    def setUp(self):
        self.site = _Site()
        self.snapshot = SimpleNamespace(name="snapshot", schema_fingerprint="fingerprint",
                                        source_instance="source-a", snapshot_json="{}")
        self.payload = _package()
        self.provision = Mock()
        self.verify = Mock()
        self.settings = Mock()
        self.lock = Mock()
        patches = [
            patch.object(direct, "frappe", self.site),
            patch.object(exchange, "frappe", self.site),
            patch.object(direct, "_prepare", side_effect=lambda _n, payload, _s: (
                {rule["definition_id"]: rule for rule in payload["mapping_package"]["mappings"]},
                self.snapshot, "selected", {"counts": {}})),
            patch("hero_integration.services.schema_provisioning.provision_schema", self.provision),
            patch("hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping.lock_exchange_context",
                  self.lock),
            patch("hero_integration.services.mapping_runtime.plan_from_definitions", return_value=SimpleNamespace()),
            patch("hero_integration.services.query_mapping.seal_query", return_value={"version": 2}),
            patch("hero_integration.services.personal_mapping_content.configured_mutation_helpers",
                  return_value=(lambda content, _schema: deepcopy(content), lambda *_a, **_kw: None)),
            patch.object(exchange, "_remap_mutation_rules", side_effect=AssertionError("Legacy rule remapping used.")),
            patch.object(exchange, "_verify_replacement_readers", self.verify),
            patch.object(exchange, "_settings_changes", return_value={}),
            patch.object(exchange, "_apply_settings", self.settings),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        # The importer must not construct or publish even temporary drafts.
        for path in (
            "hero_integration.services.frappe_mapping_draft_storage.FrappeMappingDraftStorage",
            "hero_integration.services.mapping_draft_exchange.save_import",
            "hero_integration.services.simple_mapping_exchange.publish_imported_mappings",
        ):
            guard = patch(path, side_effect=AssertionError("Obsolete personal draft workflow called."))
            guard.start()
            self.addCleanup(guard.stop)

    def apply(self, payload=None):
        return direct.apply("snapshot", payload or self.payload, "source-a")

    def test_first_import_creates_active_query_rules_readers_mutations_and_contracts_without_drafts(self):
        result = self.apply()
        self.assertTrue(result["activation"])
        self.assertFalse(result["execution"])
        self.assertEqual(len(self.site.active(direct.MAPPING)), 2)
        self.assertEqual(len(self.site.active(direct.RULE)), 1)
        self.assertEqual(len(self.site.active(direct.SERVICE)), 2)
        self.assertEqual(len(self.site.active(direct.CONTRACT)), 1)
        self.assertFalse(any("Draft" in doctype or "Revision" in doctype for doctype, _name in self.site.rows))
        query = next(row for row in self.site.active(direct.MAPPING) if row["direction"] == "QUERY_TO_FRAPPE")
        mutation = next(row for row in self.site.active(direct.MAPPING) if row["direction"] == "FRAPPE_TO_MUTATION")
        reader = next(row for row in self.site.active(direct.SERVICE) if row.get("read_mapping"))
        self.assertEqual(json.loads(mutation["mapping_json"])["read_service"], reader["name"])
        self.assertEqual(reader["approved_mapping_revision"], query["mapping_revision"])
        self.assertEqual(json.loads(self.site.active(direct.CONTRACT)[0]["mapping_json"])["mapping_definition_ids"],
                         [mutation["definition_id"]])

    def test_identical_import_is_idempotent_without_revisions_or_document_writes(self):
        self.apply()
        before, writes = deepcopy(self.site.rows), len(self.site.writes)
        self.apply()
        self.assertEqual(self.site.rows, before)
        self.assertEqual(len(self.site.writes), writes)

    def test_changed_fields_parameters_and_contracts_keep_identities_and_update_runtime_revisions(self):
        self.apply()
        before = deepcopy(self.site.rows)
        self.payload["read_services"][0]["mapping"]["page_size"] = 200
        self.payload["mapping_package"]["mappings"][0]["transformation"] = "Identity"
        self.payload["mutation_contracts"][0]["service_name"] = "Updated contract"
        self.apply()
        self.assertEqual(set(self.site.rows), set(before))
        query = next(row for row in self.site.active(direct.MAPPING) if row["direction"] == "QUERY_TO_FRAPPE")
        self.assertGreater(query["mapping_revision"], before[direct.MAPPING, query["name"]]["mapping_revision"])
        self.assertEqual(json.loads(query["mapping_json"])["page_size"], 200)
        self.assertEqual(self.site.active(direct.RULE)[0]["transformation"], "Identity")
        self.assertTrue(any(row["service_name"] == "Updated contract" for row in self.site.active(direct.SERVICE)))

    def test_omitted_mutations_and_contracts_are_disabled_but_not_deleted(self):
        self.apply()
        keys = set(self.site.rows)
        self.payload["mutation_mappings"], self.payload["mutation_contracts"] = [], []
        self.apply()
        self.assertEqual(set(self.site.rows), keys)
        self.assertFalse(any(row["direction"] == "FRAPPE_TO_MUTATION" for row in self.site.active(direct.MAPPING)))
        self.assertEqual(self.site.active(direct.CONTRACT), [])
        self.assertEqual(len(self.site.active(direct.SERVICE)), 1)

    def test_empty_complete_configuration_retires_all_mapping_components(self):
        self.apply()
        self.payload["read_services"] = []
        self.payload["mapping_package"]["mappings"] = []
        self.payload["mutation_mappings"], self.payload["mutation_contracts"] = [], []
        self.apply()
        for doctype in (direct.MAPPING, direct.RULE, direct.SERVICE, direct.CONTRACT):
            self.assertEqual(self.site.active(doctype), [], doctype)

    def test_failed_final_runtime_check_rolls_back_all_configuration_changes(self):
        self.apply()
        before = deepcopy(self.site.rows)
        self.payload["mapping_package"]["mappings"][0]["transformation"] = "Identity"
        self.verify.side_effect = ValueError("Invalid runtime contract")
        with self.assertRaisesRegex(ValueError, "runtime contract"):
            self.apply()
        self.assertEqual(self.site.rows, before)

    def test_failure_after_queries_were_written_rolls_back_partial_first_installation(self):
        before = deepcopy(self.site.rows)
        save = _Document.save
        def fail_contract(document):
            if document.doctype == direct.CONTRACT:
                raise ValueError("Contract persistence failed")
            return save(document)
        with patch.object(_Document, "save", fail_contract):
            with self.assertRaisesRegex(ValueError, "persistence failed"):
                self.apply()
        self.assertEqual(self.site.rows, before)
        self.assertIsNone(direct._document_write.get())

    def test_metadata_callback_failure_rolls_back_without_writing_configuration(self):
        before, writes = deepcopy(self.site.rows), len(self.site.writes)
        with self.assertRaisesRegex(ValueError, "changed"):
            direct.apply("snapshot", self.payload, "source-a",
                         before_mapping_write=Mock(side_effect=ValueError("preview changed")))
        self.assertEqual(self.site.rows, before)
        self.assertEqual(len(self.site.writes), writes)

    def test_another_source_business_records_and_open_drafts_are_not_modified(self):
        self.site.seed(direct.SNAPSHOT, "foreign-snapshot", source_instance="source-b")
        self.site.seed(direct.MAPPING, "foreign-mapping", source_instance="source-b", schema_snapshot="foreign-snapshot",
                       status="Active", direction="QUERY_TO_FRAPPE", mapping_json='{"mapping_definition_ids":[]}')
        self.site.seed("HERO Mapping Draft", "foreign-draft", state="OPEN", owner_user="other")
        self.site.seed("HERO Integration Link", "link", source_id="42", target_name="existing-task")
        self.site.seed("Task", "existing-task", subject="Local change")
        protected = {key: deepcopy(value) for key, value in self.site.rows.items()
                     if key[0] in ("HERO Mapping Draft", "HERO Integration Link", "Task") or key[1].startswith("foreign")}
        self.apply()
        for key, value in protected.items():
            self.assertEqual(self.site.rows[key], value)

    def test_snapshot_switch_keeps_mapping_and_service_names(self):
        self.apply()
        keys = set(self.site.rows)
        self.site.seed(direct.SNAPSHOT, "new-snapshot", source_instance="source-a")
        self.snapshot.name = "new-snapshot"
        self.apply()
        self.assertEqual(set(self.site.rows) - keys, {(direct.SNAPSHOT, "new-snapshot")})
        for doctype in (direct.MAPPING, direct.RULE, direct.SERVICE, direct.CONTRACT):
            for row in self.site.active(doctype):
                self.assertEqual(row["schema_snapshot"], "new-snapshot")

    def test_ordinary_user_is_rejected_before_ddl_or_document_lookup(self):
        self.site.session.user = "ordinary"
        self.site.only_for.side_effect = PermissionError("System Manager required")
        with self.assertRaises(PermissionError):
            self.apply()
        self.provision.assert_not_called()
        self.assertEqual(self.site.writes, [])

    def test_system_manager_can_import_without_last_editor_ownership(self):
        self.apply()
        self.site.session.user = "manager@example.invalid"
        self.payload["read_services"][0]["mapping"]["page_size"] = 250
        self.apply()
        self.site.only_for.assert_called_with("System Manager")

    def test_shared_rules_still_selected_by_another_source_are_not_disabled(self):
        self.apply()
        rule = self.site.active(direct.RULE)[0]
        self.site.seed(direct.SNAPSHOT, "foreign-snapshot", source_instance="source-b")
        self.site.seed(direct.MAPPING, "foreign-mapping", source_instance="source-b", schema_snapshot="foreign-snapshot",
                       status="Active", direction="QUERY_TO_FRAPPE",
                       mapping_json=json.dumps({"mapping_definition_ids": [rule["definition_id"]]}))
        self.payload["read_services"] = []
        self.payload["mapping_package"]["mappings"] = []
        self.payload["mutation_mappings"], self.payload["mutation_contracts"] = [], []
        self.apply()
        self.assertEqual(self.site.rows[direct.RULE, rule["name"]]["status"], "Active")

    def test_import_scope_cannot_be_reused_for_another_document_or_user(self):
        doc = self.site.get_doc({"doctype": direct.MAPPING})
        other = self.site.get_doc({"doctype": direct.MAPPING})
        with direct._writing(doc):
            self.assertTrue(direct.accepts_document(doc))
            self.assertFalse(direct.accepts_document(other))
            self.site.session.user = "other"
            self.assertFalse(direct.accepts_document(doc))
        self.assertFalse(direct.accepts_document(doc))

    def test_service_approvals_do_not_increment_query_document_revision(self):
        doc = direct._put(direct.SERVICE, {"query_document": "query { id }", "status": "Validated"})
        approved = direct._put(direct.SERVICE, {"execution_status": "Approved"}, doc)
        self.assertEqual(approved.service_revision, 1)

    def test_preview_is_nonwriting_and_does_not_require_existing_contract_identity(self):
        result = direct.preview("snapshot", self.payload, "source-a")
        self.assertTrue(result["replace_mappings"])
        self.assertFalse(result["execution"])
        self.assertEqual(result["read_services"], [{"target_operation": "HeroTasks", "state": "Replace"}])
        self.assertEqual(self.site.writes, [])
        self.provision.assert_not_called()

    def test_mutation_export_reads_active_configuration_not_personal_drafts(self):
        self.apply()
        result = direct.export_mutations("source-a", "snapshot")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["operation_name"], "update_task")
        self.assertNotIn("base_active_revision", result[0])
        contracts = direct.export_contracts("source-a", "snapshot", mutation_mappings=result)
        self.assertEqual(len(contracts), 1)
        self.assertEqual(result[0]["definition_id"], "incoming-mutation")
        self.assertEqual(contracts[0]["mapping"]["mapping_definition_ids"], ["incoming-mutation"])

    def test_export_does_not_include_legacy_arbitrary_mutation_profiles(self):
        self.apply()
        self.site.seed(direct.MAPPING, "legacy", source_instance="source-a", schema_snapshot="snapshot",
                       direction="FRAPPE_TO_MUTATION", status="Active", mapping_json='{"usage":"SEND_TO_HERO"}')
        self.assertEqual(len(direct.export_mutations("source-a", "snapshot")), 1)

    def test_explicit_query_rule_references_roundtrip_with_original_cli_identity(self):
        self.payload["mutation_mappings"][0]["content"]["assignments"] = [{"query_rule_id": "incoming-rule"}]
        self.apply()
        mutation = next(row for row in self.site.active(direct.MAPPING) if row["direction"] == "FRAPPE_TO_MUTATION")
        rule = self.site.active(direct.RULE)[0]
        content = json.loads(mutation["mapping_json"])
        self.assertEqual(content["assignments"][0]["query_rule_id"], rule["definition_id"])
        exported = direct.export_mutations("source-a", "snapshot")[0]
        self.assertEqual(exported["content"]["assignments"][0]["query_rule_id"], "incoming-rule")

    def test_preview_of_identical_configuration_reports_unchanged_without_new_rules(self):
        self.apply()
        result = direct.preview("snapshot", self.payload, "source-a")
        self.assertEqual(result["adopt"], 0)
        self.assertEqual(result["read_services"][0]["state"], "Unchanged")

    def test_apply_inventory_uses_current_reads_after_acquiring_the_source_lock(self):
        with patch.object(self.site, "get_all", side_effect=AssertionError("Stale consistent inventory read.")):
            self.apply()
        self.assertIn(direct.MAPPING, self.site.current_reads)
        self.assertIn(direct.SNAPSHOT, self.site.current_reads)


class TestDirectConfigurationRouting(unittest.TestCase):
    def test_parse_preserves_contract_comments_and_formatting_for_the_unchanged_cli(self):
        from hero_integration.tests.test_config_mutation_drafts import package
        from hero_integration.tests.test_config_mutation_contracts import fixtures

        payload = package()
        contract, mutation = fixtures()
        contract["query"] = "# Local mapping definition\n" + contract["query"] + "\n"
        payload["mutation_mappings"] = [mutation]
        payload["mutation_contracts"] = [contract]
        parsed = exchange._parse(json.dumps(payload))
        self.assertEqual(parsed["mutation_contracts"], payload["mutation_contracts"])

    def test_existing_import_endpoint_routes_replace_mode_to_direct_import(self):
        payload = _package()
        with patch.object(exchange, "frappe", SimpleNamespace(only_for=Mock())), \
                patch.object(exchange, "_parse", return_value=payload), \
                patch.object(direct, "apply", return_value={"activation": True}) as apply:
            result = exchange.import_config("snapshot", "json", "source")
        self.assertTrue(result["activation"])
        apply.assert_called_once_with("snapshot", payload, "source", before_mapping_write=None)

    def test_validator_can_plan_contract_replacement_without_inspecting_the_old_contract(self):
        from hero_integration.services import config_mutation_contracts
        from hero_integration.tests.test_config_mutation_contracts import fixtures, SCHEMA

        item, group = fixtures()
        payload = {"mapping_package": {"schema": {"custom_doctypes": [], "custom_fields": [], "relations": []}},
                   "read_services": [], "mutation_mappings": [group], "mutation_contracts": [item]}
        with patch("hero_integration.services.schema_provisioning.preview_schema",
                   return_value={"items": [], "counts": {}}), \
                patch("hero_integration.services.schema_provisioning._definitions", return_value={}), \
                patch("hero_integration.services.schema_provisioning._PlannedMetadata"), \
                patch("hero_integration.services.mapped_update_v2.validate_sources"), \
                patch.object(config_mutation_contracts, "preview", side_effect=AssertionError("Old-state conflict check")):
            exchange._replacement_check(SimpleNamespace(name="existing", snapshot_json=json.dumps(SCHEMA)),
                                         payload, {}, replace_existing=True)


class TestConfigurationMetadataPermissionScope(unittest.TestCase):
    def setUp(self):
        self.frappe = SimpleNamespace(
            local=SimpleNamespace(site="test.invalid"),
            session=SimpleNamespace(user="manager@example.invalid"),
            only_for=Mock(),
            get_meta=lambda _doctype: SimpleNamespace(istable=False),
            has_permission=Mock(side_effect=PermissionError("No business-data write permission")),
        )

    def test_manager_can_compile_metadata_without_receiving_business_write_permission(self):
        from hero_integration.services.doctype_permissions import configuration_metadata_review, ensure_doctype_write
        with configuration_metadata_review(self.frappe):
            ensure_doctype_write("Employee", self.frappe)
        self.frappe.only_for.assert_called_once_with("System Manager")
        self.frappe.has_permission.assert_not_called()
        with self.assertRaises(PermissionError):
            ensure_doctype_write("Employee", self.frappe)

    def test_scope_is_reset_on_failure(self):
        from hero_integration.services.doctype_permissions import configuration_metadata_review, ensure_doctype_write
        with self.assertRaises(ValueError):
            with configuration_metadata_review(self.frappe):
                raise ValueError("Compilation failed")
        with self.assertRaises(PermissionError):
            ensure_doctype_write("Employee", self.frappe)

    def test_scope_does_not_follow_a_changed_actor_or_site(self):
        from hero_integration.services.doctype_permissions import configuration_metadata_review, ensure_doctype_write
        for key in ("user", "site"):
            with self.subTest(key=key), configuration_metadata_review(self.frappe):
                target = self.frappe.session if key == "user" else self.frappe.local
                old = getattr(target, key)
                setattr(target, key, "other")
                with self.assertRaises(PermissionError):
                    ensure_doctype_write("Employee", self.frappe)
                setattr(target, key, old)

    def test_non_manager_cannot_enter_configuration_review_scope(self):
        from hero_integration.services.doctype_permissions import configuration_metadata_review, ensure_doctype_write
        self.frappe.only_for.side_effect = PermissionError("System Manager required")
        with self.assertRaises(PermissionError):
            with configuration_metadata_review(self.frappe):
                self.fail("Unauthorized configuration review")
        with self.assertRaises(PermissionError):
            ensure_doctype_write("Employee", self.frappe)
