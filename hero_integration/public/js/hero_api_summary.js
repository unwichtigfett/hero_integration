(() => {
    "use strict";

    if (window.hero_integration_api_summary) return;

    const platform = window.hero_integration_platform;

    function node(tag, className = "", text) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== undefined) element.textContent = String(text);
        return element;
    }

    function panel(className, title, description, ruled = false) {
        const section = node("section", `hero-page-panel ${className}`);
        const head = node("div", `hero-page-panel-head${ruled ? " is-ruled" : ""}`);
        const text = node("div", "");
        text.append(node("h3", "", title), node("p", "", description));
        head.append(text);
        section.append(head);
        return section;
    }

    const TOOL_ICONS = Object.freeze({analyzer: "◇", graph: "⬡", services: "⌘"});

    function toolCard(title, description, footer, tool, context) {
        const card = node("a", "hero-api-summary-tool");
        card.href = platform.url("api", {
            hero_tool: tool,
            snapshot: context?.selected_snapshot || "",
        });
        const top = node("span", "hero-api-summary-tool-top");
        const icon = node("span", "hero-api-summary-tool-icon", TOOL_ICONS[tool] || "◇");
        icon.setAttribute("aria-hidden", "true");
        top.append(icon, node("span", "hero-api-summary-tool-title", title));
        card.append(top, node("span", "hero-api-summary-tool-copy", description), node("span", "hero-api-summary-tool-footer", footer));
        return card;
    }

    async function mount(workspace, routeContext = {}) {
        const root = node("section", "hero-page hero-api-summary");
        const header = node("header", "hero-page-header");
        const heading = node("div", "");
        heading.append(node("h2", "", "API & Schema"), node("p", "", "Inspect saved GraphQL schema metadata and open the protected analysis tools."));
        const badge = node("span", "hero-page-badge", "Loading schema status …");
        header.append(heading, badge);

        const statusPanel = panel("hero-api-summary-status-panel", "Integration status", "Connection and schema capabilities", true);
        const statusStrip = node("dl", "hero-api-summary-status");
        statusPanel.append(statusStrip);

        const explorer = panel("hero-api-summary-explorer", "Schema explorer", "Select a validated saved schema before opening the existing read-only explorer tools.");
        const snapshot = document.createElement("select");
        snapshot.className = "form-control hero-api-summary-snapshot";
        snapshot.setAttribute("aria-label", "Saved GraphQL schema snapshot");
        const openAnalyzer = node("a", "hero-page-button", "Open Analyzer");
        const openGraph = node("a", "hero-page-button is-quiet", "Open Graph Viewer");
        const controls = node("div", "hero-api-summary-controls");
        controls.append(snapshot, openAnalyzer, openGraph);
        explorer.append(controls);

        const toolsPanel = panel("hero-api-summary-tools-panel", "Schema & API tools", "Read-only exploration of the selected validated schema");
        const tools = node("div", "hero-api-summary-tools");
        toolsPanel.append(tools);
        const state = node("p", "hero-page-note hero-api-summary-state", "Loading stored metadata …");
        state.setAttribute("role", "status");

        root.append(header, statusPanel, explorer, toolsPanel, state);
        workspace.replaceChildren(root);

        function href(tool, selectedSnapshot) {
            return platform.url("api", {hero_tool: tool, snapshot: selectedSnapshot || ""});
        }

        function metric(label, value, state = "") {
            const cell = node("div", "hero-api-summary-cell");
            const definition = node("dd", "", value || "Not available");
            if (state) definition.dataset.state = state;
            cell.append(node("dt", "hero-page-label", label), definition);
            return cell;
        }

        function render(context, settings) {
            const selectedName = routeContext.snapshot || routeContext.snapshot_name || context?.selected_snapshot || "";
            const selected = (context?.snapshots || []).find((item) => item.name === selectedName && item.usable) || context?.selected_snapshot_metadata || null;
            const available = (context?.snapshots || []).filter((item) => item.usable);
            const schemaReady = Boolean(selected?.usable || context?.selected_snapshot);

            badge.textContent = schemaReady ? "Validated schema available" : "No validated schema snapshot";
            badge.dataset.state = schemaReady ? "ok" : "";
            statusStrip.replaceChildren(
                metric("Connector", settings?.enabled ? "Enabled" : "Disabled", settings?.enabled ? "ok" : "off"),
                metric("Credential", settings?.api_token_configured ? "Configured" : "Not configured", settings?.api_token_configured ? "ok" : "warning"),
                metric("Binding generation", String(context?.binding_generation || 0)),
                metric("GraphQL mutations", context?.mutations_enabled ? "Enabled" : "Disabled", context?.mutations_enabled ? "" : "off"),
            );

            snapshot.replaceChildren(node("option", "", "Choose a validated GraphQL schema snapshot"));
            snapshot.options[0].value = "";
            available.forEach((item) => {
                const option = node("option", "", `${item.name}${item.coverage?.type_count ? ` · ${item.coverage.type_count} types` : ""}`);
                option.value = item.name;
                option.selected = item.name === selectedName || item.name === context?.selected_snapshot;
                snapshot.append(option);
            });
            openAnalyzer.href = href("analyzer", snapshot.value);
            openGraph.href = href("graph", snapshot.value);
            tools.replaceChildren(
                toolCard("API Analyzer", "Inspect read-only operations, fields and arguments from a validated schema.", "View analyzer", "analyzer", context),
                toolCard("Graph Viewer", "Explore the saved schema graph, relations and focus views.", "View graph", "graph", context),
                toolCard("GraphQL Services", "Review saved query and mutation contracts without changing their state.", "View services", "services", context),
            );
            state.textContent = schemaReady ? "Stored schema metadata is current." : "No validated schema is selected. Existing tools remain available once a snapshot is selected.";
            state.dataset.state = schemaReady ? "ok" : "warning";
        }

        snapshot.addEventListener("change", () => {
            openAnalyzer.href = href("analyzer", snapshot.value);
            openGraph.href = href("graph", snapshot.value);
        });

        try {
            const [context, settings] = await Promise.all([
                platform.api("workspace_context", routeContext.snapshot ? {snapshot_name: routeContext.snapshot} : {}),
                platform.api("workspace_settings_get"),
            ]);
            render(context, settings);
        } catch (error) {
            badge.textContent = "Stored metadata unavailable";
            badge.dataset.state = "";
            state.textContent = error?.message || "Could not load stored HERO API metadata.";
            state.dataset.state = "error";
        }

        return {destroy() { root.remove(); }};
    }

    window.hero_integration_api_summary = Object.freeze({mount});
})();
