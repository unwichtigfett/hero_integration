from __future__ import annotations

import json
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAGES = PACKAGE_ROOT / "hero_integration" / "page"
WORKSPACE_JS = PACKAGE_ROOT / "public" / "js" / "hero_workspace.js"
WORKSPACE_CSS = PACKAGE_ROOT / "public" / "css" / "hero_workspace.css"
TOOLS_JS = PACKAGE_ROOT / "public" / "js" / "hero_tools.js"
SETTINGS_JS = PACKAGE_ROOT / "public" / "js" / "hero_settings.js"
MAPPING_EDITOR_JS = PACKAGE_ROOT / "public" / "js" / "hero_mapping_editor.js"
MIGRATION_JS = PACKAGE_ROOT / "public" / "js" / "hero_migration.js"
ADAPTER_CSS = PACKAGE_ROOT / "public" / "css" / "hero_adapter.css"
PARITY_MATRIX = PACKAGE_ROOT.parent / "UI_PARITY_MATRIX.md"


class TestHeroDeskUiContract(unittest.TestCase):
    def test_only_the_canonical_desk_page_is_registered(self):
        definitions = sorted(PAGES.glob("*/*.json"))
        self.assertEqual(definitions, [PAGES / "hero_integration" / "hero_integration.json"])
        metadata = json.loads(definitions[0].read_text())
        self.assertEqual(metadata["page_name"], "hero-integration")
        self.assertEqual(metadata["module"], "HERO Integration")
        self.assertEqual(metadata["roles"], [{"role": "System Manager"}])

    def test_workspace_uses_stored_state_and_one_canonical_route(self):
        source = WORKSPACE_JS.read_text()
        self.assertIn('CANONICAL_PAGE = "hero-integration"', source)
        self.assertIn("hero-unified-navigation", source)
        for endpoint in ("workspace_context", "workspace_schema", "workspace_graph", "workspace_service_save"):
            self.assertIn(endpoint, source)
        self.assertIn("unsupported", source)
        self.assertNotIn("execute_graphql_service", source)
        for removed in ("legacyPageSections", "redirectLegacy", "hero-api-analyzer", "hero-graph-viewer", "hero-graphql-services", "hero-adapter-tools", "hero-migration-center", "hero-settings"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, source)

    def test_graph_workspace_keeps_native_interactions_in_the_single_workspace_engine(self):
        source = TOOLS_JS.read_text()
        for contract in (
            "persistentGraphCache", "Fit", "QUERY_OPERATION", "bindGraphNavigation",
            "renderGraphScene", "Full Schema",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        self.assertNotIn("limited to 90 filtered nodes", source)
        for removed in ("appendMark", "candidateRecords", "loadCandidates", "Open Existing Variable Mapping"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, source)

    def test_mutation_drafts_remain_local_and_execution_stays_disabled(self):
        source = "\n".join((WORKSPACE_JS.read_text(), TOOLS_JS.read_text()))
        self.assertIn("workspace_mutation_draft_validate", source)
        self.assertIn("Mutation drafts are local-only", source)
        self.assertIn("HERO_MUTATION_EXECUTION_DISABLED", source)
        api_source = (PACKAGE_ROOT / "api.py").read_text()
        execution = api_source.split("def execute_graphql_service", 1)[1].split("class _FrappePickerMetadata", 1)[0]
        self.assertIn("MUTATION_EXECUTION_RELEASED = False", api_source)
        self.assertIn("_assert_mutation_execution_released()", execution)
        self.assertIn("MUTATION_EXECUTION_DISABLED", execution)
        self.assertNotIn("HeroGraphQLClient", execution)

    def test_canonical_settings_renders_a_backend_controlled_mutation_status(self):
        source = SETTINGS_JS.read_text()
        for contract in (
            "GraphQL Mutations", "workspace_mutation_execution_set", "Allow mutation execution",
            "System Managers", "Mutation drafts remain available independently",
            "HERO_MUTATION_EXECUTION_DISABLED",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        doctype = json.loads((PACKAGE_ROOT / "hero_integration" / "doctype" / "hero_integration_settings" / "hero_integration_settings.json").read_text())
        toggle = next(field for field in doctype["fields"] if field["fieldname"] == "allow_graphql_mutation_execution")
        self.assertEqual(toggle["fieldtype"], "Check")
        self.assertEqual(toggle["default"], "0")

    def test_graph_viewer_keeps_snapshot_selection_separate_from_performance_overview(self):
        source = TOOLS_JS.read_text()
        for contract in (
            "snapshotPicker", "selected_snapshot_metadata", "Schema Overview",
            "Knotenauswahl öffnet den zugehörigen Fokus", "Klassenkarten verdichten", "graphNeedsOverview",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        self.assertIn("this.options.snapshot_name", source)
        self.assertIn("history.replaceState", source)
        self.assertIn("inspectNode(id,serial=this.sequence,open=true)", source)
        self.assertNotIn("focusButton", source)

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
        for contract in (
            "adopt_mapping_package", "frappe.confirm", "pendingSourcePath",
            "mapping_target_catalog", "target_doctype", "hero-mapping-grid",
            "Vorschläge prüfen", "Aktive Mappings", "hero-mapping-statuses",
            "hero-mapping-table", "hero-mapping-advanced", "status: \"Draft\"",
            "mapping_source_picker_page", "mapping_target_picker_page", "PAGE_SIZE = 50",
            "hero-mapping-picker", "Show more suggestions", "Prepare Custom Field provisioning",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        self.assertIn("Promise.all", source)
        self.assertIn("sourceSearch.val()", source)
        self.assertIn("activeSearch.on", source)
        self.assertIn('if (!targetDocType.val()) return clearTargets', source)
        self.assertNotIn("hero-mapping-field-list", source)

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

    def test_workspace_tabs_remain_the_only_navigation_targets(self):
        source = "\n".join((WORKSPACE_JS.read_text(), MAPPING_EDITOR_JS.read_text()))
        for section in ("overview", "analyzer", "graph", "services", "mapping", "migration", "settings"):
            with self.subTest(section=section):
                self.assertIn(section, source)
