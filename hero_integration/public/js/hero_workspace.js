(() => {
    "use strict";

    if (window.hero_integration_ui) return;

    const ASSET_STYLE_ID = "hero-integration-workspace-style";
    const API = "hero_integration.api";

    const element = (tag, className = "", text = "") => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== "") node.textContent = text;
        return node;
    };

    const button = (label, handler, className = "btn btn-default btn-sm") => {
        const node = element("button", className, __(label));
        node.type = "button";
        node.addEventListener("click", handler);
        return node;
    };

    const request = (method, args = {}) => frappe.call({method: `${API}.${method}`, args})
        .then((response) => response.message);

    const routeOptions = () => ({...(frappe.route_options || {})});

    const setRoute = (route, options = {}) => {
        frappe.route_options = options;
        frappe.set_route(route);
    };

    const shortFingerprint = (value) => value ? `${value.slice(0, 12)}…` : __("Not available");

    const ensureStyles = () => {
        if (document.getElementById(ASSET_STYLE_ID)) return;
        const link = document.createElement("link");
        link.id = ASSET_STYLE_ID;
        link.rel = "stylesheet";
        link.href = "/assets/hero_integration/css/hero_workspace.css";
        document.head.append(link);
    };

    const createPage = (wrapper, title) => {
        ensureStyles();
        const page = frappe.ui.make_app_page({parent: wrapper, title: __(title), single_column: true});
        page.main.empty();
        const root = element("section", "hero-integration-ui");
        page.main.append(root);
        return {page, root};
    };

    const errorNotice = (root, error) => {
        root.replaceChildren(element("p", "hero-integration-notice is-error", error?.message || __("The requested saved state could not be loaded.")));
    };

    const emptyNotice = (message) => element("p", "hero-integration-notice", message);

    const statusBadge = (status) => element("span", `hero-integration-badge status-${String(status || "unknown").toLowerCase().replaceAll(" ", "-")}`, status || __("Unknown"));

    const addSnapshotPicker = async (root, onChange, selectedName = "") => {
        const group = element("div", "hero-integration-picker");
        const label = element("label", "", __("Saved GraphQL schema snapshot"));
        const select = element("select", "form-control");
        label.htmlFor = "hero-integration-snapshot";
        select.id = label.htmlFor;
        group.append(label, select);
        root.append(group);

        const refresh = async () => {
            const snapshots = await request("schema_snapshot_list");
            select.replaceChildren(element("option", "", __("Choose a saved snapshot")));
            snapshots.forEach((snapshot) => {
                const option = element("option", "", `${snapshot.name} · ${shortFingerprint(snapshot.schema_fingerprint)}`);
                option.value = snapshot.name;
                select.append(option);
            });
            const known = snapshots.some((snapshot) => snapshot.name === selectedName);
            select.value = known ? selectedName : snapshots[0]?.name || "";
            await onChange(select.value, snapshots);
        };
        select.addEventListener("change", () => onChange(select.value));
        await refresh();
        return {select, refresh};
    };

    const navigationCard = (title, description, route, icon) => {
        const card = element("button", "hero-integration-nav-card");
        card.type = "button";
        card.append(
            element("span", "hero-integration-nav-icon", icon),
            element("strong", "", __(title)),
            element("span", "", __(description)),
        );
        card.addEventListener("click", () => setRoute(route));
        return card;
    };

    const renderOverview = async (root) => {
        root.replaceChildren(emptyNotice(__("Loading saved HERO integration context…")));
        try {
            const summary = await request("integration_overview");
            const connection = summary.connection;
            const cards = element("div", "hero-integration-summary-grid");
            const connectionCard = element("article", "hero-integration-card");
            connectionCard.append(
                element("h3", "", __("Connection overview")),
                element("p", "", connection.endpoint_configured ? __("Endpoint configured") : __("Endpoint not configured")),
                element("p", "hero-integration-muted", `${connection.allowed_host_count} ${__("allowed hosts")} · ${connection.scheduled_sync_enabled ? __("scheduled sync enabled") : __("scheduled sync disabled")}`),
                element("p", "hero-integration-muted", `${__("Sync status")}: ${connection.sync_status}`),
            );
            const snapshotCard = element("article", "hero-integration-card");
            snapshotCard.append(element("h3", "", __("GraphQL schema")), element("p", "hero-integration-metric", String(summary.snapshot_count)), element("p", "hero-integration-muted", __("saved snapshots")));
            const mappingCard = element("article", "hero-integration-card");
            mappingCard.append(
                element("h3", "", __("Mappings")),
                element("p", "hero-integration-metric", String(summary.mapping_counts.Active || 0)),
                element("p", "hero-integration-muted", `${summary.mapping_counts.Draft || 0} ${__("draft")} · ${__("active")} ${summary.mapping_counts.Active || 0}`),
            );
            const runCard = element("article", "hero-integration-card");
            const latest = summary.latest_run?.[0];
            runCard.append(
                element("h3", "", __("Migration and synchronization")),
                latest ? element("p", "", `${latest.action}: ${latest.state}`) : element("p", "", __("No migration has been started.")),
                element("p", "hero-integration-muted", __("Writes are always HERO to Frappe only.")),
            );
            cards.append(connectionCard, snapshotCard, mappingCard, runCard);

            const navigation = element("div", "hero-integration-navigation");
            navigation.append(
                navigationCard("API Analyzer", "Inspect saved schema types and fields", "hero-api-analyzer", "◫"),
                navigationCard("Graph Viewer", "Explore stored GraphQL relationships", "hero-graph-viewer", "◌"),
                navigationCard("GraphQL Services", "Manage validated read-only services", "hero-graphql-services", "⌁"),
                navigationCard("Mapping Editor", "Review field suggestions and adoption", "hero-adapter-tools", "⇄"),
                navigationCard("Migration Center", "Run dry-runs and controlled imports", "hero-migration-center", "→"),
            );
            root.replaceChildren(
                element("h2", "hero-integration-heading", __("HERO Integration")),
                element("p", "hero-integration-lead", __("A native Frappe workspace for saved HERO schema, mapping and controlled migration.")),
                cards,
                element("h3", "hero-integration-section-heading", __("Workspace")),
                navigation,
                emptyNotice(__("This overview only reads saved Frappe state. It never contacts HERO.")),
            );
        } catch (error) {
            errorNotice(root, error);
        }
    };

    const overview = (wrapper) => {
        const {page, root} = createPage(wrapper, "HERO Integration");
        page.set_primary_action(__("Schema aktualisieren"), () => frappe.new_doc("HERO GraphQL Schema Snapshot"));
        page.set_secondary_action(__("Kontext neu laden"), () => renderOverview(root));
        renderOverview(root);
    };

    const analyzer = (wrapper) => {
        const {root} = createPage(wrapper, "HERO API Analyzer");
        const content = element("div", "hero-integration-panel");
        root.append(element("p", "hero-integration-lead", __("Analyze a saved GraphQL snapshot without contacting HERO.")), content);
        const initial = routeOptions();
        addSnapshotPicker(root, async (snapshotName) => {
            content.replaceChildren();
            if (!snapshotName) {
                content.append(emptyNotice(__("Create or select a validated snapshot to analyze its saved structure.")));
                return;
            }
            try {
                const graph = await request("graph_view", {snapshot_name: snapshotName});
                const types = graph.nodes.filter((node) => node.id.startsWith("type:"));
                const fieldsByType = new Map();
                graph.edges.filter((edge) => edge.kind === "HAS_FIELD").forEach((edge) => {
                    const list = fieldsByType.get(edge.source) || [];
                    list.push(graph.nodes.find((node) => node.id === edge.target));
                    fieldsByType.set(edge.source, list.filter(Boolean));
                });
                const counts = types.reduce((value, node) => {
                    value[node.kind] = (value[node.kind] || 0) + 1;
                    return value;
                }, {});
                const summary = element("div", "hero-integration-summary-grid compact");
                summary.append(
                    element("article", "hero-integration-card", ""),
                    element("article", "hero-integration-card", ""),
                    element("article", "hero-integration-card", ""),
                );
                summary.children[0].append(element("h3", "", __("Types")), element("p", "hero-integration-metric", String(types.length)));
                summary.children[1].append(element("h3", "", __("Fields")), element("p", "hero-integration-metric", String(graph.nodes.filter((node) => node.kind === "FIELD").length)));
                summary.children[2].append(element("h3", "", __("Fingerprint")), element("p", "hero-integration-fingerprint", graph.fingerprint));
                const layout = element("div", "hero-integration-split");
                const list = element("div", "hero-integration-list");
                const detail = element("div", "hero-integration-detail");
                const search = element("input", "form-control");
                search.placeholder = __("Search types and fields");
                const renderList = () => {
                    const needle = search.value.trim().toLowerCase();
                    list.replaceChildren(search);
                    types.filter((node) => !needle || node.label.toLowerCase().includes(needle) || node.kind.toLowerCase().includes(needle)).forEach((node) => {
                        const item = button(`${node.label} · ${node.kind} (${fieldsByType.get(node.id)?.length || 0})`, () => {
                            detail.replaceChildren(
                                element("h3", "", node.label),
                                statusBadge(node.kind),
                                element("p", "hero-integration-muted", `${fieldsByType.get(node.id)?.length || 0} ${__("stored fields")}`),
                                button("Open in Graph Viewer", () => setRoute("hero-graph-viewer", {snapshot_name: snapshotName, selected_node: node.id}), "btn btn-primary btn-sm"),
                            );
                            const fieldList = element("div", "hero-integration-field-list");
                            (fieldsByType.get(node.id) || []).forEach((field) => fieldList.append(element("span", "hero-integration-field-chip", field.label)));
                            detail.append(fieldList);
                        });
                        list.append(item);
                    });
                    if (list.children.length === 1) list.append(emptyNotice(__("No stored type matches this search.")));
                };
                search.addEventListener("input", renderList);
                renderList();
                detail.append(emptyNotice(__("Select a type to inspect its fields and open its graph context.")));
                layout.append(list, detail);
                const categoryText = Object.entries(counts).map(([kind, count]) => `${kind}: ${count}`).join(" · ");
                content.append(summary, element("p", "hero-integration-muted", categoryText), layout);
            } catch (error) {
                errorNotice(content, error);
            }
        }, initial.snapshot_name);
    };

    const graphViewer = (wrapper) => {
        const {root} = createPage(wrapper, "HERO Graph Viewer");
        const content = element("div", "hero-integration-panel");
        root.append(element("p", "hero-integration-lead", __("Explore only the validated schema stored in Frappe.")), content);
        const options = routeOptions();
        addSnapshotPicker(root, async (snapshotName) => {
            content.replaceChildren();
            if (!snapshotName) {
                content.append(emptyNotice(__("Choose a saved snapshot to display its graph.")));
                return;
            }
            try {
                const graph = await request("graph_view", {snapshot_name: snapshotName});
                const controls = element("div", "hero-integration-graph-controls");
                const search = element("input", "form-control");
                search.placeholder = __("Filter graph nodes");
                const operation = element("select", "form-control");
                ["HeroCustomers", "HeroProjectMatches"].forEach((value) => {
                    const item = element("option", "", value);
                    item.value = value;
                    operation.append(item);
                });
                operation.value = options.operation || "HeroCustomers";
                controls.append(search, operation);
                const canvas = element("div", "hero-integration-graph-canvas");
                const inspector = element("aside", "hero-integration-graph-inspector");
                const render = () => {
                    const needle = search.value.trim().toLowerCase();
                    const visible = graph.nodes.filter((node) => !needle || node.label.toLowerCase().includes(needle) || node.kind.toLowerCase().includes(needle)).slice(0, 90);
                    const ids = new Set(visible.map((node) => node.id));
                    const rows = Math.max(1, Math.ceil(visible.length / 6));
                    const width = 960;
                    const height = Math.max(360, rows * 105);
                    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
                    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
                    svg.setAttribute("role", "img");
                    svg.setAttribute("aria-label", __("Stored GraphQL schema graph"));
                    const position = new Map(visible.map((node, index) => [node.id, {x: 24 + (index % 6) * 156, y: 24 + Math.floor(index / 6) * 105}]));
                    graph.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target)).forEach((edge) => {
                        const source = position.get(edge.source);
                        const target = position.get(edge.target);
                        const line = document.createElementNS(svg.namespaceURI, "line");
                        line.setAttribute("x1", source.x + 58); line.setAttribute("y1", source.y + 33);
                        line.setAttribute("x2", target.x + 58); line.setAttribute("y2", target.y + 33);
                        line.setAttribute("class", "hero-integration-graph-edge");
                        svg.append(line);
                    });
                    const showNode = (node) => {
                        const related = graph.edges.filter((edge) => edge.source === node.id || edge.target === node.id);
                        inspector.replaceChildren(
                            element("h3", "", node.label),
                            statusBadge(node.kind),
                            element("p", "hero-integration-muted", `${related.length} ${__("stored relationships")}`),
                        );
                        if (node.kind === "FIELD") {
                            inspector.append(button("Open selected field in Mapping Editor", () => setRoute("hero-adapter-tools", {
                                snapshot_name: snapshotName,
                                operation: operation.value,
                                source_path: node.label,
                            }), "btn btn-primary btn-sm"));
                        } else {
                            inspector.append(emptyNotice(__("Select a field node to hand its source name to the Mapping Editor.")));
                        }
                    };
                    visible.forEach((node) => {
                        const point = position.get(node.id);
                        const group = document.createElementNS(svg.namespaceURI, "g");
                        group.setAttribute("class", `hero-integration-graph-node kind-${node.kind.toLowerCase()}`);
                        group.setAttribute("tabindex", "0");
                        group.setAttribute("role", "button");
                        group.setAttribute("aria-label", `${node.label} ${node.kind}`);
                        const rect = document.createElementNS(svg.namespaceURI, "rect");
                        rect.setAttribute("x", point.x); rect.setAttribute("y", point.y); rect.setAttribute("width", "116"); rect.setAttribute("height", "66"); rect.setAttribute("rx", "7");
                        const name = document.createElementNS(svg.namespaceURI, "text");
                        name.setAttribute("x", point.x + 8); name.setAttribute("y", point.y + 28); name.textContent = node.label.slice(0, 17);
                        const kind = document.createElementNS(svg.namespaceURI, "text");
                        kind.setAttribute("x", point.x + 8); kind.setAttribute("y", point.y + 48); kind.setAttribute("class", "hero-integration-graph-kind"); kind.textContent = node.kind;
                        group.append(rect, name, kind);
                        group.addEventListener("click", () => showNode(node));
                        group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); showNode(node); } });
                        svg.append(group);
                    });
                    canvas.replaceChildren(svg, element("p", "hero-integration-muted", visible.length < graph.nodes.length ? __("Graph display is limited to 90 filtered nodes.") : ""));
                    const requested = visible.find((node) => node.id === options.selected_node);
                    if (requested) showNode(requested);
                };
                search.addEventListener("input", render);
                render();
                inspector.append(emptyNotice(__("Select a node to inspect its saved graph relationships.")));
                const layout = element("div", "hero-integration-graph-layout");
                layout.append(canvas, inspector);
                content.append(controls, layout, element("p", "hero-integration-fingerprint", `${__("Schema fingerprint")}: ${graph.fingerprint}`));
            } catch (error) {
                errorNotice(content, error);
            }
        }, options.snapshot_name);
    };

    const graphqlServices = (wrapper) => {
        const {page, root} = createPage(wrapper, "HERO GraphQL Services");
        const content = element("div", "hero-integration-panel");
        root.append(element("p", "hero-integration-lead", __("Manage saved GraphQL services. This page does not execute them.")), content);
        page.set_primary_action(__("New GraphQL Service"), () => frappe.new_doc("HERO GraphQL Service"));
        page.set_secondary_action(__("Neu laden"), () => renderServices(content));
        renderServices(content);
    };

    const renderServices = async (content) => {
        content.replaceChildren(emptyNotice(__("Loading saved GraphQL services…")));
        try {
            const services = await request("graphql_service_list");
            if (!services.length) {
                content.replaceChildren(emptyNotice(__("No GraphQL service has been saved yet.")));
                return;
            }
            const table = element("table", "table table-bordered hero-integration-table");
            const head = element("thead");
            const headRow = element("tr");
            ["Service", "Operation", "Snapshot", "Status", "Revision", ""].forEach((label) => headRow.append(element("th", "", __(label))));
            head.append(headRow);
            const body = element("tbody");
            services.forEach((service) => {
                const row = element("tr");
                row.append(
                    element("td", "", service.service_name || service.name),
                    element("td", "", service.operation_name),
                    element("td", "", service.schema_snapshot),
                    element("td", "", ""),
                    element("td", "", String(service.service_revision)),
                );
                row.children[3].append(statusBadge(service.status));
                const actions = element("td");
                actions.append(button("Open", () => frappe.set_route("Form", "HERO GraphQL Service", service.name)));
                row.append(actions);
                body.append(row);
            });
            table.append(head, body);
            content.replaceChildren(table, emptyNotice(__("Only a separately confirmed service execution can contact a configured HERO endpoint.")));
        } catch (error) {
            errorNotice(content, error);
        }
    };

    window.hero_integration_ui = Object.freeze({
        analyzer,
        createPage,
        ensureStyles,
        graphViewer,
        graphqlServices,
        overview,
        routeOptions,
        setRoute,
    });
})();
