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

    const metricPresentation = Object.freeze({
        "Connector": "HERO integration availability",
        "Credential": "Stored credential state",
        "Binding": "Schema binding state",
        "Schema": "Saved schema metadata",
        "Local Configuration Access": "Local configuration permission",
        "HERO Mutation Execution": "Server-side mutation gate",
        "Automatic Synchronization": "Polling configuration",
    });

    // Status tile: label, value and a short description.
    function metric(label, value, state = "") {
        const card = el("div", "hero-page-stat");
        const v = el("div", "hero-page-stat-value", value);
        if (state) v.dataset.state = state;
        v.title = uiText(value);
        card.append(el("div", "hero-page-label", label), v, el("div", "hero-page-stat-sub", metricPresentation[label] || ""));
        return card;
    }

    // Read-only field of the connection details.
    function detail(label, value, mono = false) {
        const wrap = el("div", "hero-page-field");
        wrap.append(el("div", "hero-page-field-label", label), el("div", `hero-page-field-value${mono ? " hero-page-mono" : ""}`, value));
        return wrap;
    }

    // Capability row: title, description and a state pill.
    function capability(label, value, state = "") {
        const row = el("div", "hero-page-row");
        const text = el("div");
        text.append(el("strong", "", label), el("div", "hero-page-row-sub", metricPresentation[label] || ""));
        const pill = el("span", "hero-page-pill", value);
        if (state) pill.dataset.state = state;
        row.append(text, pill);
        return row;
    }

    function shortFingerprint(value) {
        if (!value) return "Not available";
        return value.length > 20 ? `${value.slice(0, 8)}…${value.slice(-7)}` : value;
    }

    function panel(title, description, ruled, ...body) {
        const section = el("section", "hero-page-panel");
        const head = el("div", `hero-page-panel-head${ruled ? " is-ruled" : ""}`);
        const text = el("div");
        text.append(el("h3", "", title), el("p", "", description));
        head.append(text);
        section.append(head, ...body);
        return section;
    }

    function errorMessage(error) {
        return typeof error?.message === "string" && error.message
            ? error.message
            : "Please check the saved HERO settings and try again.";
    }

    function toolLink(label, route, params = {}) {
        const link = el("a", "hero-page-chip", label);
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

    // Mappings of the parent snapshot are inherited as personal drafts; nothing is activated.
    function inheritanceMessage(inheritance) {
        if (!inheritance || inheritance.status === "NO_PARENT") return "";
        if (inheritance.status !== "COMPLETED") return " Mapping inheritance did not complete; retry it in the Mapping Editor.";
        const counts = inheritance.counts || {};
        return ` Inherited ${counts["Draft Created"] || 0} mapping(s) from the parent as personal drafts (${counts.Incompatible || 0} incompatible, ${counts.Skipped || 0} skipped). Nothing was activated.`;
    }

    function refreshResultMessage(result) {
        const snapshot = result?.snapshot_name || "saved snapshot";
        const types = Number.isInteger(result?.type_count) ? " (" + result.type_count + " types)" : "";
        if (result?.status === "UNCHANGED") return "Schema is unchanged: " + snapshot + types + ".";
        return "Schema refresh succeeded: " + snapshot + types + "." + inheritanceMessage(result?.inheritance);
    }

    async function mount(workspace) {
        const root = el("section", "hero-page hero-overview");
        const header = el("header", "hero-page-header");
        const title = el("div");
        title.append(
            el("h2", "", "HERO Overview"),
            el("p", "", "Saved GraphQL schema and local integration status.")
        );
        const reload = el("button", "hero-page-button", "Refresh Status");
        reload.type = "button";
        header.append(title, reload);

        const grid = el("div", "hero-page-stats hero-overview-grid");
        grid.setAttribute("aria-label", uiText("Connection and schema status"));
        const details = el("div", "hero-page-panel-body hero-overview-details");
        const capabilities = el("div", "hero-page-panel-body hero-overview-capabilities");
        const columns = el("div", "hero-overview-columns");
        columns.append(
            panel("Connection details", "Read-only information from the current HERO source.", true, details),
            panel("Capabilities & permissions", "Current integration gates and execution availability.", true, capabilities)
        );

        const sync = el("button", "hero-page-button", "Synchronize Binding");
        const schema = el("button", "hero-page-button is-quiet", "Refresh Schema");
        sync.type = schema.type = "button";
        const actions = el("div", "hero-overview-actions");
        actions.append(sync, schema);

        const links = el("div", "hero-overview-links");
        links.append(
            toolLink("API Analyzer", "api", {hero_tool: "analyzer"}),
            toolLink("Graph Viewer", "api", {hero_tool: "graph"}),
            toolLink("GraphQL Services", "api", {hero_tool: "services"}),
            toolLink("Mapping Editor", "mapping"),
            toolLink("Settings", "settings")
        );
        const toolbar = el("div", "hero-overview-toolbar");
        toolbar.append(actions, links);

        const status = el("div", "hero-page-note hero-overview-status", "Loading …");
        status.setAttribute("role", "status");
        root.append(header, grid, columns, panel("Actions & tools", "Use the existing integration operations and analysis tools.", false, toolbar), status);
        workspace.replaceChildren(root);

        let destroyed = false;
        let context = null;

        function say(text, state = "") {
            status.textContent = text;
            status.dataset.state = state;
        }
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
            const bindingState = c.needs_binding_update ? ["Update required", "warning"] : (c.connection_id ? ["Current", "ok"] : ["Not bound", "warning"]);
            grid.replaceChildren(
                metric("Connector", s.enabled ? "Enabled" : "Disabled", s.enabled ? "ok" : "off"),
                metric("Credential", s.api_token_configured ? "Configured" : "Not configured", s.api_token_configured ? "ok" : "warning"),
                metric("Binding", ...bindingState),
                metric("Schema", c.schema_fingerprint ? "Available" : "Not available", c.schema_fingerprint ? "" : "warning")
            );
            const pairs = el("div", "hero-overview-pairs");
            pairs.append(
                detail("Connection ID", c.connection_id || "Not bound", Boolean(c.connection_id)),
                detail("Timeout", `${s.timeout_seconds || 30} s`),
                detail("Binding Generation", String(c.binding_generation || 0)),
                detail("Schema fingerprint", shortFingerprint(c.schema_fingerprint), Boolean(c.schema_fingerprint))
            );
            const fingerprint = pairs.lastElementChild.lastElementChild;
            if (c.schema_fingerprint) fingerprint.title = c.schema_fingerprint;
            details.replaceChildren(detail("API Endpoint", s.api_url || "Not configured"), pairs);
            capabilities.replaceChildren(
                capability("Local Configuration Access", localAccess, localAccessState),
                capability("HERO Mutation Execution", mutationExecution, mutationExecutionState),
                capability("Automatic Synchronization", automaticSynchronization, automaticSynchronizationState)
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
            say(uiText("Loading …"));
            try {
                settings = await platform.api(SETTINGS);
                try {
                    context = await platform.api(CONTEXT, snapshotName ? {snapshot_name: snapshotName} : {});
                    say(uiText("HERO status is current."), "ok");
                } catch (_) {
                    context = null;
                    say(uiText("Connection context unavailable. Check HERO Settings."), "warning");
                }
                if (!destroyed) render();
            } catch (_) {
                if (!destroyed) say(uiText("Failed to load HERO status."), "error");
            } finally {
                if (!destroyed) busy(false);
            }
        }

        async function synchronize() {
            if (!context) return;
            busy(true);
            say(uiText("Synchronizing binding …"));
            try {
                await platform.api(SYNC, {expected_generation: context.binding_generation || 0}, {write: true});
                await load();
            } catch (_) {
                say(uiText("Failed to synchronize HERO binding."), "error");
                busy(false);
            }
        }

        async function refreshSchema() {
            if (!schemaRefreshAvailable(context, settings)) return;
            busy(true);
            say(uiText("Refreshing schema …"));
            try {
                const result = await platform.api(REFRESH, {}, {write: true});
                if (result.snapshot_name) {
                    const target = new URL(location.href);
                    target.searchParams.set("snapshot", result.snapshot_name);
                    history.replaceState(null, "", target.pathname + target.search + target.hash);
                }
                await load(result.snapshot_name);
                if (!destroyed) say(uiText(refreshResultMessage(result)), "ok");
            } catch (error) {
                say(uiText("Failed to refresh HERO schema.") + " " + errorMessage(error), "error");
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
