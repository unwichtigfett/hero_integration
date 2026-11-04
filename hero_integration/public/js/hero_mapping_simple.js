// Simple Mapping editor (layout of the owner's "Mapping Editor" sketch, 2026-11-04):
// Query/Mutation, schema snapshot, Map Fields (Source → Target, Create), Active Mappings
// (Activate, Delete, filters) and Mapping Import. The previous editor stays under "Advanced".
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
        for (const item of items) node.append(el("option", {value: item.value, text: item.label, disabled: item.disabled || false}));
        node.value = value;
        return node;
    };
    const labelled = (label, control) => el("label", {class: "hms-control"}, el("span", {text: t(label)}), control);
    const info = (rows) => el("dl", {class: "hms-info"}, ...rows.flatMap(([key, value]) => [el("dt", {text: t(key)}), el("dd", {text: value || "—"})]));

    function mount(page, context = {}, advanced = null) {
        const host = page.main.empty().addClass("hero-integration-ui")[0];
        const state = {data: null, tab: "Query", operation: "", object: null, sourcePath: "", app: "", doctype: "", field: "",
                       targetFields: [], selected: new Set(), filterOperation: "", search: "", filterType: "", filterValue: ""};
        const status = el("p", {class: "hms-status", role: "status", "aria-live": "polite"});
        const say = (message, kind = "") => { status.textContent = message || ""; status.dataset.kind = kind; };
        const root = el("section", {class: "hms"});
        host.append(root);

        async function load() {
            try {
                state.data = await rpc("simple_mapping_state");
                render();
            } catch (error) { say(failure(error), "error"); render(); }
        }

        function header() {
            const tabs = ["Query", "Mutation"].map((name) => el("button", {
                type: "button", class: `btn btn-sm ${state.tab === name ? "btn-primary" : "btn-default"}`, text: t(name),
                onclick: () => { state.tab = name; render(); }}));
            const snapshots = (state.data?.snapshots || []).map((name) => ({value: name, label: name}));
            const snapshot = select(t("choose snapshot"), snapshots, snapshots[0]?.value || "");
            const more = advanced ? el("button", {type: "button", class: "btn btn-link btn-sm hms-advanced", text: t("Advanced"),
                onclick: () => advanced()}) : null;
            return el("header", {class: "hms-header"},
                el("div", {class: "hms-title"}, el("h2", {text: t("HERO Mapping Editor")}), el("div", {class: "hms-tabs"}, tabs), more),
                labelled("GraphQL Schema Snapshot", snapshot));
        }

        const operations = () => state.data?.operations || [];
        const currentOperation = () => operations().find((item) => item.operation === state.operation);
        const rulesFor = (predicate) => (state.data?.rules || []).filter(predicate);

        function sourceCard() {
            const editable = operations().filter((item) => item.editable);
            const operation = select(t("enter operation"), operations().map((item) => ({
                value: item.operation, label: t(item.label) + (item.editable ? "" : ` (${t("fixed")})`), disabled: !item.editable})), state.operation);
            operation.addEventListener("change", () => {
                state.operation = operation.value; state.object = null; state.sourcePath = "";
                const area = currentOperation();
                state.doctype = area?.target_doctype || ""; state.app = ""; state.field = ""; state.targetFields = [];
                render(); loadTargetFields();
            });
            const sources = currentOperation()?.sources || [];
            const objects = [...new Set(sources.map((item) => item.object))].sort();
            const object = select(t("enter object"), objects.map((value) => ({value, label: value || t("Main record")})), state.object ?? "");
            object.disabled = !state.operation;
            object.addEventListener("change", () => { state.object = object.value; state.sourcePath = ""; render(); });
            const fields = sources.filter((item) => state.object === null || item.object === state.object);
            const field = select(t("enter field"), fields.map((item) => ({value: item.path, label: item.field})), state.sourcePath);
            field.disabled = !state.operation;
            field.addEventListener("change", () => {
                state.sourcePath = field.value;
                const picked = sources.find((item) => item.path === field.value);
                if (picked && state.object === null) state.object = picked.object;
                render();
            });
            const picked = sources.find((item) => item.path === state.sourcePath);
            const mapped = picked ? rulesFor((rule) => rule.status === "Active" && rule.operation === state.operation && rule.hero === picked.path) : [];
            return el("section", {class: "hms-card"}, el("h3", {text: t("Source")}),
                el("div", {class: "hms-row"}, labelled("Operation", operation), labelled("Object", object), labelled("Field", field)),
                info([["ID", picked?.path], ["Name", picked?.field], ["Type", picked?.type], ["Node", picked ? (picked.object || t("Main record")) : ""],
                      ["Revision", currentOperation() ? String(currentOperation().revision) : ""],
                      ["Status", picked ? (mapped.length ? `${t("Mapped to")} ${mapped.map((rule) => rule.target).join(", ")}` : t("Not mapped")) : ""]]),
                editable.length ? null : el("p", {class: "text-muted", text: t("No area with configurable field rules.")}));
        }

        async function loadTargetFields() {
            if (!state.doctype) return;
            try {
                const items = [];
                for (let start = 0; start < 1000; start += 50) {
                    const page = await rpc("mapping_target_catalog_page", {doctype: state.doctype, start, page_size: 50});
                    items.push(...page.items);
                    if (!page.has_more) break;
                }
                state.targetFields = items;
                state.app = items[0]?.app_name || "";
                render();
            } catch (error) { say(failure(error), "error"); }
        }

        function targetCard() {
            const app = select(t("enter app"), state.app ? [{value: state.app, label: state.app}] : [], state.app);
            const doctype = select(t("enter DocType"), state.doctype ? [{value: state.doctype, label: state.doctype}] : [], state.doctype);
            app.disabled = doctype.disabled = true;  // an area writes its own record type
            const field = select(t("enter field"), state.targetFields.map((item) => ({
                value: item.path, label: `${item.label || item.path} (${item.path})`, disabled: !item.selectable})), state.field);
            field.disabled = !state.targetFields.length;
            field.addEventListener("change", () => { state.field = field.value; render(); });
            const picked = state.targetFields.find((item) => item.path === state.field);
            const writer = picked ? rulesFor((rule) => rule.status === "Active" && rule.target === `${state.doctype}.${picked.path}`) : [];
            const create = el("button", {type: "button", class: "btn btn-primary hms-create", text: t("Create"),
                disabled: !(state.operation && state.sourcePath && picked?.selectable) || writer.length > 0, onclick: createRule});
            return el("section", {class: "hms-card"}, el("h3", {text: t("Target")}),
                el("div", {class: "hms-row"}, labelled("App", app), labelled("DocType", doctype), labelled("Field", field)),
                info([["ID", picked ? `${state.doctype}.${picked.path}` : ""], ["Name", picked?.label], ["Type", picked?.fieldtype],
                      ["Node", picked ? state.doctype : ""], ["Revision", ""],
                      ["Status", picked ? (writer.length ? `${t("Already written by")} ${writer.map((rule) => rule.hero).join(", ")}` : t(picked.reason || "")) : ""]]),
                el("div", {class: "hms-card-actions"}, create));
        }

        async function createRule() {
            say(t("Creating mapping …"));
            try {
                const result = await rpc("simple_mapping_create", {operation: state.operation, source_path: state.sourcePath,
                    target_doctype: state.doctype, target_field: state.field}, true);
                say(`${t("Mapping is active.")} ${t("Transformation")}: ${t(result.transformation)}`, "success");
                state.sourcePath = ""; state.field = "";
                await load();
            } catch (error) { say(failure(error), "error"); }
        }

        function visibleRows() {
            const rows = state.tab === "Query" ? (state.data?.rules || []) : (state.data?.sends || []);
            const needle = state.search.toLowerCase(), value = state.filterValue.toLowerCase();
            return rows.filter((row) => (!state.filterOperation || row.operation === state.filterOperation)
                && (!needle || `${row.operation_label || row.operation} ${row.hero} ${row.app} ${row.target}`.toLowerCase().includes(needle))
                && (!state.filterType || !value || String(row[state.filterType] ?? "").toLowerCase().includes(value)));
        }

        async function change(kind) {
            const rows = (state.data?.rules || []).filter((row) => state.selected.has(row.definition_id));
            const wanted = rows.filter((row) => kind === "activate" ? row.status === "Inactive" : row.status === "Active");
            if (!wanted.length) { say(t(kind === "activate" ? "Select inactive mappings to activate." : "Select active mappings to delete."), "error"); return; }
            if (kind === "remove" && wanted.some((row) => !row.removable)) { say(t("Identity, name and relation rules cannot be removed here."), "error"); return; }
            const byOperation = new Map();
            wanted.forEach((row) => byOperation.set(row.operation, [...(byOperation.get(row.operation) || []), row.definition_id]));
            say(t(kind === "activate" ? "Activating …" : "Deleting …"));
            try {
                for (const [operation, ids] of byOperation) {
                    await rpc(kind === "activate" ? "simple_mapping_activate" : "simple_mapping_remove",
                              {operation, definition_ids: JSON.stringify(ids)}, true);
                }
                state.selected.clear();
                say(t(kind === "activate" ? "Mappings activated." : "Mappings deleted."), "success");
                await load();
            } catch (error) { say(failure(error), "error"); }
        }

        function listCard() {
            const query = state.tab === "Query";
            const operation = select(t("All"), operations().map((item) => ({value: item.operation, label: t(item.label)})), state.filterOperation);
            operation.addEventListener("change", () => { state.filterOperation = operation.value; render(); });
            const search = el("input", {class: "form-control input-sm", placeholder: t("search mapping"), value: state.search});
            search.addEventListener("input", () => { state.search = search.value; renderTable(); });
            const type = select(t("type"), [["status", "Status"], ["app", "Target App"], ["target", "Target"], ["hero", "HERO"]]
                .map(([value, label]) => ({value, label: t(label)})), state.filterType);
            type.addEventListener("change", () => { state.filterType = type.value; renderTable(); });
            const value = el("input", {class: "form-control input-sm", placeholder: t("value"), value: state.filterValue});
            value.addEventListener("input", () => { state.filterValue = value.value; renderTable(); });
            const actions = query ? el("div", {class: "hms-list-actions"},
                el("button", {type: "button", class: "btn btn-default", text: t("Activate"), onclick: () => change("activate")}),
                el("button", {type: "button", class: "btn btn-default", text: t("Delete"), onclick: () => change("remove")})) : null;
            const table = el("table", {class: "table hms-table"});
            function renderTable() {
                table.replaceChildren(el("thead", {}, el("tr", {}, ...(query ? [el("th", {})] : []),
                    ...["Status", "Operation", "HERO", "Target App", "Target", "Revision"].map((name) => el("th", {text: t(name)})))));
                const body = el("tbody");
                for (const row of visibleRows()) {
                    const box = query ? el("input", {type: "checkbox", "aria-label": t("Select"), checked: state.selected.has(row.definition_id)}) : null;
                    box?.addEventListener("change", () => box.checked ? state.selected.add(row.definition_id) : state.selected.delete(row.definition_id));
                    body.append(el("tr", {}, query ? el("td", {}, box) : null,
                        el("td", {}, el("span", {class: `hms-badge hms-${String(row.status).toLowerCase()}`, text: t(row.status)})),
                        el("td", {text: t(row.operation_label || row.operation)}), el("td", {text: row.hero}),
                        el("td", {text: row.app}), el("td", {text: row.target}), el("td", {text: String(row.revision ?? "")})));
                }
                if (!body.children.length) body.append(el("tr", {}, el("td", {colspan: query ? 7 : 6, class: "text-muted", text: t("No mappings.")})));
                table.append(body);
            }
            renderTable();
            return el("section", {class: "hms-list"},
                el("div", {class: "hms-list-head"}, el("h3", {text: t(query ? "Active Mappings" : "Active Send Mappings")}), actions),
                el("div", {class: "hms-filters"}, labelled("Operation", operation), labelled("Search", search),
                   el("div", {class: "hms-control"}, el("span", {text: t("Filter")}), el("div", {class: "hms-pair"}, type, value))),
                el("div", {class: "hms-table-wrap"}, table),
                query ? null : el("p", {class: "text-muted", text: t("Send mappings (ERPNext → HERO) are created under Advanced.")}));
        }

        function importCard() {
            const file = el("input", {type: "file", accept: ".json,application/json", class: "hms-file"});
            file.addEventListener("change", async () => {
                const picked = file.files?.[0]; file.value = "";
                if (!picked) return;
                try {
                    const payload = await picked.text();
                    await rpc("config_import_preview", {payload_json: payload});
                    frappe.confirm(t("Import and activate this configuration now?"), async () => {
                        try {
                            await rpc("config_import", {payload_json: payload, confirmed: 1}, true);
                            say(t("Configuration imported."), "success"); await load();
                        } catch (error) { say(failure(error), "error"); }
                    });
                } catch (error) { say(failure(error), "error"); }
            });
            return el("section", {class: "hms-import"}, el("h3", {text: t("Mapping Import")}),
                el("label", {class: "btn btn-default btn-sm"}, t("Choose File"), file));
        }

        function render() {
            const query = state.tab === "Query";
            root.replaceChildren(header(), status,
                query ? el("section", {class: "hms-map"}, el("h3", {text: t("Map Fields")}),
                    el("div", {class: "hms-cards"}, sourceCard(), el("div", {class: "hms-arrow", "aria-hidden": "true", text: "→"}), targetCard())) : null,
                listCard(), importCard());
        }

        render();
        load();
        return {destroy() { root.remove(); }};
    }

    window.hero_integration_mapping_simple = Object.freeze({mount});
})();
