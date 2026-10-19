(() => {
    "use strict";

    if (window.hero_integration_ui) return;

    const API = "hero_integration.api";
    const ASSETS = [
        "/assets/hero_integration/css/hero_tools.css",
        "/assets/hero_integration/css/hero_adapter.css",
        "/assets/hero_integration/css/hero_overview.css",
        "/assets/hero_integration/css/hero_settings.css",
        "/assets/hero_integration/js/hero_tools.js",
        "/assets/hero_integration/js/hero_overview.js",
        "/assets/hero_integration/js/hero_settings.js",
    ];
    const routes = {
        "connections/hero/api-analyzer": "hero-api-analyzer",
        "connections/hero/graph-viewer": "hero-graph-viewer",
        "connections/hero/graphql-services": "hero-graphql-services",
        "connections/hero/variable-mapping": "hero-adapter-tools",
        "connections/hero/settings": "hero-settings",
    };
    const unsupported = new Set([
        "synchronize_connection", "refresh_schema", "list_candidates", "mark_candidate",
        "remove_candidate", "execute_service", "get_analyzer_service_draft", "save_analyzer_service",
    ]);

    const request = (method, args = {}) =>
        frappe.call({method: `${API}.${method}`, args}).then((response) => response.message);

    const snapshotName = (args) => args.snapshot_name || args.connection_id || "";

    async function rpc(method, args = {}) {
        if (unsupported.has(method)) {
            throw new Error("This historic action is intentionally unavailable in the native workspace. Use the Mapping Editor or saved read-only service definitions.");
        }
        const snapshot_name = snapshotName(args);
        if (method === "get_context") return request("workspace_context", {snapshot_name});
        if (method === "get_schema") return request("workspace_schema", {
            snapshot_name, search: args.search || "", kind: args.kind || "", start: args.start || 0, page_size: args.page_size || 50,
        });
        if (method === "get_graph") return request("workspace_graph", {
            snapshot_name, start: args.start || 0, page_size: args.page_size || 200, node_id: args.node_id || "",
        });
        if (method === "list_services") return request("workspace_service_list", {
            snapshot_name, operation_kind: args.operation_kind || "query", start: args.start || 0, page_size: args.page_size || 50,
        });
        if (method === "get_query") return request("workspace_service_detail", {query_id: args.query_id});
        if (method === "save_query") return request("workspace_service_save", {
            snapshot_name, query_id: args.query_id || "", expected_revision: args.expected_revision || 0,
            payload_json: JSON.stringify({...args.payload, operation_kind: args.operation_kind || "query"}),
        });
        if (method === "validate_saved_query") return request("workspace_service_validate", {
            snapshot_name, query_id: args.query_id || "", expected_revision: args.expected_revision || 0,
        });
        if (method === "preview_query") return request("workspace_service_validate", {
            snapshot_name, payload_json: JSON.stringify({...args.payload, operation_kind: args.operation_kind || "query"}),
        });
        if (method === "delete_query") return request("workspace_service_delete", {
            query_id: args.query_id, expected_revision: args.expected_revision || 0,
        });
        throw new Error("This historic action has no native compatibility mapping.");
    }

    const loadStyle = (path) => new Promise((resolve, reject) => {
        if (document.querySelector(`link[href="${path}"]`)) return resolve();
        const link = document.createElement("link");
        link.rel = "stylesheet"; link.href = path; link.onload = resolve; link.onerror = reject;
        document.head.append(link);
    });
    const loadScript = (path) => new Promise((resolve, reject) => {
        if ([...document.scripts].some((script) => script.src.endsWith(path))) return resolve();
        frappe.require(path, resolve);
        window.setTimeout(() => {
            if (!window.hero_workspace_tools && path.endsWith("hero_tools.js")) reject(new Error("HERO workspace asset failed to load."));
        }, 10000);
    });

    let assets;
    const ensureAssets = () => assets ||= Promise.all(ASSETS.map((path) =>
        path.endsWith(".css") ? loadStyle(path) : loadScript(path)
    ));

    window.hero_integration_i18n = Object.freeze({text: (value) => __(value)});
    window.hero_integration_shell = Object.freeze({
        href: (_page, params = {}) => `/app/${params.tool === "graph" ? "hero-graph-viewer" : params.tool === "services" ? "hero-graphql-services" : "hero-api-analyzer"}`,
        mapping: () => "/app/hero-adapter-tools",
        navigationLink: (anchor) => anchor,
    });
    window.hero_integration_platform = Object.freeze({
        boot: {csrf_token: frappe.csrf_token || ""},
        url: (route) => `/app/${routes[route] || "hero-integration"}`,
        api: (method, args = {}) => {
            if (method === "workspace_settings_get") return request(method);
            if (method === "workspace_settings_save") return request(method, args);
            return rpc(method, args);
        },
        loadAce: async () => {
            if (window.ace) return;
            await frappe.ui.form.ControlCode.prototype.load_lib.call({});
        },
    });
    window.hero_integration_rpc = Object.freeze({call: (method, args) => rpc(method, args)});

    const page = (wrapper, title) => {
        const appPage = frappe.ui.make_app_page({parent: wrapper, title: __(title), single_column: true});
        appPage.main.empty();
        const host = document.createElement("section");
        host.className = "admin-workspace admin-workspace--tool";
        appPage.main.append(host);
        return host;
    };
    const failure = (host, error) => {
        host.replaceChildren(Object.assign(document.createElement("p"), {className: "text-danger", textContent: error.message || String(error)}));
    };
    const mountTool = async (wrapper, title, tool) => {
        const host = page(wrapper, title);
        try {
            await ensureAssets();
            const toolHost = document.createElement("div");
            toolHost.className = "admin-hero-tool-host";
            host.append(toolHost);
            const workspace = new window.hero_workspace_tools.Workspace(toolHost, {tool, operationKind: "query"});
            await workspace.show(true);
            return workspace;
        } catch (error) {
            failure(host, error);
            return null;
        }
    };
    const overview = async (wrapper) => {
        const host = page(wrapper, "HERO Integration");
        try {
            await ensureAssets();
            await window.hero_integration_overview.mount(host);
        } catch (error) { failure(host, error); }
    };
    const settings = async (wrapper) => {
        const host = page(wrapper, "HERO Settings");
        try {
            await ensureAssets();
            await window.hero_integration_settings_ui.mount(host);
        } catch (error) { failure(host, error); }
    };

    window.hero_integration_ui = Object.freeze({
        overview,
        settings,
        analyzer: (wrapper) => mountTool(wrapper, "HERO API Analyzer", "analyzer"),
        graphViewer: (wrapper) => mountTool(wrapper, "HERO Graph Viewer", "graph"),
        graphqlServices: (wrapper) => mountTool(wrapper, "HERO GraphQL Services", "services"),
    });
})();
