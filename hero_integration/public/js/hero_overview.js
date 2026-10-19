(() => {
    "use strict";

    const administratorI18n =
        window.hero_integration_i18n;

    const uiText =
        administratorI18n
            ? administratorI18n.text
            : (
                value
            ) => value;

    if (window.hero_integration_overview) return;

    const platform = window.hero_integration_platform;
    const CONTEXT = "workspace_context";
    const SETTINGS = "workspace_settings_get";
    const SYNC = "workspace_binding_refresh";
    const REFRESH = "workspace_schema_refresh";

    function el(tag, cls = "", text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = uiText(text);
        return node;
    }

    function metric(label, value, state = "") {
        const card = el("div", "hero-overview-metric");
        const v = el("div", "hero-overview-value", value);
        if (state) v.dataset.state = state;
        card.append(el("div", "hero-overview-label", label), v);
        return card;
    }

    function toolLink(label, route) {
        const link = el("a", "hero-overview-link", label);
        link.href = platform.url(route);
        return link;
    }

    async function mount(workspace) {
        const root = el("section", "hero-overview");
        const header = el("div", "hero-overview-header");
        const title = el("div");
        title.append(
            el("h2", "", "HERO Overview"),
            el("p", "", "Saved GraphQL schema and local integration status.")
        );
        const reload = el("button", "hero-overview-button secondary", "Refresh Status");
        reload.type = "button";
        header.append(title, reload);

        const grid = el("div", "hero-overview-grid");
        const actions = el("div", "hero-overview-actions");
        const sync = el("button", "hero-overview-button primary", "Synchronize Binding");
        const schema = el("button", "hero-overview-button primary", "Refresh Schema");
        sync.type = schema.type = "button";

        const links = el("div", "hero-overview-links");
        links.append(
            toolLink("API Analyzer", "connections/hero/api-analyzer"),
            toolLink("Graph Viewer", "connections/hero/graph-viewer"),
            toolLink("GraphQL Services", "connections/hero/graphql-services"),
            toolLink("Variable Mapping", "connections/hero/variable-mapping"),
            toolLink("Settings", "connections/hero/settings")
        );

        const status = el("div", "hero-overview-status", "Loading …");
        status.setAttribute("role", "status");
        actions.append(sync, schema);
        root.append(header, grid, actions, links, status);
        workspace.replaceChildren(root);

        let destroyed = false;
        let context = null;
        let settings = null;

        function busy(value) {
            reload.disabled = value;
            sync.disabled = value || !context?.can_write;
            schema.disabled = value || !context?.can_write || !context?.enabled ||
                context?.needs_binding_update || !context?.connection_id || !context?.schema_refresh_available;
        }

        function render() {
            const c = context || {};
            const s = settings || {};
            grid.replaceChildren(
                metric("Connector", s.enabled ? "Enabled" : "Disabled", s.enabled ? "ok" : "off"),
                metric("API Endpoint", s.api_url || "Not configured"),
                metric("Credential", s.api_token_configured ? "Configured" : "Not configured", s.api_token_configured ? "ok" : "warning"),
                metric("Timeout", `${s.timeout_seconds || 30} s`),
                metric("Connection ID", c.connection_id || "Not bound"),
                metric("Binding Generation", String(c.binding_generation || 0)),
                metric("Binding", c.needs_binding_update ? "Update required" : (c.connection_id ? "Current" : "Not bound"), c.needs_binding_update ? "warning" : "ok"),
                metric("Schema", c.schema_fingerprint || "Not available"),
                metric("Write Access", c.can_write ? "Allowed" : "Read only", c.can_write ? "ok" : "off")
            );
            sync.disabled = !c.can_write || !c.needs_binding_update || !c.binding_refresh_available;
            schema.disabled = !c.can_write || !c.enabled || c.needs_binding_update || !c.connection_id || !c.schema_refresh_available;
        }

        async function load() {
            busy(true);
            status.textContent = uiText("Loading …");
            try {
                settings = await platform.api(SETTINGS);
                try {
                    context = await platform.api(CONTEXT);
                    status.textContent = uiText("HERO status is current.");
                } catch (_) {
                    context = null;
                    status.textContent = uiText("Connection context unavailable. Check HERO Settings.");
                }
                if (!destroyed) render();
            } catch (_) {
                if (!destroyed) status.textContent = uiText("Failed to load HERO status.");
            } finally {
                if (!destroyed) busy(false);
            }
        }

        async function synchronize() {
            if (!context) return;
            busy(true);
            status.textContent = uiText("Synchronizing binding …");
            try {
                await platform.api(SYNC, {expected_generation: context.binding_generation || 0}, {write: true});
                await load();
            } catch (_) {
                status.textContent = uiText("Failed to synchronize HERO binding.");
                busy(false);
            }
        }

        async function refreshSchema() {
            if (!context?.connection_id) return;
            busy(true);
            status.textContent = uiText("Refreshing schema …");
            try {
                await platform.api(REFRESH, {
                    connection_id: context.connection_id,
                    binding_generation: context.binding_generation
                }, {write: true});
                await load();
            } catch (_) {
                status.textContent = uiText("Failed to refresh HERO schema.");
                busy(false);
            }
        }

        reload.addEventListener("click", load);
        sync.addEventListener("click", synchronize);
        schema.addEventListener("click", refreshSchema);
        await load();

        return {destroy() { destroyed = true; root.remove(); }};
    }

    window.hero_integration_overview = Object.freeze({mount});
})();
