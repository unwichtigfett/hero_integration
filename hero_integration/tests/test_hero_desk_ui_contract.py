from __future__ import annotations

import json
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAGES = PACKAGE_ROOT / "hero_integration" / "page"
WORKSPACE_JS = PACKAGE_ROOT / "public" / "js" / "hero_workspace.js"
WORKSPACE_CSS = PACKAGE_ROOT / "public" / "css" / "hero_workspace.css"
PARITY_MATRIX = PACKAGE_ROOT.parent / "UI_PARITY_MATRIX.md"


class TestHeroDeskUiContract(unittest.TestCase):
    def test_native_desk_pages_are_system_manager_pages(self):
        expected = {
            "hero_integration": "hero-integration",
            "hero_api_analyzer": "hero-api-analyzer",
            "hero_graph_viewer": "hero-graph-viewer",
            "hero_graphql_services": "hero-graphql-services",
        }
        for directory, page_name in expected.items():
            with self.subTest(page_name=page_name):
                metadata = json.loads((PAGES / directory / f"{directory}.json").read_text())
                self.assertEqual(metadata["page_name"], page_name)
                self.assertEqual(metadata["module"], "HERO Integration")
                self.assertEqual(metadata["roles"], [{"role": "System Manager"}])

    def test_workspace_uses_stored_state_and_native_routes(self):
        source = WORKSPACE_JS.read_text()
        for route in ("hero-api-analyzer", "hero-graph-viewer", "hero-graphql-services", "hero-adapter-tools", "hero-migration-center"):
            self.assertIn(route, source)
        for endpoint in ("integration_overview", "schema_snapshot_list", "graphql_service_list", "graph_view"):
            self.assertIn(endpoint, source)
        self.assertIn("Open selected field in Mapping Editor", source)
        self.assertIn("This overview only reads saved Frappe state", source)
        self.assertNotIn("execute_graphql_service", source)

    def test_graph_workspace_keeps_native_interactions_in_the_single_workspace_engine(self):
        source = WORKSPACE_JS.read_text()
        for contract in (
            "Customer focus", "Fit graph", "QUERY_OPERATION", "Open selected field in Mapping Editor",
            "Search operation, type or field", "stored graph nodes displayed",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        self.assertNotIn("limited to 90 filtered nodes", source)

    def test_ui_assets_do_not_import_legacy_runtime(self):
        source = "\n".join(path.read_text() for path in [WORKSPACE_JS, *PAGES.glob("*/*.js")])
        for forbidden in ("auto_domain", "hero_connector", "process_core_shell", "semantic variable", "state machine"):
            self.assertNotIn(forbidden, source.lower())

    def test_mapping_editor_retains_one_confirmed_bulk_adoption(self):
        source = (PAGES / "hero_adapter_tools" / "hero_adapter_tools.js").read_text()
        self.assertIn("adopt_mapping_package", source)
        self.assertIn("frappe.confirm", source)
        self.assertIn("pendingSourcePath", source)

    def test_styles_are_packaged_with_the_app(self):
        self.assertTrue(WORKSPACE_CSS.is_file())
        pyproject = PACKAGE_ROOT.parent / "pyproject.toml"
        self.assertIn('"**/*.css"', pyproject.read_text())

    def test_parity_matrix_states_native_boundary_and_evidence(self):
        matrix = PARITY_MATRIX.read_text()
        self.assertIn("Legacy UI function", matrix)
        self.assertIn("IMPLEMENTED_SOURCE_TESTED", matrix)
        self.assertIn("Process-Core shell", matrix)
        self.assertIn("manual step", matrix)
