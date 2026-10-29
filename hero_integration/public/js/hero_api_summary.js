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

    function statusValue(value, positive = false) {
        const element = node("strong", positive ? "hero-api-summary-positive" : "", value || "Not available");
        return element;
    }

    function toolCard(title, description, tool, context) {
        const card = node("a", "hero-api-summary-tool");
        card.href = platform.url("api", {
            hero_tool: tool,
            snapshot: context?.selected_snapshot || "",
        });
        card.append(node("span", "hero-api-summary-tool-icon", "◇"), node("span", "hero-api-summary-tool-title", title), node("span", "hero-api-summary-tool-copy", description));
        return card;
    }

    function snapshotLabel(snapshot) {
        if (!snapshot) return "No validated schema snapshot";
        return snapshot.name || "Unnamed snapshot";
    }

    async function mount(workspace, routeContext = {}) {
        const root = node("section", "hero-api-summary");
        const header = node("header", "hero-api-summary-header");
        const heading = node("div", "");
        heading.append(node("h2", "", "API & Schema"), node("p", "", "Inspect saved GraphQL schema metadata and open the protected analysis tools."));
        const badge = node("span", "hero-api-summary-badge", "Loading schema status …");
        header.append(heading, badge);

        const statusStrip = node("dl", "hero-api-summary-status");
        const connectionCard = node("section", "hero-api-summary-card");
        const schemaCard = node("section", "hero-api-summary-card");
        const explorer = node("section", "hero-api-summary-explorer");
        const tools = node("section", "hero-api-summary-tools");
        const state = node("p", "hero-api-summary-state", "Loading stored metadata …");
        state.setAttribute("role", "status");

        const snapshot = document.createElement("select");
        snapshot.className = "form-control hero-api-summary-snapshot";
        snapshot.setAttribute("aria-label", "Saved GraphQL schema snapshot");
        const openAnalyzer = node("a", "btn btn-default", "Open Analyzer");
        const openGraph = node("a", "btn btn-default", "Open Graph Viewer");
        explorer.append(
            node("h3", "", "Schema explorer"),
            node("p", "hero-api-summary-copy", "Select a validated saved schema before opening the existing read-only explorer tools."),
            snapshot,
            node("div", "hero-api-summary-actions")
        );
        explorer.querySelector(".hero-api-summary-actions").append(openAnalyzer, openGraph);

        tools.append(node("h3", "", "Schema & API tools"));
        root.append(header, statusStrip, node("div", "hero-api-summary-columns", connectionCard, schemaCard), explorer, tools, state);
        workspace.replaceChildren(root);

        function href(tool, selectedSnapshot) {
            return platform.url("api", {hero_tool: tool, snapshot: selectedSnapshot || ""});
        }

        function metric(label, value, positive = false) {
            const term = node("dt", "", label);
            const definition = node("dd", "");
            definition.append(statusValue(value, positive));
            return [term, definition];
        }

        function render(context, settings) {
            const selectedName = routeContext.snapshot || routeContext.snapshot_name || context?.selected_snapshot || "";
            const selected = (context?.snapshots || []).find((item) => item.name === selectedName && item.usable) || context?.selected_snapshot_metadata || null;
            const available = (context?.snapshots || []).filter((item) => item.usable);
            const connected = Boolean(settings?.enabled && settings?.api_token_configured);
            const schemaReady = Boolean(selected?.usable || context?.selected_snapshot);

            badge.textContent = schemaReady ? "Validated schema available" : "No validated schema snapshot";
            badge.dataset.state = schemaReady ? "ready" : "empty";
            statusStrip.replaceChildren(...[
                ...metric("Connector", settings?.enabled ? "Enabled" : "Disabled", Boolean(settings?.enabled)),
                ...metric("Credential", settings?.api_token_configured ? "Configured" : "Not configured", Boolean(settings?.api_token_configured)),
                ...metric("Schema", schemaReady ? "Available" : "Not available", schemaReady),
                ...metric("Binding generation", String(context?.binding_generation || 0)),
                ...metric("GraphQL mutations", context?.mutations_enabled ? "Enabled" : "Disabled", false),
            ]);

            connectionCard.replaceChildren(
                node("h3", "", "Connection details"),
                node("p", "hero-api-summary-label", "GraphQL endpoint"),
                node("p", "hero-api-summary-value", settings?.api_url || "Not configured"),
                node("p", "hero-api-summary-label", "Access"),
                statusValue(connected ? "Endpoint and credential configured" : "Configuration incomplete", connected),
                node("p", "hero-api-summary-label", "Request timeout"),
                node("p", "hero-api-summary-value", `${settings?.timeout_seconds || 30} seconds`),
            );
            schemaCard.replaceChildren(
                node("h3", "", "GraphQL schema snapshot"),
                node("p", "hero-api-summary-label", "Latest usable snapshot"),
                node("p", "hero-api-summary-value", snapshotLabel(selected)),
                node("p", "hero-api-summary-label", "Schema fingerprint"),
                node("p", "hero-api-summary-value hero-api-summary-fingerprint", selected?.schema_fingerprint || context?.schema_fingerprint || "Not available"),
                node("p", "hero-api-summary-copy", selected?.analysis_detail || "A validated saved snapshot is required before operations can be explored."),
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
                node("h3", "", "Schema & API tools"),
                toolCard("API Analyzer", "Inspect read-only operations, fields and arguments from a validated schema.", "analyzer", context),
                toolCard("Graph Viewer", "Explore the saved schema graph, relations and focus views.", "graph", context),
                toolCard("GraphQL Services", "Review saved query and mutation contracts without changing their state.", "services", context),
            );
            state.textContent = schemaReady ? "Stored schema metadata is current." : "No validated schema is selected. Existing tools remain available once a snapshot is selected.";
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
            badge.dataset.state = "empty";
            state.textContent = error?.message || "Could not load stored HERO API metadata.";
        }

        return {destroy() { root.remove(); }};
    }

    window.hero_integration_api_summary = Object.freeze({mount});
})();
