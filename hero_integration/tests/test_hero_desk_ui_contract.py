from __future__ import annotations

import json
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAGES = PACKAGE_ROOT / "hero_integration" / "page"
WORKSPACE_JS = PACKAGE_ROOT / "public" / "js" / "hero_workspace.js"
WORKSPACE_CSS = PACKAGE_ROOT / "public" / "css" / "hero_workspace.css"
TOOLS_JS = PACKAGE_ROOT / "public" / "js" / "hero_tools.js"
MAPPING_EDITOR_JS = PACKAGE_ROOT / "public" / "js" / "hero_mapping_editor.js"
MIGRATION_JS = PACKAGE_ROOT / "public" / "js" / "hero_migration.js"
ADAPTER_CSS = PACKAGE_ROOT / "public" / "css" / "hero_adapter.css"
PARITY_MATRIX = PACKAGE_ROOT.parent / "UI_PARITY_MATRIX.md"


class TestHeroDeskUiContract(unittest.TestCase):
    def test_native_desk_pages_are_system_manager_pages(self):
        expected = {
            "hero_integration": "hero-integration",
            "hero_api_analyzer": "hero-api-analyzer",
            "hero_graph_viewer": "hero-graph-viewer",
            "hero_graphql_services": "hero-graphql-services", "hero_settings": "hero-settings",
        }
        for directory, page_name in expected.items():
            with self.subTest(page_name=page_name):
                metadata = json.loads((PAGES / directory / f"{directory}.json").read_text())
                self.assertEqual(metadata["page_name"], page_name)
                self.assertEqual(metadata["module"], "HERO Integration")
                self.assertEqual(metadata["roles"], [{"role": "System Manager"}])

    def test_workspace_uses_stored_state_and_one_canonical_route(self):
        source = WORKSPACE_JS.read_text()
        for route in ("hero-api-analyzer", "hero-graph-viewer", "hero-graphql-services", "hero-adapter-tools", "hero-settings"):
            self.assertIn(route, source)
        self.assertIn('CANONICAL_PAGE = "hero-integration"', source)
        self.assertIn("redirectLegacy", source)
        self.assertIn("hero-unified-navigation", source)
        for endpoint in ("workspace_context", "workspace_schema", "workspace_graph", "workspace_service_save"):
            self.assertIn(endpoint, source)
        self.assertIn("hero-adapter-tools", source)
        self.assertIn("unsupported", source)
        self.assertNotIn("execute_graphql_service", source)

    def test_graph_workspace_keeps_native_interactions_in_the_single_workspace_engine(self):
        source = TOOLS_JS.read_text()
        for contract in (
            "persistentGraphCache", "Fit", "QUERY_OPERATION", "bindGraphNavigation",
            "renderGraphScene", "Full Schema",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        self.assertNotIn("limited to 90 filtered nodes", source)

    def test_mutation_drafts_remain_local_and_execution_stays_disabled(self):
        source = "\n".join((WORKSPACE_JS.read_text(), TOOLS_JS.read_text()))
        self.assertIn("workspace_mutation_draft_validate", source)
        self.assertIn("Mutation drafts are local-only", source)
        self.assertIn("HERO_MUTATION_EXECUTION_DISABLED", source)
        api_source = (PACKAGE_ROOT / "api.py").read_text()
        execution = api_source.split("def execute_graphql_service", 1)[1].split("class _FrappePickerMetadata", 1)[0]
        self.assertIn("MUTATION_EXECUTION_DISABLED", execution)
        self.assertNotIn("HeroGraphQLClient", execution)

    def test_graph_viewer_keeps_snapshot_selection_separate_from_performance_overview(self):
        source = TOOLS_JS.read_text()
        for contract in (
            "snapshotPicker", "selected_snapshot_metadata", "Schema Overview",
            "Open Node Focus", "Klassenkarten verdichten", "graphNeedsOverview",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        self.assertIn("this.options.snapshot_name", source)
        self.assertIn("history.replaceState", source)
        self.assertIn("inspectNode(id,serial=this.sequence,open=false)", source)

    def test_workspace_theme_tokens_are_scoped_to_hero_hosts(self):
        source = ADAPTER_CSS.read_text()
        self.assertIn(".admin-hero-tool-host,\n.ht-focus-dialog", source)
        for token in ("--pc-text:", "--pc-diagramText:", "--pc-diagramBorder:"):
            with self.subTest(token=token):
                self.assertIn(token, source)
        self.assertNotIn(":root", source)

    def test_ui_assets_do_not_import_legacy_runtime(self):
        source = "\n".join(path.read_text() for path in [
            WORKSPACE_JS, TOOLS_JS, PACKAGE_ROOT / "public" / "js" / "hero_overview.js",
            PACKAGE_ROOT / "public" / "js" / "hero_settings.js", *PAGES.glob("*/*.js"),
        ])
        for forbidden in ("auto_domain", "hero_connector", "process_core_shell", "semantic variable", "state machine"):
            self.assertNotIn(forbidden, source.lower())

    def test_mapping_editor_retains_one_confirmed_bulk_adoption(self):
        source = MAPPING_EDITOR_JS.read_text()
        self.assertIn("adopt_mapping_package", source)
        self.assertIn("frappe.confirm", source)
        self.assertIn("pendingSourcePath", source)
        self.assertIn("mapping_target_catalog", source)
        self.assertIn("target_doctype", source)

    def test_styles_are_packaged_with_the_app(self):
        self.assertTrue(WORKSPACE_CSS.is_file())
        for asset in ("hero_tools.css", "hero_adapter.css", "hero_overview.css", "hero_settings.css"):
            self.assertTrue((PACKAGE_ROOT / "public" / "css" / asset).is_file())
        self.assertTrue(MAPPING_EDITOR_JS.is_file())
        self.assertTrue(MIGRATION_JS.is_file())
        pyproject = PACKAGE_ROOT.parent / "pyproject.toml"
        self.assertIn('"**/*.css"', pyproject.read_text())

    def test_parity_matrix_states_native_boundary_and_evidence(self):
        matrix = PARITY_MATRIX.read_text()
        self.assertIn("Original RPC", matrix)
        self.assertIn("IMPLEMENTED_SOURCE_TESTED", matrix)
        self.assertIn("LEGACY_PROCESS_ONLY", matrix)
        self.assertIn("manual step", matrix)

    def test_legacy_pages_only_redirect_into_the_canonical_workspace(self):
        expected = {
            "hero_adapter_tools": "mapping",
            "hero_api_analyzer": "analyzer",
            "hero_graph_viewer": "graph",
            "hero_graphql_services": "services",
            "hero_migration_center": "migration",
            "hero_settings": "settings",
        }
        for directory, section in expected.items():
            source = (PAGES / directory / f"{directory}.js").read_text()
            with self.subTest(directory=directory):
                self.assertIn(f'redirectLegacy("{section}")', source)
                self.assertNotIn("make_app_page", source)
