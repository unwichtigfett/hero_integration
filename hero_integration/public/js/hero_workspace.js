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
        root.append(element("p", "hero-integration-lead", __("Analyze saved GraphQL operations, types, arguments and relations without contacting HERO.")), content);
        const initial = routeOptions();
        addSnapshotPicker(root, async (snapshotName) => {
            content.replaceChildren();
            if (!snapshotName) return content.append(emptyNotice(__("Create or select a validated snapshot to analyze its saved structure.")));
            try {
                const graph = await request("graph_view", {snapshot_name: snapshotName});
                const details = (node) => {
                    const relations = graph.edges.filter((edge) => edge.source === node.id || edge.target === node.id);
                    const rows = [
                        [__("Kind"), node.kind], [__("GraphQL type"), node.type_ref || node.type_name || "—"],
                        [__("Nullability"), node.nullable === undefined ? "—" : (node.nullable ? __("Nullable") : __("Required"))],
                        [__("Relations"), String(relations.length)],
                    ];
                    const panel = element("div", "hero-integration-detail");
                    panel.append(element("h3", "", node.label), statusBadge(node.kind));
                    if (node.description) panel.append(element("p", "hero-integration-muted", node.description));
                    const table = element("dl", "hero-integration-detail-list");
                    rows.forEach(([key, value]) => { table.append(element("dt", "", key), element("dd", "", value)); });
                    panel.append(table, button("Open in Graph Viewer", () => setRoute("hero-graph-viewer", {snapshot_name: snapshotName, selected_node: node.id}), "btn btn-primary btn-sm"));
                    return panel;
                };
                const categories = ["", "QUERY_OPERATION", "MUTATION_OPERATION", "OBJECT", "INPUT_OBJECT", "INTERFACE", "UNION", "ENUM", "SCALAR", "FIELD", "ARGUMENT", "INPUT_FIELD"];
                const search = element("input", "form-control");
                search.placeholder = __("Search operations, types, fields and arguments");
                const kind = element("select", "form-control");
                categories.forEach((value) => { const option = element("option", "", value ? value.replaceAll("_", " ") : __("All kinds")); option.value = value; kind.append(option); });
                const controls = element("div", "hero-integration-graph-controls"); controls.append(search, kind);
                const list = element("div", "hero-integration-list");
                const detail = element("div", "hero-integration-detail");
                const show = (node) => {
                    const panel = details(node);
                    detail.replaceChildren(...Array.from(panel.childNodes));
                };
                const render = () => {
                    const needle = search.value.trim().toLowerCase();
                    list.replaceChildren();
                    const rows = graph.nodes.filter((node) => (!kind.value || node.kind === kind.value) && (!needle || `${node.label} ${node.type_name || ""} ${node.kind}`.toLowerCase().includes(needle)));
                    rows.forEach((node) => list.append(button(`${node.label} · ${node.kind}${node.type_ref ? ` · ${node.type_ref}` : ""}`, () => show(node))));
                    if (!rows.length) list.append(emptyNotice(__("No stored operation, type or field matches this filter.")));
                };
                search.addEventListener("input", render); kind.addEventListener("change", render); render();
                detail.append(emptyNotice(__("Choose an operation, type or field to inspect its actual saved GraphQL contract.")));
                const summary = element("div", "hero-integration-summary-grid compact");
                const operationCount = graph.nodes.filter((node) => node.kind.endsWith("_OPERATION")).length;
                summary.append(
                    element("article", "hero-integration-card", `${graph.nodes.filter((node) => node.id.startsWith("type:")).length} ${__("types")}`),
                    element("article", "hero-integration-card", `${operationCount} ${__("operations")}`),
                    element("article", "hero-integration-card", shortFingerprint(graph.fingerprint)),
                );
                const layout = element("div", "hero-integration-split"); layout.append(list, detail);
                content.append(summary, controls, layout);
            } catch (error) { errorNotice(content, error); }
        }, initial.snapshot_name);
    };

    const graphViewer = (wrapper) => {
        const {root} = createPage(wrapper, "HERO Graph Viewer");
        const content = element("div", "hero-integration-panel");
        root.append(element("p", "hero-integration-lead", __("Explore the complete validated GraphQL schema stored in Frappe.")), content);
        const options = routeOptions();
        addSnapshotPicker(root, async (snapshotName) => {
            content.replaceChildren();
            if (!snapshotName) return content.append(emptyNotice(__("Choose a saved snapshot to display its graph.")));
            try {
                const graph = await request("graph_view", {snapshot_name: snapshotName});
                const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
                const search = element("input", "form-control"); search.placeholder = __("Search operation, type or field");
                const kind = element("select", "form-control");
                ["", "QUERY_OPERATION", "MUTATION_OPERATION", "OBJECT", "INPUT_OBJECT", "ENUM", "SCALAR", "FIELD", "ARGUMENT", "INPUT_FIELD"].forEach((value) => {
                    const item = element("option", "", value ? value.replaceAll("_", " ") : __("All graph nodes")); item.value = value; kind.append(item);
                });
                const operation = element("select", "form-control");
                ["HeroCustomers", "HeroProjectMatches"].forEach((value) => { const item = element("option", "", value); item.value = value; operation.append(item); });
                operation.value = options.operation || "HeroCustomers";
                const focusCustomer = button("Customer focus", () => { search.value = "Customer"; render(); });
                const zoomOut = button("−", () => { scale = Math.max(.35, scale - .15); applyTransform(); });
                const zoomIn = button("+", () => { scale = Math.min(2.5, scale + .15); applyTransform(); });
                const reset = button("Fit graph", () => { scale = 1; pan = {x: 0, y: 0}; applyTransform(); });
                const controls = element("div", "hero-integration-graph-controls"); controls.append(search, kind, operation, focusCustomer, zoomOut, zoomIn, reset);
                const canvas = element("div", "hero-integration-graph-canvas");
                const inspector = element("aside", "hero-integration-graph-inspector");
                let scale = 1; let pan = {x: 0, y: 0}; let scene;
                const applyTransform = () => { if (scene) scene.setAttribute("transform", `translate(${pan.x} ${pan.y}) scale(${scale})`); };
                const showNode = (node) => {
                    const relations = graph.edges.filter((edge) => edge.source === node.id || edge.target === node.id);
                    inspector.replaceChildren(element("h3", "", node.label), statusBadge(node.kind));
                    if (node.description) inspector.append(element("p", "hero-integration-muted", node.description));
                    inspector.append(element("p", "hero-integration-muted", `${node.type_ref || node.type_name || "—"} · ${relations.length} ${__("relations")}`));
                    const relationList = element("div", "hero-integration-field-list");
                    relations.slice(0, 40).forEach((edge) => {
                        const related = nodeById.get(edge.source === node.id ? edge.target : edge.source);
                        if (related) relationList.append(button(`${edge.kind}: ${related.label}`, () => showNode(related)));
                    });
                    inspector.append(relationList);
                    if (node.kind === "FIELD" && node.type_name) inspector.append(button("Open selected field in Mapping Editor", () => setRoute("hero-adapter-tools", {
                        snapshot_name: snapshotName, operation: operation.value, source_path: node.label,
                    }), "btn btn-primary btn-sm"));
                };
                const render = () => {
                    const needle = search.value.trim().toLowerCase();
                    const visible = graph.nodes.filter((node) => (!kind.value || node.kind === kind.value) && (!needle || `${node.label} ${node.type_name || ""} ${node.kind}`.toLowerCase().includes(needle)));
                    const ids = new Set(visible.map((node) => node.id));
                    const width = Math.max(960, Math.ceil(Math.sqrt(Math.max(visible.length, 1))) * 170);
                    const columns = Math.max(6, Math.floor(width / 160));
                    const height = Math.max(360, Math.ceil(visible.length / columns) * 104 + 50);
                    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
                    svg.setAttribute("viewBox", `0 0 ${width} ${height}`); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", __("Interactive stored GraphQL schema graph"));
                    scene = document.createElementNS(svg.namespaceURI, "g"); svg.append(scene); applyTransform();
                    const position = new Map(visible.map((node, index) => [node.id, {x: 24 + (index % columns) * 156, y: 24 + Math.floor(index / columns) * 104}]));
                    graph.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target)).forEach((edge) => {
                        const source = position.get(edge.source); const target = position.get(edge.target);
                        const line = document.createElementNS(svg.namespaceURI, "line"); line.setAttribute("x1", source.x + 58); line.setAttribute("y1", source.y + 31); line.setAttribute("x2", target.x + 58); line.setAttribute("y2", target.y + 31); line.setAttribute("class", "hero-integration-graph-edge"); scene.append(line);
                    });
                    visible.forEach((node) => {
                        const point = position.get(node.id); const group = document.createElementNS(svg.namespaceURI, "g");
                        group.setAttribute("class", `hero-integration-graph-node kind-${node.kind.toLowerCase()}`); group.setAttribute("tabindex", "0"); group.setAttribute("role", "button"); group.setAttribute("aria-label", `${node.label} ${node.kind}`);
                        const rect = document.createElementNS(svg.namespaceURI, "rect"); rect.setAttribute("x", point.x); rect.setAttribute("y", point.y); rect.setAttribute("width", "116"); rect.setAttribute("height", "64"); rect.setAttribute("rx", "7");
                        const name = document.createElementNS(svg.namespaceURI, "text"); name.setAttribute("x", point.x + 8); name.setAttribute("y", point.y + 27); name.textContent = node.label.slice(0, 18);
                        const caption = document.createElementNS(svg.namespaceURI, "text"); caption.setAttribute("x", point.x + 8); caption.setAttribute("y", point.y + 47); caption.setAttribute("class", "hero-integration-graph-kind"); caption.textContent = node.type_ref || node.kind;
                        group.append(rect, name, caption); group.addEventListener("click", () => showNode(node)); group.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); showNode(node); } }); scene.append(group);
                    });
                    let drag; svg.addEventListener("pointerdown", (event) => { if (event.target.closest(".hero-integration-graph-node")) return; drag = {x: event.clientX, y: event.clientY, pan: {...pan}}; svg.setPointerCapture(event.pointerId); });
                    svg.addEventListener("pointermove", (event) => { if (!drag) return; pan = {x: drag.pan.x + event.clientX - drag.x, y: drag.pan.y + event.clientY - drag.y}; applyTransform(); });
                    svg.addEventListener("pointerup", () => { drag = null; }); svg.addEventListener("wheel", (event) => { event.preventDefault(); scale = Math.max(.35, Math.min(2.5, scale * (event.deltaY > 0 ? .9 : 1.1))); applyTransform(); }, {passive: false});
                    canvas.replaceChildren(svg, element("p", "hero-integration-muted", `${visible.length} / ${graph.nodes.length} ${__("stored graph nodes displayed")}`));
                    const requested = visible.find((node) => node.id === options.selected_node); if (requested) showNode(requested);
                };
                search.addEventListener("input", render); kind.addEventListener("change", render); render();
                const builtin = ["Int", "String", "ID", "Boolean", "Float"];
                const scalarRows = graph.nodes.filter((node) => node.kind === "SCALAR");
                const scalarList = element("div", "hero-integration-field-list");
                scalarRows.filter((node) => builtin.includes(node.label)).forEach((node) => scalarList.append(button(node.label, () => showNode(node))));
                scalarRows.filter((node) => !builtin.includes(node.label)).forEach((node) => scalarList.append(button(node.label, () => showNode(node))));
                inspector.append(element("h3", "", __("Built-in and custom scalars")), scalarList, emptyNotice(__("Select a graph node to inspect fields, arguments and navigable relations.")));
                const layout = element("div", "hero-integration-graph-layout"); layout.append(canvas, inspector);
                content.append(controls, layout, element("p", "hero-integration-fingerprint", `${__("Schema fingerprint")}: ${graph.fingerprint}`));
            } catch (error) { errorNotice(content, error); }
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
            ["Service", "Operation", "Snapshot", "Status", "Revision", "Validation", ""].forEach((label) => headRow.append(element("th", "", __(label))));
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
                    element("td", "hero-integration-muted", service.validation_summary || __("Not validated")),
                );
                row.children[3].append(statusBadge(service.status));
                const actions = element("td");
                actions.append(button("Open", async () => {
                    try {
                        const detail = await request("graphql_service_detail", {service_name: service.name});
                        const panel = element("article", "hero-integration-card");
                        panel.append(
                            element("h3", "", detail.service_name || detail.name), statusBadge(detail.status),
                            element("p", "hero-integration-muted", `${__("Revision")}: ${detail.service_revision} · ${detail.schema_snapshot}`),
                            element("pre", "hero-integration-service-query", detail.query_document || ""),
                            element("p", "hero-integration-muted", detail.validation_summary || __("Not validated")),
                            button("Edit definition", () => frappe.set_route("Form", "HERO GraphQL Service", detail.name)),
                        );
                        content.querySelector(".hero-integration-service-detail")?.remove();
                        const detailHost = element("div", "hero-integration-service-detail"); detailHost.append(panel); content.append(detailHost);
                    } catch (error) { frappe.msgprint(error?.message || __("Saved service could not be loaded.")); }
                }));
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
