from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAGES = PACKAGE_ROOT / "hero_integration" / "page"
WORKSPACE_JS = PACKAGE_ROOT / "public" / "js" / "hero_workspace.js"
WORKSPACE_CSS = PACKAGE_ROOT / "public" / "css" / "hero_workspace.css"
TOOLS_JS = PACKAGE_ROOT / "public" / "js" / "hero_tools.js"
OVERVIEW_JS = PACKAGE_ROOT / "public" / "js" / "hero_overview.js"
OVERVIEW_CSS = PACKAGE_ROOT / "public" / "css" / "hero_overview.css"
SETTINGS_JS = PACKAGE_ROOT / "public" / "js" / "hero_settings.js"
MAPPING_EDITOR_JS = PACKAGE_ROOT / "public" / "js" / "hero_mapping_editor.js"
MIGRATION_JS = PACKAGE_ROOT / "public" / "js" / "hero_migration.js"
ADAPTER_CSS = PACKAGE_ROOT / "public" / "css" / "hero_adapter.css"
PARITY_MATRIX = PACKAGE_ROOT.parent / "UI_PARITY_MATRIX.md"
SIDEBAR_JSON = PACKAGE_ROOT / "hero_integration" / "sidebar" / "hero_integration" / "hero_integration.json"


class TestHeroDeskUiContract(unittest.TestCase):
    def test_only_the_canonical_desk_page_is_registered(self):
        definitions = sorted(PAGES.glob("*/*.json"))
        self.assertEqual(definitions, [PAGES / "hero_integration" / "hero_integration.json"])
        metadata = json.loads(definitions[0].read_text())
        self.assertEqual(metadata["page_name"], "hero-integration")
        self.assertEqual(metadata["module"], "HERO Integration")
        self.assertEqual(metadata["roles"], [{"role": "Employee"}, {"role": "System Manager"}])

    def test_universal_mapping_frontend_uses_only_authorized_lifecycle_contracts(self):
        workspace = WORKSPACE_JS.read_text()
        mapping = MAPPING_EDITOR_JS.read_text()
        for contract in (
            "isSystemManager", 'section === "mapping"', "frontend_mapping_list", "frontend_mapping_detail",
            "Employee read-only view", "definition_id", "expected_generation", "expected_draft_revision",
            "expected_active_revision", "unchanged", "mapping_draft_create", "mapping_draft_read",
            "mapping_draft_save", "mapping_draft_discard", "mapping_draft_revisions",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, workspace + mapping)
        self.assertNotIn('method: "frappe.client.get_list"', mapping)
        migration = MIGRATION_JS.read_text()
        for contract in (
            "pending_sync", "expected_plan_generation", "Pending Sync", "RECONCILIATION_REQUIRED",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, migration)
        self.assertNotIn("sync_hero_preview", migration)
        self.assertNotIn("sync_hero_execute", migration)

    def test_workspace_uses_stored_state_and_one_canonical_route(self):
        source = WORKSPACE_JS.read_text()
        self.assertIn('CANONICAL_PAGE = "hero-integration"', source)
        self.assertIn("hero-unified-navigation", source)
        self.assertIn("hero_workspace.css", source)
        self.assertIn("ASSET_RELEASE", source)
        self.assertIn("const loadScript = (path) => frappe.require(path);", source)
        self.assertNotIn("frappe.require(assetUrl(path)", source)
        for endpoint in (
            "workspace_context", "workspace_schema", "workspace_graph", "workspace_service_save",
            "workspace_testing_settings_save", "workspace_erpnext_company_save", "workspace_erpnext_company_confirm",
        ):
            self.assertIn(endpoint, source)
        self.assertIn("unsupported", source)
        self.assertNotIn("execute_graphql_service", source)
        for removed in ("legacyPageSections", "redirectLegacy", "hero-api-analyzer", "hero-graph-viewer", "hero-graphql-services", "hero-adapter-tools", "hero-migration-center", "hero-settings"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, source)

    def test_module_sidebar_contains_only_the_canonical_page(self):
        sidebar = json.loads(SIDEBAR_JSON.read_text())

        self.assertEqual(sidebar["doctype"], "Sidebar")
        self.assertEqual(sidebar["app"], "hero_integration")
        self.assertEqual(sidebar["module"], "HERO Integration")
        self.assertEqual(sidebar["title"], "HERO Integration")
        self.assertEqual(sidebar["standard"], 1)
        self.assertEqual(sidebar["items"], [{
            "label": "HERO Integration", "link_to": "hero-integration", "link_type": "Page", "type": "Link",
        }])

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

    def test_mutation_execution_requires_separate_server_side_contract_and_confirmation(self):
        source = "\n".join((WORKSPACE_JS.read_text(), TOOLS_JS.read_text()))
        self.assertIn("workspace_mutation_draft_validate", source)
        self.assertIn("Mutation drafts are local-only", source)
        self.assertIn("HERO_MUTATION_EXECUTION_DISABLED", source)
        api_source = (PACKAGE_ROOT / "api.py").read_text()
        execution = api_source.split("def execute_graphql_service", 1)[1].split("class _FrappePickerMetadata", 1)[0]
        self.assertIn("_assert_mutation_execution_released()", execution)
        self.assertIn("confirmation_fingerprint", execution)
        self.assertIn("HERO Mutation Audit", execution)
        self.assertIn("HeroMutationClient", execution)

    def test_canonical_settings_renders_a_backend_controlled_mutation_status(self):
        source = SETTINGS_JS.read_text()
        for contract in (
            "GraphQL Mutations", "workspace_mutation_execution_set", "Allow mutation execution",
            "System Managers", "active per-operation contract",
            "global gate",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        doctype = json.loads((PACKAGE_ROOT / "hero_integration" / "doctype" / "hero_integration_settings" / "hero_integration_settings.json").read_text())
        toggle = next(field for field in doctype["fields"] if field["fieldname"] == "allow_graphql_mutation_execution")
        self.assertEqual(toggle["fieldtype"], "Check")
        self.assertEqual(toggle["default"], "0")

    def test_settings_exposes_persistent_bounded_testing_controls(self):
        source = SETTINGS_JS.read_text()
        for contract in (
            "Testing", "testingEnabled", "testingMaxActions", "testing_enabled", "testing_max_actions",
            "manual synchronization previews are limited", "No business data is modified.",
            "Save Testing Settings", "workspace_testing_settings_save",
            "Save Company", "Confirm Company", "workspace_erpnext_company_save", "workspace_erpnext_company_confirm",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        doctype = json.loads((PACKAGE_ROOT / "hero_integration" / "doctype" / "hero_integration_settings" / "hero_integration_settings.json").read_text())
        fields = {field["fieldname"]: field for field in doctype["fields"]}
        self.assertEqual((fields["testing_enabled"]["fieldtype"], fields["testing_enabled"]["default"]), ("Check", "0"))
        self.assertEqual((fields["testing_max_actions"]["fieldtype"], fields["testing_max_actions"]["default"]), ("Int", "5"))

    def test_synchronization_lists_server_recorded_testing_outcomes(self):
        source = MIGRATION_JS.read_text()
        api_source = (PACKAGE_ROOT / "api.py").read_text()
        for contract in ("testing_status", "testing_executed_actions", "testing_records_received", "testing_results_json"):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
                self.assertIn(contract, api_source)
        self.assertIn("HERO requests", source)

    def test_graph_viewer_keeps_snapshot_selection_separate_from_performance_overview(self):
        source = TOOLS_JS.read_text()
        for contract in (
            "snapshotPicker", "selected_snapshot_metadata", "Schema Overview",
            "Select a node to open its focus view", "class cards condense", "graphNeedsOverview",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        self.assertIn("this.options.snapshot_name", source)
        self.assertIn("history.replaceState", source)
        self.assertIn("inspectNode(id,serial=this.sequence,open=true)", source)
        self.assertNotIn("focusButton", source)

    def test_workspace_theme_tokens_are_scoped_to_hero_hosts(self):
        source = ADAPTER_CSS.read_text()
        self.assertIn(".admin-hero-tool-host,\n.admin-compat-modal", source)
        for token in ("--hero-text: #000000", "--pc-text:", "--pc-diagramText:", "--pc-diagramBorder:"):
            with self.subTest(token=token):
                self.assertIn(token, source)
        self.assertNotIn(":root", source)

    def test_hero_text_uses_frappe_theme_attributes_not_os_preference(self):
        adapter = ADAPTER_CSS.read_text()
        overview = OVERVIEW_CSS.read_text()
        settings = (PACKAGE_ROOT / "public" / "css" / "hero_settings.css").read_text()
        tools = (PACKAGE_ROOT / "public" / "css" / "hero_tools.css").read_text()

        self.assertIn('html[data-theme="dark"]', adapter)
        self.assertIn("--hero-text: #ffffff", adapter)
        self.assertIn('html[data-theme="light"]', adapter)
        self.assertNotIn("prefers-color-scheme", adapter)
        self.assertNotIn("prefers-color-scheme", overview)
        for source in (overview, settings, tools):
            with self.subTest(source=source):
                self.assertIn("var(--pc-text)", source)
        self.assertIn(".ht-editor .ace_text-layer span", tools)
        self.assertIn(".admin-compat-modal", adapter)
        self.assertIn(".hero-integration-ui .btn", adapter)
        self.assertIn(".hero-integration-ui) a", adapter)

    def test_ui_assets_do_not_import_legacy_runtime(self):
        source = "\n".join(path.read_text() for path in [
            WORKSPACE_JS, TOOLS_JS, PACKAGE_ROOT / "public" / "js" / "hero_overview.js",
            PACKAGE_ROOT / "public" / "js" / "hero_settings.js", *PAGES.glob("*/*.js"),
        ])
        for forbidden in ("auto_domain", "hero_connector", "process_core_shell", "semantic variable", "state machine"):
            self.assertNotIn(forbidden, source.lower())

    def test_mapping_editor_bounds_collections_and_preserves_server_identity(self):
        source = MAPPING_EDITOR_JS.read_text()
        for contract in ("graphql_total", "frappe_total", "Definitions in this direction on this page", "HERO operations and mapping definitions",
                         "definition_id: active.definition_id", "mapping_type: kind, name: item.name", "page_size: 20"):
            self.assertIn(contract, source)
        for excluded in ("graphql_mapping_save", "import_mapping_json", "runtime_plan_approve"):
            self.assertNotIn(excluded, source)

    def test_schema_explorer_is_admin_only_and_reads_saved_schema(self):
        source = MAPPING_EDITOR_JS.read_text()
        explorer = source.split("function renderSchemaExplorer()", 1)[1].split("function renderList()", 1)[0]
        for contract in ("if (!admin) return", "analysis_status === 'Validated'", "schema_snapshot_list",
                         "schema_operation_catalog", "mapping_suggestions", "leaveWorkspace", "hero_tool: 'graph'",
                         "epoch === viewEpoch", "section[0].isConnected", "tab === viewTab",
                         "hero-mapping-picker-resume", "loadSnapshots(name, operation)",
                         "suggestionSerial === suggestionRequest", "Retry saved schema",
                         "result.length >= 100", "this list may be incomplete",
                         "previously selected snapshot is not available in this result"):
            self.assertIn(contract, explorer)
        for forbidden in ("graphql_mapping_save", "workspace_schema_refresh"):
            self.assertNotIn(forbidden, explorer)

    def test_legacy_package_actions_require_preview_confirmation_and_admin(self):
        source = MAPPING_EDITOR_JS.read_text()
        explorer = source.split("function renderSchemaExplorer()", 1)[1].split("function renderList()", 1)[0]
        for contract in ("if (!admin) return", "preview_mapping_package", "export_mapping_json",
                         "downloadExport(result.filename, result.content)", "item.selectable !== true",
                         "frappe.confirm", "adopt_mapping_package", "selected_ids_json: JSON.stringify(selection)",
                         "confirmed: true", "anyDirty()", "packageCurrent(serial, name)",
                         "Schema changes may persist if adoption fails", "Check the adoption record", "setBusy(true)"):
            self.assertIn(contract, explorer)
        self.assertLess(explorer.index("frappe.confirm"), explorer.index("rpc('adopt_mapping_package'"))
        self.assertIn("rpc('preview_mapping_package', {snapshot_name: name, payload_json: payload}, true)", explorer)
        self.assertIn('type: write ? "POST" : "GET"', source)

    def test_mapping_editor_has_explicit_save_and_lossless_local_undo(self):
        source = MAPPING_EDITOR_JS.read_text()
        for contract in ("class DraftState", "parseContent(JSON.stringify(value))", "this.undo.push", "this.undo.pop",
                         "mapping_draft_save", "Save and leave", "Discard changes", "Continue editing",
                         "TimestampMismatchError", "Check saved draft", "beforeunload"):
            self.assertIn(contract, source)
        self.assertNotIn("setInterval", source)
        self.assertNotIn("setTimeout(save", source)

    def test_mapping_editor_uses_existing_catalogs_and_scoped_table_details(self):
        source = MAPPING_EDITOR_JS.read_text()
        for contract in ("mapping_target_catalog", "mapping_target_catalog_page", "schema_operation_catalog",
                         "hero-mapping-editor-grid", "hero-mapping-row-detail", "hero-mapping-table",
                         "Search fields by name, full path or type", "My saved revisions — read only"):
            self.assertIn(contract, source)
        self.assertNotIn('"Customer"', source)
        self.assertNotIn('"Project"', source)

    def test_polling_and_synchronization_controls_are_visible_without_a_new_page(self):
        settings = SETTINGS_JS.read_text()
        migration = MIGRATION_JS.read_text()
        for contract in (
            "Polling Settings", "hero-settings-polling-details", "polling_data_sources", "polling_filters_json", "polling_pagination_strategy",
            "polling_reconciliation", "polling_retry_backoff_seconds", "polling_notifications", "AUTO_SYNC",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, settings)
        for contract in (
            "workspace_sync_status", "Preview never writes business or link status", "Cancel", "Resume",
            "Customer Migration", "Project Migration", "Cheap Data Migration", "Full Data Migration", "Sync",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, migration)

    def test_graphql_services_use_the_canonical_api_route_and_operation_kind(self):
        workspace = WORKSPACE_JS.read_text()
        tools = TOOLS_JS.read_text()

        self.assertIn("operationKind: context.operation_kind || \"query\"", workspace)
        self.assertIn("controller.workspace?.destroy?.()", workspace)
        self.assertIn("platform.url('api',{hero_tool:'services',operation_kind:kind,query:queryId||null})", tools)
        self.assertIn("ui.setRoute('api',{hero_tool:'services',operation_kind:kind,query:queryId||null})", tools)
        self.assertNotIn("connections/hero/graphql-services/", tools)
        self.assertNotIn("location.assign(this.serviceUrl", tools)

    def test_settings_polling_is_a_closed_native_accordion_without_side_effect_handlers(self):
        source = SETTINGS_JS.read_text()

        self.assertIn('document.createElement("details")', source)
        self.assertIn('node("summary", "hero-settings-polling-summary")', source)
        self.assertIn("pollingSummary.append(pollingSummaryTitle)", source)
        self.assertNotIn("pollingSummaryStatus", source)
        self.assertNotIn("hero-settings-polling-status", source)
        self.assertNotIn("pollingDetails.open", source)
        self.assertNotIn("pollingDetails.addEventListener", source)
        self.assertIn("Could not load HERO settings.", source)

    def test_overview_separates_local_access_from_backend_mutation_and_polling_status(self):
        source = OVERVIEW_JS.read_text()

        for contract in (
            "Local Configuration Access", "HERO Mutation Execution", "Automatic Synchronization",
            "mutation_execution_status", "savedSettings?.polling?.mode", 'case "DISABLED"',
            '["Unavailable", "warning"]',
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        self.assertNotIn('metric("Write Access"', source)

    def test_overview_metrics_are_scoped_bordered_status_cards_in_both_themes(self):
        source = OVERVIEW_CSS.read_text()

        for contract in (
            ".hero-integration-ui .hero-overview", "--hero-overview-card-surface",
            "--hero-overview-card-border", "#f0f1f2", "#65717d", "#272d33",
            "#aab3bb", 'html[data-theme="dark"]', "--hero-overview-card-label: var(--pc-text)",
            "repeat(auto-fit, minmax(min(100%, 220px), 1fr))",
            "border: 2px solid var(--hero-overview-card-border) !important",
            "background-color: var(--hero-overview-card-surface) !important",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)

    def test_hero_control_contrast_is_scoped_and_stateful(self):
        adapter = ADAPTER_CSS.read_text()
        settings_css = (PACKAGE_ROOT / "public" / "css" / "hero_settings.css").read_text()
        tools_css = (PACKAGE_ROOT / "public" / "css" / "hero_tools.css").read_text()

        for contract in (
            "--hero-field-bg", "--hero-field-border", "--hero-field-readonly-bg",
            "--hero-field-disabled-bg", "--hero-field-error", "--hero-text: #ffffff",
            ".admin-hero-tool-host .form-control", ".admin-compat-modal .form-control", ".hero-integration-ui .form-control",
            ".hero-integration-ui :is(input", "#30363d", "#aab3bb", ":focus-visible",
            "[aria-invalid=\"true\"]",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, adapter)
        self.assertIn("var(--hero-field-bg", settings_css)
        self.assertIn("hero-settings-section", settings_css)
        self.assertIn("display: list-item", settings_css)
        self.assertIn(".ht-editor .ace_scroller", tools_css)
        self.assertIn("border:2px solid var(--pc-border)", tools_css)

    def test_remaining_workspaces_use_scoped_grouping_surfaces(self):
        mapping = MAPPING_EDITOR_JS.read_text()
        migration = MIGRATION_JS.read_text()
        workspace_css = WORKSPACE_CSS.read_text()
        settings = SETTINGS_JS.read_text()

        for contract in (
            "hero-unified-content", "hero-unified-api-navigation",
            "hero-mapping-section", "hero-migration-panel", "hero-migration-actions",
            "hero-migration-action-buttons",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, workspace_css)
        for contract in ("HERO Connection", "Sources", "ERPNext Company", "hero-settings-section"):
            with self.subTest(contract=contract):
                self.assertIn(contract, settings)
        self.assertIn("hero-migration-panel", migration)
        self.assertIn("hero-migration-actions", migration)
        self.assertIn("Field mapping details", mapping)

    def test_hero_owned_ui_strings_are_not_routed_through_frappe_translation(self):
        sources = [WORKSPACE_JS, TOOLS_JS, SETTINGS_JS, MAPPING_EDITOR_JS, MIGRATION_JS]
        for source_path in sources:
            with self.subTest(source=source_path.name):
                self.assertNotIn("__(", source_path.read_text())

    def test_native_page_rpc_and_visual_mapping_failures_are_not_routed_to_legacy_or_rendered_as_objects(self):
        workspace = WORKSPACE_JS.read_text()
        mapping = MAPPING_EDITOR_JS.read_text()
        for contract in (
            "nativeMethods", '"workspace_context"', '"integration_overview"', "errorMessage(error)",
            "HERO workspace request failed", "HERO interface assets could not be loaded",
            "REQUIRED_ASSET_GLOBALS", "assertLoadedAssets",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, workspace)
        for contract in ("errorText(error)", "Your changes have been kept", "request !== epoch", "disposed"):
            self.assertIn(contract, mapping)

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
        source = WORKSPACE_JS.read_text()
        sections = re.search(r"const SECTION_NAMES = Object\.freeze\(\{(?P<body>.*?)\n    \}\);", source, re.DOTALL)
        tools = re.search(r"const API_TOOLS = Object\.freeze\(\{(?P<body>.*?)\n    \}\);", source, re.DOTALL)

        self.assertIsNotNone(sections)
        self.assertIsNotNone(tools)
        self.assertEqual(re.findall(r"^\s*(\w+):", sections.group("body"), re.MULTILINE), [
            "overview", "api", "mapping", "synchronization", "settings",
        ])
        self.assertEqual(re.findall(r"^\s*(\w+):", tools.group("body"), re.MULTILINE), [
            "analyzer", "graph", "services",
        ])
        self.assertIn("LEGACY_SECTION_TO_TOOL", source)
        self.assertIn('section === "migration"', source)
        self.assertIn("apiNavigation", source)
