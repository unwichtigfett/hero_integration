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
