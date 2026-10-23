from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "hero_integration"
PRODUCTION_SUFFIXES = {".py", ".js", ".json", ".toml", ".txt"}
FORBIDDEN_RUNTIME_MARKERS = (
    "/home/fradev",
    "/home/luis",
    "/Users/",
    "hero.test",
    "172.16.",
    "postgresql://",
    "mysql://",
    "ssh-rsa",
    "ssh-ed25519",
)


class TestPortableRelease(unittest.TestCase):
    def test_bench_metadata_and_package_files_are_present(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())

        self.assertEqual(metadata["project"]["name"], "hero_integration")
        dependency = metadata["tool"]["bench"]["frappe-dependencies"]["frappe"]
        self.assertEqual(dependency, ">=16.0.0-dev,<17.0.0")
        self.assertEqual(metadata["project"]["dependencies"], [])
        self.assertTrue((PACKAGE / "modules.txt").is_file())
        self.assertTrue((PACKAGE / "patches.txt").is_file())
        self.assertTrue((PACKAGE / "install.py").is_file())
        manifest = (ROOT / "MANIFEST.in").read_text()
        self.assertIn("recursive-include hero_integration *.json *.js *.txt", manifest)
        self.assertIn("recursive-include hero_integration *.css", manifest)

    def test_required_release_documents_are_present(self):
        required = {
            "README.md",
            "INSTALLATION.md",
            "docs/CONFIGURATION.md",
            "docs/MANUAL_ACCEPTANCE.md",
            "docs/FEATURE_MATRIX.md",
            "docs/PORTABILITY_REPORT.md",
            "docs/DOCTYPE_EXTENSIONS.md",
            "docs/MAPPING_MATRIX.md",
        }

        self.assertTrue(all((ROOT / filename).is_file() for filename in required))

    def test_production_files_have_no_fixed_host_bindings(self):
        files = [
            path
            for path in PACKAGE.rglob("*")
            if path.is_file()
            and path.suffix in PRODUCTION_SUFFIXES
            and "tests" not in path.parts
            # AppleDouble sidecars can be materialized by macOS archive tools.
            # They are transport metadata, never Python package source.
            and not path.name.startswith("._")
        ]
        content = "\n".join(path.read_text() for path in files)

        self.assertFalse([marker for marker in FORBIDDEN_RUNTIME_MARKERS if marker in content])
        self.assertNotIn("auto_domain", content)
        self.assertNotIn("hero_connector", content)
        self.assertNotIn("import helpdesk", content)
        self.assertIn("from erpnext import __version__", (PACKAGE / "install.py").read_text())

    def test_security_and_permissions_contract(self):
        settings = self._doctype("hero_integration_settings")
        fields = {field["fieldname"]: field for field in settings["fields"]}

        self.assertEqual(fields["hero_api_token"]["fieldtype"], "Password")
        self.assertEqual(
            settings["permissions"],
            [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
        )
        hooks = (PACKAGE / "hooks.py").read_text()
        self.assertNotIn('required_apps = ["erpnext"]', hooks)
        self.assertIn("ERPNext v16 must be installed before", (PACKAGE / "install.py").read_text())
        self.assertIn('ERPNext_VERSION_CONSTRAINT = ">=16.0.0-dev,<17.0.0"', (PACKAGE / "install.py").read_text())
        self.assertIn("ensure_hero_source_fields", (PACKAGE / "install.py").read_text())
        self.assertIn("run_scheduled_sync", hooks)
        self.assertIn("fixtures = []", hooks)

    def test_installation_has_no_mapping_definition_or_legacy_runtime_fallback(self):
        install = (PACKAGE / "install.py").read_text()
        migration = (PACKAGE / "services" / "migration.py").read_text()
        legacy_mapping = (PACKAGE / "integrations" / "erpnext" / "mapping.py").read_text()

        self.assertNotIn("HERO Frappe Mapping", install)
        self.assertNotIn("customer_values", migration)
        self.assertNotIn("project_values", migration)
        self.assertIn("No HERO field mappings are installed", legacy_mapping)

    def test_canonical_link_doctype_contains_identity_status_and_provenance(self):
        link = self._doctype("hero_integration_link")
        fields = {field["fieldname"]: field for field in link["fields"]}

        self.assertEqual(
            set(fields),
            {
                "source_system", "source_instance", "source_type", "source_id", "source_key",
                "target_doctype", "target_name", "sync_status", "last_synced_on", "conflict_detail",
                "source_payload_hash", "target_payload_hash", "source_field_hashes", "target_field_hashes",
                "observed_source_hash", "observed_target_hash", "last_observed_on", "mapping_version",
            },
        )
        self.assertTrue(fields["source_key"]["unique"])
        self.assertEqual(fields["target_name"]["fieldtype"], "Dynamic Link")
        self.assertEqual(fields["target_name"]["options"], "target_doctype")

    @staticmethod
    def _doctype(name: str) -> dict[str, object]:
        path = PACKAGE / "hero_integration" / "doctype" / name / f"{name}.json"
        return json.loads(path.read_text())
