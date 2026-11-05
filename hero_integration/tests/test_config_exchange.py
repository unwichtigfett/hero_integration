"""Complete configuration export/import: mapping package plus read services."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration import api
from hero_integration.services import config_exchange as cx


def entry(operation, **mapping):
    return {"target_operation": operation, "service_name": operation, "operation_name": operation.lower(),
            "mapping_operation_name": operation.lower(), "query": f"query {operation} {{ x }}", "mapping": mapping}


SNAPSHOT = {"schema_fingerprint": "SOURCE", "schema": {"__schema": {"types": []}}}


def payload(*services, settings=None, version=None):
    if version == cx.FORMAT_V1:
        return json.dumps({"format_version": version, "mapping_package": {}, "read_services": list(services)})
    return json.dumps({"format_version": cx.FORMAT_VERSION, "schema_snapshot": SNAPSHOT,
                       "settings": settings if settings is not None else {"polling_mode": "AUTO_SYNC"},
                       "mapping_package": {}, "read_services": list(services)})


class TestConfigParse(unittest.TestCase):
    def test_parameterized_error_keeps_english_exception_and_translates_at_boundary(self):
        message = "{0} needs the {1} read service first."
        error = cx.ConfigExchangeError(message, "HeroProjectMatches", "HeroCustomers")
        self.assertEqual(str(error), "HeroProjectMatches needs the HeroCustomers read service first.")
        translated = error.translated(lambda key: "{0} benötigt zuerst den Lese-Service {1}." if key == message else key)
        self.assertEqual(translated, "HeroProjectMatches benötigt zuerst den Lese-Service HeroCustomers.")
        # Literal braces in a complete message must not be treated as placeholders.
        self.assertEqual(cx.ConfigExchangeError("Invalid {example}").translated(lambda key: key), "Invalid {example}")

    def test_preview_translates_service_error_at_the_api_boundary(self):
        source = "The configuration file is not valid JSON."
        translated = "Die Konfigurationsdatei enthält kein gültiges JSON."
        def fail(message):
            raise ValueError(message)
        fake = SimpleNamespace(only_for=lambda _role: None, _=lambda message: translated if message == source else message,
                               throw=fail)
        with patch.object(api, "frappe", fake), patch.object(api, "_configured_source_instance", return_value="default"), \
             patch.object(cx, "preview_config", side_effect=cx.ConfigExchangeError(source)):
            with self.assertRaisesRegex(ValueError, "kein gültiges JSON"):
                api.config_import_preview("{")

    def test_only_the_complete_format_is_accepted(self):
        cx._parse(payload(entry("HeroCustomers")))
        for bad in ("{", json.dumps({"format_version": "other", "mapping_package": {}, "read_services": []}),
                    payload(entry("HeroCustomers"), entry("HeroCustomers")),
                    payload({**entry("HeroTasks"), "api_token": "x"}), payload(entry("Unknown"))):
            with self.assertRaises(cx.ConfigExchangeError):
                cx._parse(bad)


class TestPackageContent(unittest.TestCase):
    def setUp(self):
        binding = patch.object(cx, "_snapshot_binding", return_value={"source_instance":"default", "endpoint_fingerprint":"local-endpoint"})
        binding.start(); self.addCleanup(binding.stop)

    def test_version_1_files_still_import(self):
        self.assertEqual(cx._parse(payload(entry("HeroCustomers"), version=cx.FORMAT_V1))["format_version"], cx.FORMAT_V1)

    def test_credentials_and_safety_switches_are_never_accepted(self):
        for key in ("hero_api_token", "hero_api_url", "allowed_hero_hosts", "admin_notification_recipients",
                    "allow_graphql_mutation_execution", "allow_deletion"):
            self.assertNotIn(key, cx.SETTINGS_KEYS)
            with self.assertRaises(cx.ConfigExchangeError):
                cx._parse(payload(entry("HeroCustomers"), settings={key: "x"}))

    def test_the_files_snapshot_is_reused_or_created(self):
        existing = SimpleNamespace(get_all=lambda *a, **k: ["SNAP-1"], session=SimpleNamespace(user="Administrator"))
        with patch.object(cx, "frappe", existing):
            self.assertEqual(cx._package_snapshot({"schema_snapshot": SNAPSHOT}, create=True), ("SNAP-1", "Unchanged"))
        missing = SimpleNamespace(get_all=lambda *a, **k: [], session=SimpleNamespace(user="Administrator"))
        with patch.object(cx, "frappe", missing):
            self.assertEqual(cx._package_snapshot({"schema_snapshot": SNAPSHOT}, create=False), (None, "New"))
            with patch("hero_integration.services.schema_refresh.capture_from_schema",
                       return_value={"snapshot_name": "SNAP-2"}) as capture:
                self.assertEqual(cx._package_snapshot({"schema_snapshot": SNAPSHOT}, create=True), ("SNAP-2", "Created"))
            capture.assert_called_once_with(SNAPSHOT["schema"], "Administrator")


class TestServiceReferences(unittest.TestCase):
    def test_a_customer_service_reference_resolves_to_the_target_sites_service(self):
        item = entry("HeroProjectMatches", customer_service=cx.SERVICE_REFERENCE + "HeroCustomers", page_size=100)
        with patch.object(api, "_sync_area_read_services", return_value={"HeroCustomers": [{"service": "SVC-9"}]}):
            self.assertEqual(cx._resolved_mapping(item, "default")["customer_service"], "SVC-9")
        self.assertEqual(cx._resolved_mapping(item, "default", {"HeroCustomers": "NEW-1"})["customer_service"], "NEW-1")
        with patch.object(api, "_sync_area_read_services", return_value={}):
            with self.assertRaises(cx.ConfigExchangeError):
                cx._resolved_mapping(item, "default")


class TestSchemaRebase(unittest.TestCase):
    def test_the_package_is_bound_to_the_target_snapshot(self):
        fake = SimpleNamespace(db=SimpleNamespace(get_value=lambda *a: "TARGET"))
        payload = {"mapping_package": {"schema_fingerprint": "SOURCE", "mappings": []}}
        with patch.object(cx, "frappe", fake):
            package, source = cx._rebased_package("SNAP", payload)
        self.assertEqual((json.loads(package)["schema_fingerprint"], source), ("TARGET", "SOURCE"))

    def test_a_query_that_does_not_fit_the_target_schema_blocks_the_import(self):
        package = {"items": [], "preview_revision": "rev"}
        fake = SimpleNamespace(only_for=lambda role: None, db=SimpleNamespace(get_value=lambda *a: "TARGET"))
        with patch.object(cx, "frappe", fake), \
                patch.object(cx, "_resolve_snapshot", return_value=("SNAP", "selected")), \
                patch("hero_integration.services.schema_provisioning.preview_package", return_value=package), \
                patch.object(api, "_workspace_service_validate", side_effect=ValueError("Field x does not exist")), \
                patch.object(cx, "_import_service") as created:
            with self.assertRaises(cx.ConfigExchangeError):
                cx.import_config("SNAP", payload(entry("HeroCustomers")))
        created.assert_not_called()


class TestImportOrder(unittest.TestCase):
    def test_customers_are_imported_before_projects_and_unchanged_services_are_kept(self):
        calls = []
        states = {"HeroCustomers": "New", "HeroProjectMatches": "New", "HeroTasks": "Unchanged"}
        package = {"items": [{"id": "rule-1", "state": "New"}, {"id": "field-1", "state": "Unchanged"}],
                   "preview_revision": "rev"}
        fake = SimpleNamespace(only_for=lambda role: None, db=SimpleNamespace(get_value=lambda *a: "TARGET"))
        with patch.object(cx, "frappe", fake), patch.object(cx, "_query_problem", return_value=None), \
                patch.object(cx, "_resolve_snapshot", return_value=("SNAP", "Unchanged")), \
                patch.object(cx, "_settings_changes", return_value={}), \
                patch("hero_integration.services.schema_provisioning.preview_package", return_value=package), \
                patch("hero_integration.services.adoption.start",
                      side_effect=lambda *args: calls.append(("adopt", args[2])) or {"adoption_name": "AD-1"}), \
                patch.object(cx, "_service_state", side_effect=lambda item, *a: states[item["target_operation"]]), \
                patch.object(cx, "_import_service", side_effect=lambda s, item, i, created: calls.append(
                    ("service", item["target_operation"], dict(created))) or "SVC-" + item["target_operation"]):
            result = cx.import_config("SNAP", payload(entry("HeroTasks"), entry("HeroProjectMatches"), entry("HeroCustomers")))
        self.assertEqual(calls, [("adopt", ["rule-1"]), ("service", "HeroCustomers", {}),
                                 ("service", "HeroProjectMatches", {"HeroCustomers": "SVC-HeroCustomers"})])
        self.assertEqual([row["state"] for row in result["read_services"]], ["Approved", "Approved", "Unchanged"])

    def test_conflicting_packages_import_nothing(self):
        package = {"items": [{"id": "rule-1", "state": "Conflict"}], "preview_revision": "rev"}
        with patch.object(cx, "frappe", SimpleNamespace(only_for=lambda role: None,
                                                       db=SimpleNamespace(get_value=lambda *a: "TARGET"))), \
                patch.object(cx, "_resolve_snapshot", return_value=("SNAP", "Unchanged")), \
                patch("hero_integration.services.schema_provisioning.preview_package", return_value=package):
            with self.assertRaises(cx.ConfigExchangeError):
                cx.import_config("SNAP", payload(entry("HeroCustomers")))


if __name__ == "__main__":
    unittest.main()


class TestExplicitReplacement(unittest.TestCase):
    def replacement(self, **changes):
        value = json.loads(payload(entry("HeroEmployees", target_operation="HeroEmployees", mapping_definition_ids=["R"])))
        value["replace_mappings"] = True
        value["mapping_package"] = {"format_version": "hero-frappe-mapping-package-v2", "schema_fingerprint": "SOURCE",
            "schema": {"custom_doctypes": [], "custom_fields": [], "relations": []}, "mappings": [{"definition_id": "R", "operation": "HeroEmployees",
            "source_path": "first_name", "source_type": "string|null", "target_doctype": "Employee",
            "target_field": "first_name", "purpose": "Record Name", "transformation": "Trim", "relation": "", "priority": 5,
            "schema_dependencies": [], "mapping_revision": 1}]}
        value.update(changes)
        return value

    def test_replace_flag_is_optional_boolean_only(self):
        for flag in (True, False):
            self.assertEqual(cx._parse(json.dumps(self.replacement(replace_mappings=flag)))["replace_mappings"], flag)
        for flag in (1, "true", None):
            with self.assertRaises(cx.ConfigExchangeError):
                cx._parse(json.dumps(self.replacement(replace_mappings=flag)))
        legacy = json.loads(payload(version=cx.FORMAT_V1))
        legacy["replace_mappings"] = True
        with self.assertRaises(cx.ConfigExchangeError):
            cx._parse(json.dumps(legacy))

    def test_replacement_requires_exact_selection_without_orphans_or_cross_area_rules(self):
        valid = self.replacement()
        self.assertEqual(set(cx._replacement_rules(valid)), {"R"})
        for mutation in (lambda p: p["mapping_package"]["mappings"].append(dict(p["mapping_package"]["mappings"][0])),
                         lambda p: p["read_services"][0]["mapping"].update(mapping_definition_ids=["missing"]),
                         lambda p: p["mapping_package"]["mappings"][0].update(operation="HeroProducts"),
                         lambda p: p["mapping_package"]["mappings"].append({**p["mapping_package"]["mappings"][0], "definition_id": "extra"})):
            value = self.replacement()
            mutation(value)
            with self.assertRaises(cx.ConfigExchangeError):
                cx._replacement_rules(value)

    def test_nested_reader_reference_is_remapped_without_mutating_input(self):
        item = entry("HeroProducts", query_adapter={"version": 2, "read_service": cx.SERVICE_REFERENCE + "HeroProducts"})
        resolved = cx._resolved_mapping(item, "source-a", {"HeroProducts": "TARGET-READER"})
        self.assertEqual(resolved["query_adapter"]["read_service"], "TARGET-READER")
        self.assertEqual(item["mapping"]["query_adapter"]["read_service"], cx.SERVICE_REFERENCE + "HeroProducts")

    def test_foreign_snapshot_and_source_change_are_rejected(self):
        foreign = SimpleNamespace(source_instance="source-b", analysis_status="Validated")
        with patch.object(cx, "frappe", SimpleNamespace(get_doc=lambda *a: foreign)):
            with self.assertRaisesRegex(cx.ConfigExchangeError, "another"):
                cx._source_snapshot("snapshot", self.replacement(), "source-a")
            with self.assertRaisesRegex(cx.ConfigExchangeError, "another"):
                cx._source_snapshot("snapshot", self.replacement(settings={"hero_source_instance": "source-b"}), "source-a")

    def test_missing_native_task_authority_blocks_before_schema_or_reader_writes(self):
        item = entry("HeroTasks", target_operation="HeroTasks", native_task_onboarding={"confirmed": True})
        snapshot = SimpleNamespace(name="snapshot")
        with self.assertRaisesRegex(cx.ConfigExchangeError, "source-bound parent"):
            cx._check_native_parents(snapshot, {"read_services": [item]}, [])

    def test_native_extra_fields_and_immutable_reader_changes_fail_before_writes(self):
        item = entry("HeroCustomers", target_operation="HeroCustomers", mapping_definition_ids=["R"],
                     records_path="contacts", identity_path="id", variables={}, page_size=100, customer_contract_version="v4")
        parent = _Record(name="parent", schema_snapshot="snapshot", mapping_json=json.dumps(item["mapping"]))
        snapshot = _Record(name="snapshot")
        cx._check_native_parents(snapshot, {"read_services": [item]}, [parent])
        item["mapping"]["unimplemented_feature"] = True
        with self.assertRaisesRegex(cx.ConfigExchangeError, "unsupported configuration"):
            cx._check_native_parents(snapshot, {"read_services": [item]}, [parent])
        item["mapping"].pop("unimplemented_feature")
        item["mapping"]["page_size"] = 200
        with self.assertRaisesRegex(cx.ConfigExchangeError, "reader contract"):
            cx._check_native_parents(snapshot, {"read_services": [item]}, [parent])

    def test_rule_comparison_ignores_ids_and_seals_but_retains_transform_options(self):
        value = self.replacement()
        rules = cx._replacement_rules(value)
        config = {**value["read_services"][0]["mapping"], "query_adapter_seal": {"old": 1},
                  "query_adapter": {"version": 2, "read_service": "old-reader", "transform_options": {"R": {"default": "A"}}}}
        other = {**config, "mapping_definition_ids": ["new-id"], "query_adapter_seal": {"new": 1},
                 "query_adapter": {"version": 2, "read_service": "new-reader", "transform_options": {"new-id": {"default": "A"}}}}
        self.assertEqual(cx._semantic_mapping(config, rules), cx._semantic_mapping(other, {"new-id": rules["R"]}))
        other["query_adapter"]["transform_options"]["new-id"]["default"] = "B"
        self.assertNotEqual(cx._semantic_mapping(config, rules), cx._semantic_mapping(other, {"new-id": rules["R"]}))
        self.assertEqual(config["query_adapter"]["read_service"], "old-reader")


class _Record(dict):
    __getattr__ = dict.get


class TestReplacementRetirement(unittest.TestCase):
    def test_retirement_preserves_foreign_references_and_clears_old_reader_approval(self):
        changes, locks = [], []
        old = _Record(name="old-map", status="Active", source_instance="source-a", mapping_revision=4)
        old_rule = _Record(name="old-rule", status="Active", schema_snapshot="snap-a", operation="HeroProducts",
                          definition_id="old-id", mapping_revision=3, target_doctype="Item", target_field="description")
        shared = _Record(name="shared-rule", status="Active", schema_snapshot="snap-a", operation="HeroProducts",
                        definition_id="shared-id", mapping_revision=2, target_doctype="Item", target_field="item_name")
        docs = {"old-map": old, "old-rule": old_rule, "shared-rule": shared, "old-reader": _Record(name="old-reader")}
        def get_all(doctype, **kwargs):
            if doctype == "HERO GraphQL Schema Snapshot":
                self.assertEqual(kwargs["filters"], {"source_instance": "source-a"})
                return ["snap-a"]
            if doctype == "HERO Frappe Mapping":
                self.assertEqual(kwargs["filters"]["schema_snapshot"], ["in", ["snap-a"]])
                return [old_rule, shared]
            if doctype == "HERO GraphQL Service":
                return ["old-reader", "new-reader"]
            self.fail(doctype)
        active = [_Record(name="old-map", mapping_json=json.dumps({"mapping_definition_ids": ["old-id", "shared-id"]})),
                  _Record(name="new-map", mapping_json=json.dumps({"mapping_definition_ids": ["new-id"]})),
                  _Record(name="foreign-map", mapping_json=json.dumps({"mapping_definition_ids": ["shared-id"]}))]
        fake = SimpleNamespace(get_all=get_all, get_doc=lambda doctype, name, **kw: docs[name],
            db=SimpleNamespace(sql=lambda *a, **kw: active, set_value=lambda *args: changes.append(args)))
        with patch.object(cx, "frappe", fake), patch.object(cx, "_source_mappings", return_value=[old]), \
                patch("hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping._lock_active_target",
                      side_effect=lambda *args: locks.append(args)):
            result = cx._retire_replaced("source-a", ["HeroProducts"], {"new-map"}, {"new-reader"})
        self.assertEqual(result["field_mappings"], 1)
        self.assertEqual(result["graphql_mappings"], 1)
        self.assertEqual([row[1] for row in changes], ["old-rule", "old-map", "old-reader"])
        self.assertEqual(changes[0][2], {"status": "Disabled", "mapping_revision": 4})
        self.assertEqual(changes[2][2]["execution_status"], "Pending Approval")
        self.assertEqual(len(locks), 2)

    def test_only_current_actor_source_drafts_are_archived(self):
        from unittest.mock import Mock
        docs = {"own-a": _Record(definition_id="id-a", generation=2, content_revision=3, base_active_mapping="",
                    content_json=json.dumps({"target_operation": "HeroProducts"}),
                    new_definition_json=json.dumps({"source_instance": "source-a", "direction": "QUERY_TO_FRAPPE"})),
                "own-b": _Record(definition_id="id-b", generation=1, content_revision=1, base_active_mapping="",
                    content_json=json.dumps({"target_operation": "HeroProducts"}),
                    new_definition_json=json.dumps({"source_instance": "source-b", "direction": "QUERY_TO_FRAPPE"}))}
        def get_all(doctype, **kwargs):
            self.assertEqual(kwargs["filters"]["owner_user"], "actor")
            self.assertEqual(kwargs["filters"]["state"], "OPEN")
            return list(docs)
        discard = Mock()
        fake = SimpleNamespace(session=SimpleNamespace(user="actor"), local=SimpleNamespace(site="site"),
                               get_all=get_all, get_doc=lambda doctype, name, **kw: docs[name])
        storage = SimpleNamespace(service=lambda: SimpleNamespace(discard=discard))
        with patch.object(cx, "frappe", fake), \
                patch("hero_integration.services.frappe_mapping_draft_storage.FrappeMappingDraftStorage", return_value=storage):
            cx._archive_replaced_drafts("source-a", ["HeroProducts"])
        discard.assert_called_once_with("id-a", "actor", 2, 3, confirmed=True)


class TestScopedConfigurationExport(unittest.TestCase):
    def test_export_carries_selected_scoped_rules_and_mapping_schema_provenance(self):
        fixture = TestExplicitReplacement().replacement()
        rule = _Record(fixture["mapping_package"]["mappings"][0])
        rule.update(schema_dependencies='{"ids":[]}', personal_graphql_mapping="mapped", schema_manifest="")
        mapping = _Record(name="mapped", schema_fingerprint="SOURCE", operation_name="partners",
            portable_schema_manifest='{"custom_doctypes":[],"custom_fields":[],"relations":[]}',
            mapping_json=json.dumps({"target_operation": "HeroEmployees", "mapping_definition_ids": ["R"],
                "query_adapter": {"version": 2, "read_service": "reader"}, "query_adapter_seal": {"old": True}}))
        snapshot = _Record(name="snapshot", source_instance="source-a", schema_fingerprint="SOURCE", snapshot_json=json.dumps(SNAPSHOT["schema"]))
        reader = _Record(name="reader", service_name="Employees", operation_name="partners", query_document="query { partners { id } }")
        docs = {"mapped": mapping, "snapshot": snapshot, "reader": reader}
        requested = []
        def get_all(doctype, **kwargs):
            requested.append(kwargs["filters"])
            return [rule]
        fake = SimpleNamespace(get_doc=lambda doctype, name: docs[name], get_all=get_all,
                               get_single=lambda *a: {key: ("source-a" if key == "hero_source_instance" else None) for key in cx.SETTINGS_KEYS})
        with patch('hero_integration.services.config_mutation_drafts.export', return_value=[]), patch.object(cx, "frappe", fake), patch.object(api, "_sync_area_read_services", return_value={
                "HeroEmployees": [{"service": "reader", "mapping": "mapped"}]}):
            exported = json.loads(cx.export_config("snapshot", "source-a"))
        self.assertTrue(exported["replace_mappings"])
        self.assertEqual([row["definition_id"] for row in exported["mapping_package"]["mappings"]], ["R"])
        self.assertEqual(requested, [{"definition_id": "R", "status": "Active"}])
        config = exported["read_services"][0]["mapping"]
        self.assertNotIn("query_adapter_seal", config)
        self.assertEqual(config["query_adapter"]["read_service"], cx.SERVICE_REFERENCE + "HeroEmployees")
        self.assertEqual(set(cx._replacement_rules(exported)), {"R"})


class TestNativeReaderIdentity(unittest.TestCase):
    def test_a_project_referenced_customer_reader_is_preferred_to_a_newer_duplicate(self):
        parent = _Record(name="customer-map", schema_snapshot="snapshot", mapping_json='{}')
        project = _Record(mapping_json=json.dumps({"customer_service": "referenced-reader"}))
        rows = [_Record(name="newer-reader", execution_status="Approved"),
                _Record(name="referenced-reader", execution_status="Pending Approval")]
        calls = []
        fake = SimpleNamespace(get_all=lambda *a, **kw: rows,
                               get_doc=lambda dt, name, **kw: calls.append((name, kw)) or _Record(name=name))
        with patch.object(cx, "frappe", fake):
            reader = cx._native_bound_reader(parent, [project], lock=True)
        self.assertEqual(reader.name, "referenced-reader")
        self.assertEqual(calls, [("referenced-reader", {"for_update": True})])

    def test_revisioned_native_reader_save_keeps_its_relation_identity(self):
        from unittest.mock import Mock
        reader = _Record(name="native-reader", service_revision=7, schema_snapshot="snapshot", reload=Mock())
        fake = SimpleNamespace(get_doc=lambda *a: reader)
        with patch.object(cx, "frappe", fake), patch.object(api, "_workspace_payload", side_effect=json.loads), \
                patch.object(api, "_save_workspace_service", return_value={"query": {"query_id": "native-reader"}}) as save, \
                patch.object(api, "_workspace_service_validate") as validate:
            self.assertIs(cx._save_replacement_reader(_Record(name="snapshot"), entry("HeroCustomers"), reader), reader)
        self.assertEqual(save.call_args.args[2:4], ("native-reader", 7))
        validate.assert_called_once_with("snapshot", "", "native-reader", 7)


class TestReplacementFinalCheck(unittest.TestCase):
    def test_cleanup_is_followed_by_full_contract_validation(self):
        reader = _Record(name="reader", read_mapping="mapping")
        mapping = _Record(name="mapping")
        docs = {"reader": reader, "mapping": mapping}
        snapshot = _Record(name="snapshot")
        with patch.object(cx, "frappe", SimpleNamespace(get_doc=lambda dt, name: docs[name])), \
                patch("hero_integration.services.read_contract.from_documents", return_value=_Record(target_operation="HeroCustomers")) as contract:
            cx._verify_replacement_readers(snapshot, {"HeroCustomers": "reader"})
            contract.assert_called_once_with(reader, mapping, snapshot)
        with patch.object(cx, "frappe", SimpleNamespace(get_doc=lambda dt, name: docs[name])), \
                patch("hero_integration.services.read_contract.from_documents", return_value=_Record(target_operation="HeroProducts")):
            with self.assertRaisesRegex(cx.ConfigExchangeError, "another mapping area"):
                cx._verify_replacement_readers(snapshot, {"HeroCustomers": "reader"})


class TestPortableTargetConfiguration(unittest.TestCase):
    def test_current_local_schema_wins_over_foreign_packaged_fingerprint(self):
        binding = {'source_instance': 'local-source', 'endpoint_fingerprint': 'local-endpoint'}
        local = SimpleNamespace(name='current', source_instance='local-source', endpoint_fingerprint='local-endpoint',
            analysis_status='Validated', schema_fingerprint='current-schema')
        fake = SimpleNamespace(get_all=lambda *a, **kw: ['current'], get_doc=lambda *a: local)
        with patch.object(cx, 'frappe', fake), patch.object(cx, '_snapshot_binding', return_value=binding):
            result, state = cx._source_snapshot(None, {'settings': {'hero_source_instance': 'foreign-source'},
                'schema_snapshot': {'schema_fingerprint': 'old-foreign-schema'}}, 'local-source')
        self.assertIs(result, local)
        self.assertEqual(state, 'selected')

    def test_legacy_source_setting_is_ignored_and_never_exported(self):
        settings = {'hero_source_instance': 'local-source', 'timeout_seconds': 30}
        fake = SimpleNamespace(get_single=lambda *a: settings)
        with patch.object(cx, 'frappe', fake):
            self.assertEqual(cx._settings_changes({'settings': {'hero_source_instance': 'foreign', 'timeout_seconds': 45}}),
                {'timeout_seconds': 45})
        self.assertNotIn('hero_source_instance', cx._export_settings(settings))
