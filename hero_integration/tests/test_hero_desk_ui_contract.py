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
UI_DOCUMENTATION = PACKAGE_ROOT.parent / "docs" / "ui_overview_1.0.md"
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
        # The technical Pending Sync area left the Synchronization page; its plan is resolved on the server.
        for removed in ("pending_sync", "Pending Sync", "Load approved plan", "runtime_plan_status", "Source instance'"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, migration)
        self.assertNotIn("sync_hero_preview", migration)
        self.assertNotIn("sync_hero_execute", migration)

    def test_workspace_uses_stored_state_and_one_canonical_route(self):
        source = WORKSPACE_JS.read_text()
        self.assertIn('CANONICAL_PAGE = "hero-integration"', source)
        self.assertIn("hero-unified-navigation", source)
        self.assertIn("hero_workspace.css", source)
        self.assertIn("ASSET_RELEASE", source)
        self.assertIn("const loadScript = (path) => {", source)
        self.assertIn("globalName && window[globalName] ? Promise.resolve() : frappe.require(path)", source)
        self.assertNotIn("frappe.require(assetUrl(path)", source)
        for endpoint in (
            "workspace_context", "workspace_schema", "workspace_graph", "workspace_service_save",
            "workspace_testing_settings_save",
        ):
            self.assertIn(endpoint, source)
        self.assertNotIn("workspace_erpnext_company", source)
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
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        for removed in ("ERPNext Company", "Save Company", "Confirm Company", "workspace_erpnext_company"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, source)
        doctype = json.loads((PACKAGE_ROOT / "hero_integration" / "doctype" / "hero_integration_settings" / "hero_integration_settings.json").read_text())
        fields = {field["fieldname"]: field for field in doctype["fields"]}
        self.assertEqual((fields["testing_enabled"]["fieldtype"], fields["testing_enabled"]["default"]), ("Check", "0"))
        self.assertEqual((fields["testing_max_actions"]["fieldtype"], fields["testing_max_actions"]["default"]), ("Int", "5"))

    def test_settings_save_bar_sits_above_polling_and_save_checks_the_token(self):
        source = SETTINGS_JS.read_text()
        # Only Refresh stays in the header; Reset and Save sit together above Polling, Save on the right.
        self.assertIn("headerActions.append(refresh);", source)
        self.assertIn('column("hero-settings-savebar", reset, save)', source)
        self.assertIn("testingCard, saveBar, pollingCard", source)
        # Saving an enabled connection with an unverified token loads the schema at once.
        self.assertIn('"workspace_schema_refresh"', source)
        self.assertIn("if (data.schema_refresh?.check_due) await verifyToken();", source)
        self.assertIn('schemaInterval.min = "10"', source)
        self.assertIn('schemaInterval.max = "240"', source)
        connection = source[source.index('section("HERO Connection"'):source.index('const sources = section(')]
        # Timeout first, then the schema refresh interval with its last check right below it.
        self.assertLess(connection.index("Timeout (seconds)"), connection.index("schemaRefreshField"))
        self.assertIn("schemaRefreshField.append(schemaStatus);", source)

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
        for token in ("--hero-text: var(--text-color", "--pc-text:", "--pc-diagramText:", "--pc-diagramBorder:"):
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
        for contract in ("graphql_total", "frappe_total", "Active Mappings", "Total definitions / field rules", "offset >= 10000",
                         "definition_id: active.definition_id", "mapping_type: kind, name: item.name", "page_size: 20"):
            self.assertIn(contract, source)
        for excluded in ("import_mapping_json", "runtime_plan_approve"):
            self.assertNotIn(excluded, source)

    def test_schema_explorer_is_admin_only_and_reads_saved_schema(self):
        source = MAPPING_EDITOR_JS.read_text()
        explorer = source.split("function renderSchemaExplorer()", 1)[1].split("function renderList()", 1)[0]
        for contract in ("if (!admin) return", "analysis_status === 'Validated'", "schema_snapshot_list",
                         "schema_operation_catalog", "leaveWorkspace", "hero_tool: 'graph'",
                         "epoch === viewEpoch", "section[0].isConnected", "tab === viewTab",
                         "hero-mapping-picker-resume", "loadSnapshots(name, operation)",
                         "Retry saved schema",
                         "readSnapshotPages((args) => rpc('schema_snapshot_list', args)", "snapshot list could not be loaded completely",
                         "previously selected snapshot is not available in this result"):
            self.assertIn(contract, explorer)
        for forbidden in ("workspace_schema_refresh", "mapping_suggestions", "Legacy synthetic record preview", "Show field suggestions"):
            self.assertNotIn(forbidden, explorer)
        self.assertIn("explorer.append(snapshot, operations, info, limitNotice, details)", explorer)

    def test_mapping_page_offers_no_import_or_export(self):
        # Import/export lives on the Settings page (Configuration package) only.
        source = MAPPING_EDITOR_JS.read_text()
        listing = source.split("function renderList()", 1)[1].split("function renderPersonalDraftList()", 1)[0]
        self.assertNotIn("function renderListExchange()", source)
        for removed in ("Load / Save mappings", "Saved field mapping package", "portable v2", "adopt_mapping_package",
                        "mapping_draft_exchange", "export_mapping_json"):
            self.assertNotIn(removed, source)
        detail = source.split("function renderDetail()", 1)[1].split("function renderContext(", 1)[0]
        for view in (listing, detail):
            self.assertIn("renderMappingsTable(body)", view)
            self.assertNotIn("renderListExchange()", view)
        context = source.split("function renderWorkspaceContext()", 1)[1].split("function inheritFromParent(", 1)[0]
        self.assertNotIn("renderDefinitionChooser(header)", context)

    def test_shared_writeback_save_is_scoped_and_conflict_guarded(self):
        source = MAPPING_EDITOR_JS.read_text()
        explorer = source.split("function renderSchemaExplorer()", 1)[1].split("function renderList()", 1)[0]
        for contract in ("if (!admin) return", "sharedWritebackDraftAccess(doc, row, name, frappe.session?.user)",
                         "reviewedWritebackEditable(doc.mapping_json, catalog)", "showSharedEditor", "if (consumed || locked() || !currentLegacy(serial, name)) return",
                         "sharedWritebackRequest({doc, row, actor:frappe.session?.user", "rpc('graphql_mapping_save', args, true)",
                         "No retry was made", "Your inputs are kept", "Only the last editor can change this shared draft"):
            self.assertIn(contract, explorer)
        for contract in ('mapping_name:doc?.name || ""', 'expected_revision:doc?.mapping_revision || 0',
                         'direction:"FRAPPE_TO_MUTATION"', 'activate:0', 'Only a current shared draft',
                         'sharedDirty || model?.dirty', 'reviewedWritebackEditable'):
            self.assertIn(contract, source)
        self.assertNotIn('activate:1', explorer)

    def test_schema_explorer_shows_no_package_import_or_export(self):
        source = MAPPING_EDITOR_JS.read_text()
        explorer = source.split("function renderSchemaExplorer()", 1)[1].split("function renderList()", 1)[0]
        for removed in ("text('Saved field mapping package')", "fileInput, exportPackage", "text('Complete configuration')"):
            self.assertNotIn(removed, explorer)
        self.assertIn("Import/export lives on the Settings page (Configuration package)", explorer)
        settings = (PACKAGE_ROOT / "public" / "js" / "hero_settings.js").read_text()
        for contract in ('section("Configuration package"', 'platform.api("config_export"', 'platform.api("config_import_preview"',
                         'platform.api("config_import", {payload_json: payload, confirmed: 1})', "frappe.confirm("):
            self.assertIn(contract, settings)
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
                         "hero-mapping-rule-details", "hero-mapping-row-detail", "hero-mapping-table",
                         'format("Search {0}", text(label))', "My saved revisions — read only"):
            self.assertIn(contract, source)
        catalog_loader = source.split('const getFieldCatalog =', 1)[1].split('const page =', 1)[0]
        self.assertNotIn('"Customer"', catalog_loader)
        self.assertNotIn('"Project"', catalog_loader)

    def test_polling_and_synchronization_controls_are_visible_without_a_new_page(self):
        settings = SETTINGS_JS.read_text()
        migration = MIGRATION_JS.read_text()
        for contract in (
            "Polling Settings", "hero-settings-polling-details", "polling_bootstrap", "Validated snapshot", "polling_pagination_strategy",
            "polling_reconciliation", "polling_retry_backoff_seconds", "polling_notifications", "AUTO_SYNC",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, settings)
        for removed in ("polling_data_sources", "polling_filters_json", "Operation filters JSON"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, settings)
        for contract in (
            "sync_page_state", "sync_area_start", "sync_service_set", "sync_page_runs", "sync_page_run_detail",
            "sync_page_conflicts", "cancel_run", "resume_run", "Cancel", "Continue", "Manual Actions",
            "Subscription Service", "Sync on", "Sync off", "Sync Services", "Service Information", "States",
            "Runtime History", "Function Details", "Conflicts", "Last successful", "Mapped objects",
            "Migration", "Runtime",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, migration)
        for removed in ("Preview", "Execute", "HERO GraphQL Service", "select approved read service", "Cheap Data Migration", "Full Data Migration",
                        "Customer Migration", "Projection Migration", "Schedule Migration", "start_migration"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, migration)

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
        shared = WORKSPACE_CSS.read_text()
        overview = OVERVIEW_JS.read_text()

        # Overview, API & Schema and Settings share the page panels of the Synchronization page tokens.
        for contract in (
            ".hero-integration-ui .hero-page", "container-name: hero-page", "--hero-page-ok: var(--hero-state-ok",
            ".hero-page-panel", ".hero-page-stat", ".hero-page-pill", ".hero-page-note",
            "border: 1px solid var(--pc-border)", "background: var(--pc-surface)",
            "repeat(4, minmax(0, 1fr))",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, shared)
        self.assertIn(".hero-integration-ui .hero-overview", source)
        self.assertIn("@container hero-page", source)
        self.assertNotIn("prefers-color-scheme", shared)
        for contract in ('"hero-page hero-overview"', "Connection details", "Capabilities & permissions", "Actions & tools"):
            with self.subTest(contract=contract):
                self.assertIn(contract, overview)

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
            "hero-mapping-section",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, workspace_css)
        sync_css = (PACKAGE_ROOT / "public" / "css" / "hero_sync.css").read_text()
        for contract in ("hero-sync-panel", "hero-sync-cards", "container-type: inline-size", "data-view=\"runtime\"",
                         "repeat(2, minmax(0, 1fr))", "repeat(4, minmax(0, 1fr))", "max-height: 640px"):
            with self.subTest(contract=contract):
                self.assertIn(contract, sync_css)
        for contract in ("HERO Connection", "Sources", "hero-settings-section"):
            with self.subTest(contract=contract):
                self.assertIn(contract, settings)
        self.assertIn("hero-sync-panel", migration)
        self.assertIn("hero-sync-cards", migration)
        self.assertIn("Field mapping details", mapping)

    def test_hero_owned_ui_strings_use_frappe_translation(self):
        self.assertIn('text: (value) => __(String(value))', WORKSPACE_JS.read_text())
        for source_path in (TOOLS_JS, SETTINGS_JS, MAPPING_EDITOR_JS, MIGRATION_JS):
            with self.subTest(source=source_path.name):
                self.assertIn("hero_integration_i18n", source_path.read_text())

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
        for asset in ("hero_tools.css", "hero_adapter.css", "hero_overview.css", "hero_settings.css", "hero_sync.css"):
            self.assertTrue((PACKAGE_ROOT / "public" / "css" / asset).is_file())
        self.assertTrue(MAPPING_EDITOR_JS.is_file())
        self.assertTrue(MIGRATION_JS.is_file())
        pyproject = PACKAGE_ROOT.parent / "pyproject.toml"
        self.assertIn('"**/*.css"', pyproject.read_text())

    def test_ui_documentation_and_source_keep_native_boundary_and_evidence(self):
        documentation = UI_DOCUMENTATION.read_text()
        self.assertIn("Explicit read-only Query execution", documentation)
        self.assertIn("Mutations are rejected before transport", documentation)
        self.assertIn("do not establish the behavior", documentation)
        api_source = (PACKAGE_ROOT / "api.py").read_text()
        for endpoint in ("workspace_context", "workspace_schema", "workspace_graph"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(f"def {endpoint}(", api_source)
                self.assertIn(endpoint, documentation)

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
