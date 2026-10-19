/* Preserved HERO Adapter host rendered against native saved Frappe state. */
(() => {
    "use strict";

    if (window.hero_integration_ui) return;

    const API = "hero_integration.api";
    const CANONICAL_PAGE = "hero-integration";
    const SECTION_NAMES = Object.freeze({
        overview: "Übersicht",
        api: "API & Schema",
        mapping: "Mapping",
        synchronization: "Synchronisation",
        settings: "Einstellungen",
    });
    const API_TOOLS = Object.freeze({
        analyzer: "API Analyzer",
        graph: "Graph Viewer",
        services: "GraphQL Services",
    });
    const LEGACY_SECTION_TO_TOOL = Object.freeze({analyzer: "analyzer", graph: "graph", services: "services"});
    const ASSETS = [
        "/assets/hero_integration/css/hero_tools.css",
        "/assets/hero_integration/css/hero_adapter.css",
        "/assets/hero_integration/css/hero_overview.css",
        "/assets/hero_integration/css/hero_settings.css",
        "/assets/hero_integration/js/hero_tools.js",
        "/assets/hero_integration/js/hero_overview.js",
        "/assets/hero_integration/js/hero_settings.js",
        "/assets/hero_integration/js/hero_mapping_editor.js",
        "/assets/hero_integration/js/hero_migration.js",
    ];
    const unsupported = new Set([
        "synchronize_connection", "refresh_schema", "list_candidates", "mark_candidate",
        "remove_candidate", "execute_service", "get_analyzer_service_draft", "save_analyzer_service",
    ]);

    const request = (method, args = {}) =>
        frappe.call({method: `${API}.${method}`, args}).then((response) => response.message);
    const snapshotName = (args) => args.snapshot_name || args.connection_id || "";

    async function rpc(method, args = {}) {
        if (method === "execute_service") {
            throw new Error("HERO_MUTATION_EXECUTION_DISABLED");
        }
        if (unsupported.has(method)) {
            throw new Error("This historic action is intentionally unavailable in the native workspace. Use the ERPNext Mapping Editor or saved read-only service definitions.");
        }
        const snapshot_name = snapshotName(args);
        if (method === "get_context") return request("workspace_context", {snapshot_name});
        if (method === "get_schema") return request("workspace_schema", {snapshot_name, search: args.search || "", kind: args.kind || "", start: args.start || 0, page_size: args.page_size || 50});
        if (method === "get_graph") return request("workspace_graph", {snapshot_name, start: args.start || 0, page_size: args.page_size || 200, node_id: args.node_id || ""});
        if (method === "list_services") return request("workspace_service_list", {snapshot_name, operation_kind: args.operation_kind || "query", start: args.start || 0, page_size: args.page_size || 50});
        if (method === "get_query") return request("workspace_service_detail", {query_id: args.query_id});
        if (method === "save_query") return request("workspace_service_save", {snapshot_name, query_id: args.query_id || "", expected_revision: args.expected_revision || 0, payload_json: JSON.stringify({...args.payload, operation_kind: args.operation_kind || "query"})});
        if (method === "validate_saved_query") return request("workspace_service_validate", {snapshot_name, query_id: args.query_id || "", expected_revision: args.expected_revision || 0});
        if (method === "preview_query" && args.operation_kind === "mutation") return request("workspace_mutation_draft_validate", {snapshot_name, payload_json: JSON.stringify(args.payload)});
        if (method === "preview_query") return request("workspace_service_validate", {snapshot_name, payload_json: JSON.stringify({...args.payload, operation_kind: args.operation_kind || "query"})});
        if (method === "delete_query") return request("workspace_service_delete", {query_id: args.query_id, expected_revision: args.expected_revision || 0});
        throw new Error("This historic action has no native compatibility mapping.");
    }

    const loadStyle = (path) => new Promise((resolve, reject) => {
        if (document.querySelector(`link[href="${path}"]`)) return resolve();
        const link = document.createElement("link");
        link.rel = "stylesheet"; link.href = path; link.onload = resolve; link.onerror = reject;
        document.head.append(link);
    });
    const loadScript = (path) => new Promise((resolve) => frappe.require(path, resolve));
    let assets;
    const ensureAssets = () => assets ||= Promise.all(ASSETS.map((path) => path.endsWith(".css") ? loadStyle(path) : loadScript(path)));

    function routeOptions() {
        const params = new URLSearchParams(location.search);
        return Object.assign({}, frappe.route_options || {}, Object.fromEntries(params.entries()));
    }
    function routeTarget(section = "", tool = "") {
        if (LEGACY_SECTION_TO_TOOL[section]) return {section: "api", tool: LEGACY_SECTION_TO_TOOL[section]};
        if (section === "migration") return {section: "synchronization", tool: ""};
        return {section: SECTION_NAMES[section] ? section : "overview", tool: API_TOOLS[tool] ? tool : "analyzer"};
    }
    function routeUrl(section, params = {}) {
        const target = routeTarget(section, params.hero_tool);
        const query = new URLSearchParams({...params, hero_section: target.section});
        if (target.section === "api") query.set("hero_tool", target.tool);
        else query.delete("hero_tool");
        return `/app/${CANONICAL_PAGE}?${query.toString()}`;
    }

    window.hero_integration_i18n = Object.freeze({text: (value) => __(value)});
    window.hero_integration_shell = Object.freeze({
        href: (_page, params = {}) => routeUrl("api", {hero_tool: params.tool}),
        mapping: () => routeUrl("mapping"),
        navigationLink: (anchor) => anchor,
    });
    window.hero_integration_platform = Object.freeze({
        boot: {csrf_token: frappe.csrf_token || ""},
        url: (section) => routeUrl(section),
        api: (method, args = {}) => ["workspace_settings_get", "workspace_settings_save", "workspace_mutation_execution_set"].includes(method) ? request(method, args) : rpc(method, args),
        loadAce: async () => {
            if (!window.ace) await frappe.ui.form.ControlCode.prototype.load_lib.call({});
        },
    });
    window.hero_integration_rpc = Object.freeze({call: (method, args) => rpc(method, args)});

    let controller;
    function resetPage(page) {
        ["clear_primary_action", "clear_secondary_action", "clear_menu", "clear_actions", "clear_actions_menu"].forEach((name) => page[name]?.());
    }
    function navigation(active) {
        const root = document.createElement("nav");
        root.className = "hero-unified-navigation";
        root.setAttribute("aria-label", __("HERO Integration sections"));
        Object.entries(SECTION_NAMES).forEach(([section, label]) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn-default btn-sm";
            button.textContent = __(label);
            button.toggleAttribute("data-active", section === active);
            button.addEventListener("click", () => setRoute(section));
            root.append(button);
        });
        return root;
    }
    function apiNavigation(active) {
        const root = document.createElement("nav");
        root.className = "hero-unified-api-navigation";
        root.setAttribute("aria-label", __("API & Schema tools"));
        Object.entries(API_TOOLS).forEach(([tool, label]) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn-default btn-sm";
            button.textContent = __(label);
            button.toggleAttribute("data-active", tool === active);
            button.addEventListener("click", () => setRoute("api", {hero_tool: tool}));
            root.append(button);
        });
        return root;
    }
    function failure(host, error) {
        host.replaceChildren(Object.assign(document.createElement("p"), {className: "text-danger", textContent: error.message || String(error)}));
    }
    async function render(section, context) {
        if (!controller) return;
        const {page, shell, content} = controller;
        resetPage(page);
        shell.replaceChildren(navigation(section), content);
        content.replaceChildren();
        const sectionPage = Object.create(page);
        sectionPage.main = $(content);
        try {
            await ensureAssets();
            if (section === "overview") await window.hero_integration_overview.mount(content);
            else if (section === "settings") await window.hero_integration_settings_ui.mount(content);
            else if (section === "mapping") window.hero_integration_mapping_editor.mount(sectionPage, context);
            else if (section === "synchronization") window.hero_integration_migration.mount(sectionPage);
            else {
                const tool = routeTarget(section, context.hero_tool).tool;
                content.append(apiNavigation(tool));
                const toolHost = document.createElement("div");
                toolHost.className = "admin-hero-tool-host";
                content.append(toolHost);
                const workspace = new window.hero_workspace_tools.Workspace(toolHost, {tool, operationKind: context.operation_kind || "query"});
                await workspace.show(true);
            }
        } catch (error) {
            failure(content, error);
        }
    }
    function showRoute() {
        const context = routeOptions();
        const target = routeTarget(context.hero_section, context.hero_tool);
        return render(target.section, {...context, hero_section: target.section, hero_tool: target.tool});
    }
    function setRoute(section, params = {}) {
        const target = routeTarget(section, params.hero_tool);
        const context = {...routeOptions(), ...params, hero_section: target.section};
        if (target.section === "api") context.hero_tool = target.tool;
        else delete context.hero_tool;
        frappe.set_route(CANONICAL_PAGE, context);
        return render(target.section, context);
    }
    async function mount(wrapper) {
        if (!controller) {
            const page = frappe.ui.make_app_page({parent: wrapper, title: __("HERO Integration"), single_column: true});
            page.main.addClass("hero-integration-ui");
            const shell = document.createElement("section");
            shell.className = "admin-workspace admin-workspace--tool";
            const content = document.createElement("div");
            content.className = "hero-unified-content";
            page.main.append(shell);
            controller = {page, shell, content};
        }
        await showRoute();
    }

    window.hero_integration_ui = Object.freeze({ensureStyles: ensureAssets, mount, showRoute, setRoute, routeOptions});
})();
