// Mapping page: mappings are written as a JSON file
// and loaded here; the page only shows them. Nothing is edited on the website.
(function () {
    "use strict";
    const t = (value) => window.hero_integration_i18n?.text(value) ?? (typeof __ === "function" ? __(value) : String(value));
    const rpc = async (method, args = {}, write = false) => {
        const response = await frappe.call({method: `hero_integration.api.${method}`, type: write ? "POST" : "GET", args});
        if (response?.exc) throw response;
        return response.message;
    };
    const failure = (error) => {
        try {
            const messages = JSON.parse(error?._server_messages || error?.responseJSON?._server_messages || "[]");
            const first = typeof messages[0] === "string" ? JSON.parse(messages[0]) : messages[0];
            if (first?.message) return String(first.message).replace(/<[^>]+>/g, "");
        } catch (_) { /* fall through */ }
        return error?.message || t("The request failed.");
    };
    const el = (tag, attrs = {}, ...children) => {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (key === "class") node.className = value;
            else if (key === "text") node.textContent = value;
            else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
            else if (value !== false && value !== null && value !== undefined) node.setAttribute(key, value === true ? "" : value);
        }
        children.flat().forEach((child) => child && node.append(child));
        return node;
    };
    const select = (placeholder, items, value = "") => {
        const node = el("select", {class: "form-control input-sm"}, el("option", {value: "", text: placeholder}));
        for (const item of items) node.append(el("option", {value: item.value, text: item.label}));
        node.value = value;
        return node;
    };
    const labelled = (label, control) => el("label", {class: "hms-control"}, el("span", {text: t(label)}), control);
    const COLUMNS = [["status", "Status"], ["operation_label", "Operation"], ["hero", "HERO"], ["app", "Target App"],
                     ["target", "Target"], ["revision", "Revision"]];

    const AREA_LABELS = {HeroCustomers: "Customers", HeroProjectMatches: "Projects", HeroCalendarEvents: "Events",
                         HeroTasks: "Tasks", HeroEmployees: "Employees", HeroProducts: "Products", HeroServices: "Services",
                         HeroDocuments: "Documents", HeroPictures: "Pictures"};
    function previewText(preview) {
        const areas = (preview?.read_services || []).map((row) => `${t(AREA_LABELS[row.target_operation] || row.target_operation)}: ${t(row.state || "")}`);
        const summary = areas.length ? `${t("Areas in the file")}: ${areas.join(", ")}` : t("The file contains no mapping areas.");
        return summary + (preview.replace_mappings
            ? ` ${t("Existing mappings in these areas will be replaced.")} ${preview.replacement?.field_mappings ?? 0} ${t("field mappings")}, ${preview.replacement?.graphql_mappings ?? 0} ${t("GraphQL mappings")}.` : "")
            + ` ${t("No HERO operation will be executed.")}`;
    }

    function jsonFormat(payload) {
        let value;
        try { value = JSON.parse(payload); } catch (_) { throw new Error(t("The file is not valid JSON.")); }
        if (["hero-integration-config-v1", "hero-integration-config-v2"].includes(value?.format_version)) return "configuration";
        if (value?.format_version === "hero-integration-mappings-v1") {
            if (["settings", "schema_snapshot", "mutation_mappings", "mutation_contracts", "contracts"].some(key => Object.hasOwn(value, key))) return "configuration";
            return "query";
        }
        if (value?.format_version === "hero-personal-mapping-portable-v2"
            && value.direction === "FRAPPE_TO_MUTATION") return "mutation";
        return "unsupported";
    }
    function expectedStates(preview) {
        if (!Array.isArray(preview?.items)) throw new Error(t("Invalid directional import preview."));
        return preview.items.map(item => {
            const expected = item.expected;
            if (!item.definition_id || !expected || !["generation", "draft_revision", "active_revision", "reconciliation_revision"]
                .every(key => Number.isInteger(expected[key]) && expected[key] >= 0)) throw new Error(t("Invalid directional import preview."));
            return {definition_id: item.definition_id, generation: expected.generation, draft_revision: expected.draft_revision,
                active_revision: expected.active_revision, reconciliation_revision: expected.reconciliation_revision};
        });
    }
    function mutationPreviewText(preview) {
        const counts = {NEW: 0, REPLACE: 0, UNCHANGED: 0, REMOVE: 0};
        for (const item of preview.items || []) if (Object.hasOwn(counts, item.state)) counts[item.state]++;
        return `${t("Mutation mappings")}: ${t("New")} ${counts.NEW}, ${t("Replace")} ${counts.REPLACE}, ` +
            `${t("Unchanged")} ${counts.UNCHANGED}, ${t("Remove")} ${counts.REMOVE}. ${t("No HERO operation will be executed.")}`;
    }

    function mount(page) {
        const host = page.main.empty().addClass("hero-integration-ui")[0];
        const state = {data: null, tab: "Query", operation: "", search: "", filterType: "", filterValue: ""};
        const status = el("p", {class: "hms-status", role: "status", "aria-live": "polite"});
        const say = (message, kind = "") => { status.textContent = message || ""; status.dataset.kind = kind; };
        const root = el("section", {class: "hms"});
        host.append(root);

        async function load() {
            try { const data = await rpc("simple_mapping_state"); if (!disposed) state.data = data; }
            catch (error) { if (!disposed) say(failure(error), "error"); }
            if (!disposed) render();
        }

        function rows() {
            const all = state.tab === "Query" ? (state.data?.rules || []) : (state.data?.sends || []);
            const needle = state.search.toLowerCase(), value = state.filterValue.toLowerCase();
            return all.filter((row) => (!state.operation || row.operation === state.operation)
                && (!needle || `${row.operation_label || row.operation} ${row.hero} ${row.app} ${row.target}`.toLowerCase().includes(needle))
                && (!state.filterType || !value || String(row[state.filterType] ?? "").toLowerCase().includes(value)));
        }

        const manager = Boolean(window.hero_integration_ui?.isSystemManager);
        let disposed = false, busy = false, snapshots = [], snapshotName = "", snapshotRequest = 0;
        function setBusy(value) {
            busy = value; root.setAttribute("aria-busy", String(value));
            root.querySelectorAll("button,input,select").forEach(node => { node.disabled = value; });
        }
        function context(tab = state.tab) {
            const snapshot = snapshots.find(row => row.name === snapshotName);
            if (!snapshot?.source_instance) throw new Error(t("Choose a validated schema snapshot first."));
            return {snapshot_name: snapshot.name, direction: tab === "Mutation" ? "FRAPPE_TO_MUTATION" : "QUERY_TO_FRAPPE"};
        }
        async function loadSnapshots() {
            const request = ++snapshotRequest;
            try {
                const all = [];
                for (let start = 0; start <= 10000; start += 100) {
                    const page = await rpc("schema_snapshot_list", {start, page_size: 100});
                    if (disposed || request !== snapshotRequest) return;
                    all.push(...page);
                    if (page.length < 100) break;
                    if (start === 10000) throw new Error(t("The snapshot list could not be loaded completely. Retry before choosing a snapshot."));
                }
                snapshots = all.filter(row => row.analysis_status === "Validated" && row.configured_source === true)
                    .sort((a, b) => String(b.creation || b.modified || "").localeCompare(String(a.creation || a.modified || "")));
                if (!snapshots.some(row => row.name === snapshotName)) snapshotName = snapshots[0]?.name || "";
                render();
            } catch (error) { if (!disposed) say(failure(error), "error"); }
        }
        function columns() {
            return COLUMNS.map(([key, label]) => [key, state.tab === "Mutation"
                ? ({hero: "Target HERO", app: "Source App", target: "Source Frappe"}[key] || label) : label]);
        }
        async function importFile(picked) {
            if (!picked || busy || disposed) return;
            const tab = state.tab;
            setBusy(true); say(t("Checking mapping file …"));
            try {
                if (picked.size > 5000000) throw new Error(t("The mapping file exceeds the permitted size."));
                const payload = await picked.text();
                if (disposed) return;
                const format = jsonFormat(payload);
                const mutation = tab === "Mutation";
                if (format === "configuration") throw new Error(t("Configuration packages belong in Settings. Import a mapping JSON file here."));
                if (mutation && format !== "mutation") throw new Error(t("Import a Mutation mapping JSON file in this tab. Query mapping files belong in Query."));
                if (!mutation && format !== "query") throw new Error(t("Import a Query mapping JSON file in this tab. Mutation mapping files belong in Mutation."));
                const args = {payload_json: payload, ...context(tab)};
                const preview = await rpc("simple_mapping_import_preview", args, true);
                if (disposed) return;
                if (preview.can_apply !== true) {
                    const blockers = (preview.blockers || []).map(item => typeof item === "string" ? t(item) : String(item.message || item.reason || JSON.stringify(item)));
                    say([t(mutation ? "The Mutation mappings cannot be activated." : "The Query mappings cannot be activated."), ...blockers].join(" "), "error"); setBusy(false); return;
                }
                if (typeof preview.preview_fingerprint !== "string" || !preview.preview_fingerprint) throw new Error(t("Invalid directional import preview."));
                const expected = mutation ? expectedStates(preview) : [];
                const summary = mutation ? mutationPreviewText(preview) : previewText(preview);
                say(summary);
                const confirmation = mutation ? t("Import and activate these Mutation mappings? No HERO operation will be executed.")
                    : t("Import and activate these Query mappings? No HERO operation will be executed.");
                let consumed = false;
                frappe.confirm(`${frappe.utils.escape_html(summary)}<br><br>${frappe.utils.escape_html(confirmation)}`, async () => {
                    if (disposed || consumed) return;
                    consumed = true;
                    say(t("Loading mappings …"));
                    try {
                        const result = await rpc("simple_mapping_import_apply",
                            {...args, expected_states_json: JSON.stringify(expected), preview_fingerprint: preview.preview_fingerprint, confirmed: 1}, true);
                        if (disposed) return;
                        if (result.activation !== true || result.execution !== false) throw new Error(t("Unexpected mapping import result."));
                        await load();
                        say(mutation ? t("Mutation mappings imported and activated. No HERO operation was executed.")
                            : t("Query mappings imported and activated. No HERO operation was executed."), "success");
                    } catch (error) { if (!disposed) say(`${t("Import failed.")} ${failure(error)}`, "error"); }
                    finally { if (!disposed) setBusy(false); }
                }, () => { consumed = true; if (!disposed) { setBusy(false); say(t("Import cancelled.")); } });
            } catch (error) { if (!disposed) { say(`${t("The file cannot be loaded.")} ${failure(error)}`, "error"); setBusy(false); } }
        }
        function loadButton() {
            const file = el("input", {type: "file", accept: ".json,application/json", class: "hms-file", "aria-label": t("Import mapping JSON")});
            file.addEventListener("change", () => { const picked = file.files?.[0]; file.value = ""; importFile(picked); });
            return el("div", {class: "hms-import"}, el("button", {type: "button", class: "btn btn-primary", text: t("Import JSON"),
                "data-mapping-import": "", onclick: () => file.click()}), file);
        }
        async function exportFile() {
            if (busy || disposed) return;
            setBusy(true); say(t("Exporting mapping JSON…"));
            try {
                const selected = context();
                const result = await rpc("simple_mapping_export", selected);
                if (disposed) return;
                if (typeof result?.content !== "string" || typeof result?.filename !== "string") throw new Error(t("Invalid mapping export response."));
                if (jsonFormat(result.content) !== (selected.direction === "FRAPPE_TO_MUTATION" ? "mutation" : "query")) throw new Error(t("Invalid mapping export response."));
                const url = URL.createObjectURL(new Blob([result.content], {type: "application/json"}));
                const link = el("a", {href: url, download: result.filename});
                document.body.append(link); link.click(); link.remove();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
                say(t("Mapping JSON exported."), "success");
            } catch (error) { if (!disposed) say(failure(error), "error"); }
            finally { if (!disposed) setBusy(false); }
        }

        function render() {
            const tabs = ["Query", "Mutation"].map((name) => el("button", {
                type: "button", class: `btn btn-sm ${state.tab === name ? "btn-primary" : "btn-default"}`, text: t(name),
                onclick: () => { if (busy) return; state.tab = name; state.operation = ""; render();
                    if (manager && !snapshots.length) loadSnapshots(); }}));
            const operations = state.tab === "Query" ? (state.data?.operations || []).map(item => ({value: item.operation, label: t(item.label)}))
                : [...new Map((state.data?.sends || []).map(row => [row.operation, {value: row.operation, label: row.operation_label || row.operation}])).values()];
            const operation = select(t("All"), operations, state.operation);
            operation.addEventListener("change", () => { state.operation = operation.value; renderTable(); });
            const search = el("input", {class: "form-control input-sm", placeholder: t("search mapping"), value: state.search});
            search.addEventListener("input", () => { state.search = search.value; renderTable(); });
            const type = select(t("type"), columns().filter(([key]) => key !== "revision").map(([value, label]) => ({value, label: t(label)})), state.filterType);
            type.addEventListener("change", () => { state.filterType = type.value; renderTable(); });
            const value = el("input", {class: "form-control input-sm", placeholder: t("value"), value: state.filterValue});
            value.addEventListener("input", () => { state.filterValue = value.value; renderTable(); });
            const table = el("table", {class: "table hms-table"});
            const count = el("span", {class: "text-muted"});
            function renderTable() {
                const visible = rows();
                count.textContent = `${visible.length} ${t("mappings")}`;
                const body = el("tbody");
                for (const row of visible) {
                    body.append(el("tr", {},
                        el("td", {}, el("span", {class: `hms-badge hms-${String(row.status).toLowerCase()}`, text: t(row.status)})),
                        el("td", {text: t(row.operation_label || row.operation)}), el("td", {text: row.hero}),
                        el("td", {text: row.app}), el("td", {text: row.target}), el("td", {text: String(row.revision ?? "")})));
                }
                if (!visible.length) body.append(el("tr", {}, el("td", {colspan: COLUMNS.length, class: "text-muted", text: t("No mappings.")})));
                table.replaceChildren(el("thead", {}, el("tr", {}, ...columns().map(([, label]) => el("th", {text: t(label)})))), body);
            }
            renderTable();
            const actions = el("div", {class: "hms-load"});
            if (manager) {
                actions.append(loadButton(), el("button", {type: "button", class: "btn btn-default", text: t("Export JSON"),
                    "data-mapping-export": "", onclick: exportFile}));
                const snapshot = select(t("Choose a validated snapshot"), snapshots.map(row => ({value: row.name, label: row.display_name || row.name})), snapshotName);
                snapshot.setAttribute("data-mapping-snapshot", "");
                snapshot.addEventListener("change", () => { snapshotName = snapshot.value; });
                actions.append(labelled("Schema snapshot", snapshot));
            }
            root.replaceChildren(
                el("header", {class: "hms-header"}, el("div", {class: "hms-title"},
                    el("h2", {text: t("HERO Mappings")}), el("div", {class: "hms-tabs"}, tabs), actions)),
                status,
                el("section", {class: "hms-list"},
                    el("div", {class: "hms-list-head"}, el("h3", {text: t(state.tab === "Query" ? "Active Mappings" : "Active Send Mappings")}), count),
                    el("div", {class: "hms-filters"}, labelled("Operation", operation), labelled("Search", search),
                       el("div", {class: "hms-control"}, el("span", {text: t("Filter")}), el("div", {class: "hms-pair"}, type, value))),
                    el("div", {class: "hms-table-wrap"}, table)),
                el("p", {class: "text-muted hms-note", text: t(state.tab === "Query"
                    ? "Query JSON contains mappings for the selected snapshot. Edit mappings in the JSON file."
                    : "Mutation JSON contains mappings for the selected snapshot. Import validates and activates mappings; it does not execute HERO operations.")}));
            setBusy(busy);
        }

        render();
        load();
        if (manager) loadSnapshots();
        return {destroy() { disposed = true; snapshotRequest++; root.remove(); }};
    }

    window.hero_integration_mapping_simple = Object.freeze({mount, previewText, jsonFormat, expectedStates, mutationPreviewText});
})();
