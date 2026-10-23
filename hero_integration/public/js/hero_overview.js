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

    function errorMessage(error) {
        return typeof error?.message === "string" && error.message
            ? error.message
            : "Please check the saved HERO settings and try again.";
    }

    function toolLink(label, route, params = {}) {
        const link = el("a", "hero-overview-link", label);
        link.dataset.heroRoute = route;
        link.dataset.heroParams = JSON.stringify(params);
        link.href = platform.url(route, params);
        return link;
    }

    function localConfigurationAccess(context) {
        if (typeof context?.can_write !== "boolean") return ["Unavailable", "warning"];
        return context.can_write ? ["Allowed", "ok"] : ["Read only", "off"];
    }

    function mutationExecutionStatus(savedSettings) {
        switch (savedSettings?.mutation_execution_status) {
            case "DISABLED": return ["Disabled", "off"];
            case "ENABLED": return ["Enabled", "ok"];
            default: return ["Unavailable", "warning"];
        }
    }

    function automaticSynchronizationStatus(savedSettings) {
        switch (savedSettings?.polling?.mode) {
            case "OFF": return ["Disabled", "off"];
            case "MONITOR": return ["Monitor only", "warning"];
            case "AUTO_SYNC": return ["Enabled", "ok"];
            default: return ["Unavailable", "warning"];
        }
    }

    function schemaRefreshAvailable(context, savedSettings) {
        return Boolean(
            context?.can_write &&
            context?.schema_refresh_available &&
            savedSettings?.enabled &&
            savedSettings?.api_token_configured
        );
    }

    function refreshResultMessage(result) {
        const snapshot = result?.snapshot_name || "saved snapshot";
        const types = Number.isInteger(result?.type_count) ? " (" + result.type_count + " types)" : "";
        return result?.status === "UNCHANGED"
            ? "Schema is unchanged: " + snapshot + types + "."
            : "Schema refresh succeeded: " + snapshot + types + ".";
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
            toolLink("API Analyzer", "api", {hero_tool: "analyzer"}),
            toolLink("Graph Viewer", "api", {hero_tool: "graph"}),
            toolLink("GraphQL Services", "api", {hero_tool: "services"}),
            toolLink("Mapping Editor", "mapping"),
            toolLink("Settings", "settings")
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
            schema.disabled = value || !schemaRefreshAvailable(context, settings);
        }

        function render() {
            const c = context || {};
            const s = settings || {};
            const [localAccess, localAccessState] = localConfigurationAccess(context);
            const [mutationExecution, mutationExecutionState] = mutationExecutionStatus(settings);
            const [automaticSynchronization, automaticSynchronizationState] = automaticSynchronizationStatus(settings);
            grid.replaceChildren(
                metric("Connector", s.enabled ? "Enabled" : "Disabled", s.enabled ? "ok" : "off"),
                metric("API Endpoint", s.api_url || "Not configured"),
                metric("Credential", s.api_token_configured ? "Configured" : "Not configured", s.api_token_configured ? "ok" : "warning"),
                metric("Timeout", `${s.timeout_seconds || 30} s`),
                metric("Connection ID", c.connection_id || "Not bound"),
                metric("Binding Generation", String(c.binding_generation || 0)),
                metric("Binding", c.needs_binding_update ? "Update required" : (c.connection_id ? "Current" : "Not bound"), c.needs_binding_update ? "warning" : "ok"),
                metric("Schema", c.schema_fingerprint || "Not available"),
                metric("Local Configuration Access", localAccess, localAccessState),
                metric("HERO Mutation Execution", mutationExecution, mutationExecutionState),
                metric("Automatic Synchronization", automaticSynchronization, automaticSynchronizationState)
            );
            sync.disabled = !c.can_write || !c.needs_binding_update || !c.binding_refresh_available;
            schema.disabled = !schemaRefreshAvailable(c, s);
            links.querySelectorAll("a[data-hero-route]").forEach((link) => {
                const params = JSON.parse(link.dataset.heroParams || "{}");
                if (c.selected_snapshot) params.snapshot = c.selected_snapshot;
                link.href = platform.url(link.dataset.heroRoute, params);
            });
        }

        async function load(snapshotName = context?.selected_snapshot || "") {
            busy(true);
            status.textContent = uiText("Loading …");
            try {
                settings = await platform.api(SETTINGS);
                try {
                    context = await platform.api(CONTEXT, snapshotName ? {snapshot_name: snapshotName} : {});
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
            if (!schemaRefreshAvailable(context, settings)) return;
            busy(true);
            status.textContent = uiText("Refreshing schema …");
            try {
                const result = await platform.api(REFRESH, {}, {write: true});
                if (result.snapshot_name) {
                    const target = new URL(location.href);
                    target.searchParams.set("snapshot", result.snapshot_name);
                    history.replaceState(null, "", target.pathname + target.search + target.hash);
                }
                await load(result.snapshot_name);
                if (!destroyed) status.textContent = uiText(refreshResultMessage(result));
            } catch (error) {
                status.textContent = uiText("Failed to refresh HERO schema.") + " " + errorMessage(error);
                busy(false);
            }
        }

        reload.addEventListener("click", load);
        sync.addEventListener("click", synchronize);
        schema.addEventListener("click", refreshSchema);
        await load(new URLSearchParams(location.search).get("snapshot") || "");

        return {destroy() { destroyed = true; root.remove(); }};
    }

    window.hero_integration_overview = Object.freeze({mount});
})();
