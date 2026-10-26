(() => {
    "use strict";
    if (window.hero_integration_mapping_editor) return;
    const text = (value) => window.hero_integration_i18n?.text(value) ?? String(value);
    // Scoped to mapping content: revive original integer tokens from the server's
    // canonical JSON text, never from a Number that HTTP JSON already rounded.
    const parseContent = (source) => JSON.parse(source, (_key, value, context) => {
        if (typeof value !== "number" || Number.isSafeInteger(value)) return value;
        if (!context?.source) {
            if (Number.isInteger(value) || !Number.isFinite(value)) throw new Error("This browser cannot preserve mapping JSON numbers.");
            return value;
        }
        if (!/^-?(0|[1-9][0-9]*)$/.test(context.source)) return value;
        if (typeof JSON.rawJSON !== "function") throw new Error("This browser cannot preserve mapping JSON numbers.");
        return JSON.rawJSON(context.source);
    });
    const clone = (value) => parseContent(JSON.stringify(value));
    const draftValue = (draft) => {
        if (!draft) return draft;
        const value = clone(draft);
        if (typeof draft.content_json === "string") value.content = parseContent(draft.content_json);
        return value;
    };
    const verifiedActiveRevision = (draft) => {
        if (typeof draft?.reconciliation_json !== "string") return null;
        try {
            const value = JSON.parse(draft.reconciliation_json);
            return Number.isInteger(value?.active_revision) && value.active_revision > 0 ? value.active_revision : null;
        } catch (_) { return null; }
    };
    const stable = (value) => JSON.stringify(value, (_, item) => item && typeof item === "object" && !JSON.isRawJSON?.(item) && !Array.isArray(item)
        ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);
    const ownRule = (value, id) => Object.hasOwn(value.field_rules || {}, id) ? value.field_rules[id] : null;
    const object = (value) => value && typeof value === "object" && !Array.isArray(value);
    class DraftState {
        constructor(draft) { this.accept(draft); }
        accept(draft) { this.saved = draftValue(draft); this.verifiedActiveRevision = verifiedActiveRevision(this.saved); this.content = clone(this.saved.content); this.referenceOrder = [...(this.content.mapping_definition_ids || [])]; this.undo = []; this.reconciliation = null; this.blocked = false; }
        get dirty() { return stable(this.content) !== stable(this.saved.content); }
        invalidateReconciliation() { this.reconciliation = null; }
        set(path, value) {
            this.invalidateReconciliation();
            let target = this.content;
            path.slice(0, -1).forEach((key) => { if (!Object.hasOwn(target, key) || !object(target[key])) Object.defineProperty(target, key, {value: {}, enumerable: true, configurable: true, writable: true}); target = target[key]; });
            Object.defineProperty(target, path.at(-1), {value, enumerable: true, configurable: true, writable: true});
        }
        remove(index) {
            this.invalidateReconciliation();
            if (Array.isArray(this.content.mapping_definition_ids)) {
                this.undo.push({kind: "reference", index, value: this.content.mapping_definition_ids[index]});
                this.content.mapping_definition_ids.splice(index, 1);
            } else {
                const removed = {};
                for (const key of ["source", "target"]) if (Object.hasOwn(this.content, key)) removed[key] = clone(this.content[key]);
                this.undo.push({kind: "single", value: removed});
                delete this.content.source; delete this.content.target;
            }
        }
        restoreReference(removed) {
            const ids = this.content.mapping_definition_ids;
            if (ids.includes(removed.value)) return;
            const rank = this.referenceOrder.indexOf(removed.value);
            const next = ids.findIndex((id) => this.referenceOrder.indexOf(id) > rank);
            ids.splice(next >= 0 ? next : ids.length, 0, removed.value);
        }
        addReference(id) {
            const ids = this.content.mapping_definition_ids;
            if (ids.includes(id)) return;
            this.invalidateReconciliation();
            const index = this.undo.findLastIndex((entry) => entry.kind === "reference" && entry.value === id);
            if (index >= 0) this.restoreReference(this.undo.splice(index, 1)[0]);
            else { this.referenceOrder.push(id); ids.push(id); }
        }
        restore() {
            const removed = this.undo.pop();
            if (!removed) return;
            this.invalidateReconciliation();
            if (removed.kind === "reference") this.restoreReference(removed);
            else Object.assign(this.content, removed.value);
        }
        applyReconciliation(contentJson, reconciliationJson, activeRevision, reconciliationRevision) {
            if (typeof reconciliationJson !== "string" || !Number.isInteger(activeRevision) || activeRevision < 1
                || !Number.isInteger(reconciliationRevision) || reconciliationRevision < 0) {
                throw new Error("Invalid reconciliation result.");
            }
            this.content = parseContent(contentJson);
            this.reconciliation = {json: reconciliationJson, activeRevision, reconciliationRevision};
            this.blocked = false;
        }
        saveArgs() {
            const d = this.saved;
            return {definition_id: d.definition_id, expected_generation: d.generation,
                expected_draft_revision: d.content_revision,
                expected_active_revision: this.reconciliation?.activeRevision ?? this.verifiedActiveRevision ?? d.base_active_revision,
                expected_reconciliation_revision: this.reconciliation?.reconciliationRevision ?? (d.reconciliation_revision || 0),
                reconciliation_json: this.reconciliation?.json || "",
                mapping_json: JSON.stringify(this.content)};
        }
        matches(current, submitted) {
            const value = clone(current), prior = this.saved.content;
            for (const id of submitted.mapping_definition_ids || []) {
                if (!Object.hasOwn(prior.field_rule_bases || {}, id)) {
                    if (!Object.hasOwn(submitted.field_rule_bases || {}, id) && value.field_rule_bases) delete value.field_rule_bases[id];
                    if (!Object.hasOwn(submitted.field_rules || {}, id) && value.field_rules) delete value.field_rules[id];
                }
            }
            for (const key of ["field_rules", "field_rule_bases"]) if (!Object.hasOwn(submitted, key) && value[key] && !Object.keys(value[key]).length) delete value[key];
            return stable(value) === stable(submitted);
        }
        resolve(current, submitted) {
            current = draftValue(current);
            const prior = this.saved;
            if (current?.state === "OPEN" && current.generation === prior.generation &&
                current.base_active_revision === prior.base_active_revision &&
                [prior.content_revision, prior.content_revision + 1].includes(current.content_revision) &&
                this.matches(current.content, submitted)) { this.accept(current); return true; }
            this.blocked = stable(current) !== stable(this.saved); return false;
        }
    }
    const rpc = async (method, args = {}, write = false) => {
        const response = await frappe.call({method: `hero_integration.api.${method}`, type: write ? "POST" : "GET", args});
        if (response?.exc) throw response;
        return response.message;
    };
    async function readActiveRule(id, list = (args) => rpc("frontend_mapping_list", args)) {
        const search = Array.from(id).slice(0, 120).join("");
        for (let start = 0; start <= 10000; start += 100) {
            const result = await list({search, start, page_size: 100});
            const found = result.frappe_mappings.find((rule) => rule.definition_id === id);
            if (found) return found;
            if (start + 100 >= result.pagination.frappe_total) return null;
        }
        // The API cannot page further: this is incomplete, never a not-found result.
        throw new Error("Active rule search exceeds the readable page range.");
    }
    const denied = (error) => [401, 403].includes(error?.status) || /PermissionError|SessionExpired/.test(error?.responseJSON?.exc_type || error?.exc_type || "");
    const errorText = (error) => denied(error) ? text("You do not have permission to view or change this mapping.")
        : text("The request failed. Your changes have been kept.");
    let current;
    function mount(page, context = {}) {
        current?.destroy();
        const host = page.main.empty().addClass("hero-integration-ui");
        const root = $("<section class='hero-mapping-core'></section>").appendTo(host);
        const shell = host.closest(".admin-workspace").addClass("hero-mapping-shell");
        const admin = Boolean(window.hero_integration_ui?.isSystemManager);
        let disposed = false, epoch = 0, busy = false, model = null, active = null, mode = "active", selected = 0;
        let search = String(context.mapping_search || ""), start = Number(context.mapping_start || 0), listing;
        let revisionsStart = 0, returnFocus = null, fieldFilter = "", detailRequest = 0;
        const ruleCache = new Map();
        const button = (label, fn, primary = false) => $("<button type='button'></button>")
            .addClass(primary ? "btn btn-primary btn-sm" : "btn btn-default btn-sm").text(text(label)).on("click", fn);
        const status = $("<p role='status' aria-live='polite'></p>");
        const toolbar = $("<div class='hero-mapping-actions'></div>");
        const body = $("<div class='hero-mapping-body'></div>");
        root.append($("<h2 tabindex='-1'></h2>").text(text("Mapping")), status, toolbar, body);
        const say = (value) => status.text(text(value));
        const focusHeading = () => root.find("h2").trigger("focus");
        const locked = () => busy || disposed;
        const mutationMapping = (value = {}) => [value, active || {}].some((candidate) =>
            candidate.operation_kind === "MUTATION" || candidate.direction === "FRAPPE_TO_MUTATION");
        const setBusy = (value) => { busy = value; root.attr("aria-busy", String(value)); root.find("button,input,select,textarea").prop("disabled", value); };
        const field = (label, value, change, parent = body) => {
            const input = $("<input class='form-control'>").val(value ?? "").prop("disabled", busy);
            $("<label class='hero-mapping-input'></label>").append($("<span></span>").text(text(label)), input).appendTo(parent);
            input.on("input", () => { change(input.val()); updateDirty(); }); return input;
        };
        const safeJson = (value) => $("<pre class='hero-mapping-preview'></pre>").text(JSON.stringify(value, null, 2));
        function modal(title, description, actions, cancel = () => {}) {
            if (root.find("dialog").length) return;
            const opener = document.activeElement;
            const dialog = $("<dialog class='hero-mapping-dialog'></dialog>");
            const heading = $("<h3></h3>").text(text(title)).attr("id", "hero-mapping-dialog-title");
            dialog.attr("aria-labelledby", "hero-mapping-dialog-title").append(heading, $("<p></p>").text(text(description)));
            const close = () => { dialog[0].close(); dialog.remove(); if (opener?.isConnected) opener.focus(); };
            actions.forEach(([label, action, primary]) => dialog.append(button(label, async () => {
                close(); await action();
            }, primary)));
            dialog.on("cancel", () => { cancel(); setTimeout(() => dialog.remove(), 0); });
            root.append(dialog); dialog[0].showModal(); dialog.find("button").last().trigger("focus");
        }
        function leave(action, cancel = () => {}) {
            if (locked()) return cancel();
            if (!model?.dirty) return action();
            modal("Unsaved changes", "Save your personal draft before leaving, discard local changes, or continue editing.", [
                ["Save and leave", async () => { if (await save()) action(); else cancel(); }, true],
                ["Discard changes", () => { model.content = clone(model.saved.content); model.undo = []; action(); }],
                ["Continue editing", cancel],
            ], cancel);
        }
        function updateDirty() {
            root.find("[data-draft-state]").text(text(model?.dirty ? "Local changes — not saved" : "Saved personal draft"));
            root.trigger("hero-mapping-content-change");
        }
        async function loadList() {
            const request = ++epoch; ruleCache.clear(); active = null; model = null; mode = "active";
            root.off("hero-mapping-content-change"); toolbar.empty(); body.empty(); say("Loading active mappings…");
            const filter = field("Search active definitions and rules", search, (value) => { search = value; }, toolbar).attr("maxlength", 120);
            const apply = () => { search = filter.val(); start = 0; loadList(); };
            filter.on("keydown", (event) => { if (event.key === "Enter") apply(); });
            toolbar.append(button("Search", apply));
            try {
                const result = await rpc("frontend_mapping_list", {search, start, page_size: 20});
                if (disposed || request !== epoch) return;
                listing = result; renderList(); say("");
                if (returnFocus) { root.find("[data-mapping-name]").filter((_, el) => el.dataset.mappingName === returnFocus).first().trigger("focus"); returnFocus = null; }
            } catch (error) { if (request === epoch && !disposed) { say(errorText(error)); body.append(button("Retry", loadList)); } }
        }
        function renderList() {
            body.empty();
            const p = listing.pagination;
            const definitions = listing.graphql_mappings;
            const groups = new Map();
            definitions.forEach((item) => { const key = `${item.operation_kind} ${item.operation_name}`; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(item); });
            body.append($("<h3></h3>").text(text("HERO operations and mapping definitions")),
                $("<p class='text-muted'></p>").text(`${text("Definitions on this page")}: ${definitions.length} / ${p.graphql_total}`));
            if (!definitions.length) body.append($("<p></p>").text(text(search ? "No active definitions match your search." : "No active mapping definitions.")));
            groups.forEach((items, label) => {
                const group = $("<details open class='hero-mapping-operation'></details>").append($("<summary></summary>").text(label));
                items.forEach((item) => group.append(button(`${item.name} · ${text("Active")} · ${text("Revision")} ${item.revision}`, () => open(item, "graphql"))
                    .attr("data-mapping-name", item.name)));
                body.append(group);
            });
            body.append($("<h3></h3>").text(text("Active field rules")), $("<p class='text-muted'></p>").text(`${text("Rules on this page")}: ${listing.frappe_mappings.length} / ${p.frappe_total}`));
            listing.frappe_mappings.forEach((item) => body.append(button(`${item.operation} · ${item.source_path} → ${item.target_doctype}.${item.target_field}`, () => open(item, "frappe"))));
            if (!listing.frappe_mappings.length) body.append($("<p></p>").text(text(search ? "No active field rules match your search." : "No active field rules.")));
            const pager = $("<div class='hero-mapping-actions'></div>");
            pager.append(button("Previous page", () => { start = Math.max(0, start - 20); loadList(); }).prop("disabled", start === 0),
                $("<span></span>").text(`${text("Page")} ${Math.floor(start / 20) + 1}`),
                button("Next page", () => { start += 20; loadList(); }).prop("disabled", start + 20 >= Math.max(p.graphql_total, p.frappe_total) || start >= 10000));
            body.append(pager);
        }
        async function open(item, kind) {
            if (locked()) return;
            const request = ++epoch; ruleCache.clear(); body.empty(); toolbar.empty(); say("Loading active definition…");
            toolbar.append(button("Back to mappings", () => leave(loadList)));
            try {
                const detail = await rpc("frontend_mapping_detail", {mapping_type: kind, name: item.name});
                if (disposed || request !== epoch) return;
                active = {...detail, mapping_type: kind}; mode = "active"; model = null; selected = 0; fieldFilter = ""; returnFocus = item.name;
                renderDetail(); say(""); focusHeading();
            } catch (error) { if (request === epoch && !disposed) say(errorText(error)); }
        }
        function content() { return mode === "draft" ? model.content : active.definition || active; }
        function rows(value) {
            if (Array.isArray(value.mapping_definition_ids)) return value.mapping_definition_ids.map((id, index) => ({id, index, reference: true}));
            if (object(value.source) || object(value.target)) return [{id: "single", index: 0, value}];
            return [];
        }
        function renderDetail() {
            toolbar.empty().append(button("Back to mappings", () => leave(loadList)));
            body.empty().append($("<h3></h3>").text(`${active.operation_name || active.operation} · ${active.name}`),
                $("<p></p>").text(`${text("Active revision")}: ${active.revision}`));
            if (admin && active.mapping_type === "graphql") {
                toolbar.append(button("Active version", () => leave(() => { epoch++; ruleCache.clear(); model = null; mode = "active"; renderDetail(); })),
                    button("Open my draft", openDraft), button("My saved revisions", () => leave(() => showRevisions(0))));
                if (mode === "draft") {
                    toolbar.append(button("Compare active changes", compareActiveChanges), button("Save draft", save, true).attr("data-save-draft", ""), button("Discard saved draft", discard));
                    const reconciliationState = model.reconciliation ? "Local reconciliation changes — not saved"
                        : model.saved.reconciliation_json ? "Saved reconciliation evidence is current only for its recorded draft content."
                            : "No saved reconciliation evidence.";
                    body.append($("<p data-draft-state></p>"), $("<p class='text-muted'></p>").text(`${text("Personal draft revision")}: ${model.saved.content_revision} · ${text("Generation")}: ${model.saved.generation}`),
                        $("<p class='text-muted' data-reconciliation-state></p>").text(text(reconciliationState)));
                    if (model.blocked) toolbar.append(button("Check saved draft", reconcile), button("Reload saved draft", reloadDraft));
                }
            }
            if (admin && active.definition_id) toolbar.append(button("Bindings", () => leave(() => showBindings(0))));
            const value = content();
            renderContext(value);
            const list = rows(value);
            const grid = $("<div class='hero-mapping-editor-grid'></div>");
            const left = $("<section class='hero-mapping-table-wrap'></section>");
            const detail = $("<section class='hero-mapping-row-detail' aria-label='Selected field mapping'></section>");
            const table = $("<table class='table table-sm hero-mapping-table'><thead><tr><th>Source</th><th>Target</th><th>Rule</th><th></th></tr></thead><tbody></tbody></table>");
            const editable = mode === "draft";
            detail.attr("data-editable", String(editable));
            const searchInput = field("Filter field mappings", fieldFilter, (value) => { fieldFilter = value; }, left);
            const drawRows = () => {
                const focusedRow = table.find(":focus").attr("data-row-selection");
                table.find("tbody").empty(); const needle = searchInput.val().trim().toLowerCase();
                list.forEach((row, index) => {
                    const v = row.value, linked = ownRule(value, row.id) || ruleCache.get(row.id), source = row.reference ? (linked?.source_path || row.id) : `${v.source?.doctype || ""}.${v.source?.field || ""}`;
                    const target = row.reference ? (linked ? `${linked.target_doctype}.${linked.target_field}` : text("Existing active field rule")) : `${v.target?.operation || ""} / ${v.target?.argument || ""}.${v.target?.field_path ?? v.target?.field ?? ""}`;
                    const rule = row.reference ? (linked?.transformation || text("Referenced rule")) : v.transformation || v.transform || text("No transformation configured");
                    if (needle && !`${source} ${target} ${rule}`.toLowerCase().includes(needle)) return;
                    const tr = $("<tr></tr>").toggleClass("is-selected", selected === index);
                    tr.append($("<td></td>").append(button(source, () => { selected = index; drawRows(); renderRow(row, detail); }).attr("data-row-selection", index)), $("<td></td>").text(target), $("<td></td>").text(typeof rule === "string" ? rule : text("Configured special rule")));
                    const actions = $("<td></td>");
                    if (editable) actions.append(button("Remove", () => { model.remove(row.index); selected = 0; renderDetail(); const next = root.find("[data-row-selection]").eq(Math.min(row.index, rows(model.content).length - 1)); if (next.length) next.trigger("focus"); else root.find("button").filter((_, el) => el.textContent.startsWith(text("Undo removal"))).trigger("focus"); say("Mapping removed locally. Undo is available until a successful save."); }));
                    tr.append(actions); table.find("tbody").append(tr);
                });
                if (list.length && !table.find("tbody").children().length) table.find("tbody").append($("<tr><td colspan='4'></td></tr>").find("td").text(text("No field mappings match this filter.")).end());
                if (focusedRow !== undefined) table.find(`[data-row-selection="${focusedRow}"]`).trigger("focus");
            };
            root.off("hero-mapping-content-change").on("hero-mapping-content-change", drawRows);
            drawRows(); left.append(table);
            const tableEpoch = epoch;
            list.filter((row) => row.reference && !ownRule(value, row.id) && !ruleCache.has(row.id)).forEach(async (row) => {
                try {
                    const rule = await readActiveRule(row.id);
                    if (rule && !disposed && tableEpoch === epoch && table[0].isConnected && !ruleCache.has(row.id)) { ruleCache.set(row.id, rule); drawRows(); }
                } catch (_) { /* The reference remains visible; details offer the error context. */ }
            });
            if (!list.length) left.append($("<p></p>").text(text("No field mappings in this definition.")));
            if (editable) {
                if (Array.isArray(value.mapping_definition_ids)) left.append(button("Add existing field rule", addReference));
                else if (!value.source && !value.target && active.direction === "FRAPPE_TO_MUTATION") left.append(button("Add field mapping", () => { model.set(["source"], {}); model.set(["target"], {}); renderDetail(); }).prop("disabled", model.undo.length > 0));
                left.append(button(`Undo removal (${model.undo.length})`, () => { model.restore(); renderDetail(); }).prop("disabled", !model.undo.length));
            }
            grid.append(left, detail); body.append(grid);
            if (list[selected]) renderRow(list[selected], detail);
            for (const key of ["records_path", "record_path", "identity_path", "target_operation"]) {
                if (!(key in value)) continue;
                if (editable && typeof value[key] === "string") field(key.replaceAll("_", " "), value[key], (v) => model.set([key], v));
                else body.append($("<p></p>").text(`${key}: ${JSON.stringify(value[key])}`));
            }
            const structure = $("<details></details>").append($("<summary></summary>").text(text("Configured structure and additional rules")), safeJson(value));
            body.append(structure);
            if (!admin) body.append($("<p class='text-muted'></p>").text(text("Employee read-only view")));
            if (editable) body.append($("<p class='text-muted'></p>").text(text("Saving keeps a personal draft. It does not validate or activate the mapping.")));
            updateDirty(); if (busy) root.find("button,input,select,textarea").prop("disabled", true);
            if (model?.blocked) root.find("[data-save-draft]").prop("disabled", true);
        }
        function renderContext(value) {
            const mutation = mutationMapping(value);
            const sourceTitle = mutation ? "Source Frappe" : "Source HERO";
            const targetTitle = mutation ? "Target HERO" : "Target Frappe";
            const sourceLines = mutation
                ? [["Installed app", value.source?.app || "—"], ["DocType", value.source?.doctype || "—"], ["Field", value.source?.field || "—"]]
                : [["Validated schema snapshot", active.schema_snapshot || "—"], ["Query / operation", active.operation_name || value.operation_name || "—"], ["Object / path", value.records_path || value.record_path || value.source_path || "—"]];
            const targetLines = mutation
                ? [["Mutation", active.operation_name || value.target?.operation || "—"], ["Argument / input path", value.target?.argument || "—"], ["Input field", value.target?.field_path || value.target?.field || "—"]]
                : [["Installed app", value.target?.app || "—"], ["DocType", value.target?.doctype || "—"], ["Field", value.target?.field || value.target_field || "—"]];
            const context = $("<section class='hero-mapping-context' aria-label='Mapping context'></section>")
                .append($("<h4></h4>").text(text(mutation ? "Mutation mapping context" : "Query mapping context")));
            const pane = (title, lines) => {
                const section = $("<section class='hero-mapping-context-pane'></section>").append($("<h5></h5>").text(text(title)));
                lines.forEach(([label, value]) => section.append($("<p></p>").append($("<strong></strong>").text(`${text(label)}: `), document.createTextNode(String(value)))));
                return section;
            };
            context.append(pane(sourceTitle, sourceLines), pane(targetTitle, targetLines));
            body.append(context);
        }
        async function renderRow(row, panel) {
            const selectionRequest = ++detailRequest;
            panel.empty(); panel.attr("data-row-id", row.id);
            if (row.reference && mode === "draft" && ownRule(model.content, row.id)) {
                const rule = model.content.field_rules[row.id];
                panel.append($("<h4></h4>").text(text("Personal field rule")));
                for (const [label, key] of [["Source path", "source_path"], ["Source type", "source_type"], ["Target DocType", "target_doctype"],
                    ["Target field", "target_field"], ["Transformation", "transformation"], ["Purpose", "purpose"], ["Relation", "relation"]]) {
                    field(label, rule[key] ?? "", (next) => model.set(["field_rules", row.id, key], next), panel);
                }
                const priority = field("Priority", rule.priority ?? "", (next) => model.set(["field_rules", row.id, "priority"], next === "" ? null : Number(next)), panel);
                priority.attr({type: "number", step: "1", min: "-1000000", max: "1000000"});
                panel.append(button("Search source fields", () => picker("source", panel, row)), button("Search target fields", () => picker("target", panel, row)),
                    $("<p class='text-muted'></p>").text(text("Changes belong to your personal draft. The active field rule stays unchanged.")));
                return;
            }
            if (row.reference) {
                panel.append($("<h4></h4>").text(row.id), $("<p></p>").text(text("This definition references a separately maintained field rule.")));
                const request = epoch;
                try {
                    // Definition IDs of field rules are not document names. Resolve using the active list contract.
                    const match = await readActiveRule(row.id);
                    if (disposed || request !== epoch || selectionRequest !== detailRequest || !panel[0].isConnected || panel.attr("data-row-id") !== row.id) return;
                    if (match) { ruleCache.set(row.id, match); root.trigger("hero-mapping-content-change"); panel.append(safeJson(match)); }
                    else panel.append($("<p></p>").text(text("The referenced rule is not visible in these active search results.")));
                } catch (error) { if (!disposed && request === epoch && selectionRequest === detailRequest && panel[0].isConnected && panel.attr("data-row-id") === row.id) panel.append($("<p></p>").text(errorText(error))); }
                return;
            }
            const value = row.value;
            panel.append($("<h4></h4>").text(text("Field mapping details")));
            if (mode !== "draft" || panel.attr("data-editable") !== "true") { panel.append(safeJson(value)); return; }
            const mutation = mutationMapping(value);
            for (const [label, path] of [[mutation ? "Source Frappe DocType" : "Source HERO DocType", ["source", "doctype"]], [mutation ? "Source Frappe field" : "Source HERO field", ["source", "field"]],
                [mutation ? "Target HERO argument" : "Target Frappe argument", ["target", "argument"]], [mutation ? "Target HERO input field" : "Target Frappe field path", ["target", Object.hasOwn(value.target || {}, "field_path") ? "field_path" : "field"]],
                ["Transformation", ["transformation"]]]) {
                const v = path.reduce((a, key) => a?.[key], value);
                if (v !== undefined && typeof v !== "string") { panel.append($("<p></p>").text(`${label}: ${text("Configured special rule; preserved unchanged")}`)); continue; }
                field(label, v || "", (next) => { model.set(path, next); if (path[0] === "target" && path[1] === "field_path" && Object.hasOwn(value.target, "field")) model.set(["target", "field"], next); }, panel);
            }
            panel.append(button("Search source fields", () => picker("source", panel)), button("Search target fields", () => picker("target", panel)));
            panel.append($("<details></details>").append($("<summary></summary>").text(text("All configured rule parameters")), safeJson(value)));
        }
        async function openDraft() {
            leave(async () => {
                if (locked()) return; const request = ++epoch; setBusy(true); say("Loading your saved draft…");
                try {
                    const result = await rpc("mapping_draft_read", {definition_id: active.definition_id});
                    if (disposed || request !== epoch) return;
                    if (result.draft?.state === "OPEN") { model = new DraftState(result.draft); mode = "draft"; renderDetail(); say(""); }
                    else modal("Create personal draft", "No open personal draft exists. Create one from the active definition?", [
                        ["Create my draft", createDraft, true], ["Cancel", () => {}],
                    ]);
                } catch (error) { say(errorText(error)); }
                finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); } }
            });
        }
        async function createDraft() {
            if (locked()) return; setBusy(true); const request = ++epoch;
            try { const result = await rpc("mapping_draft_create", {definition_id: active.definition_id}, true);
                if (disposed || request !== epoch) return; model = new DraftState(result.draft); mode = "draft"; say("Personal draft opened.");
            } catch (error) { say(errorText(error)); }
            finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); } }
        }
        async function save() {
            if (locked() || !model || model.blocked) return false;
            const invalid = root.find("input").filter((_, el) => !el.checkValidity())[0];
            if (invalid) { invalid.reportValidity(); invalid.focus(); return false; }
            const state = model, submitted = clone(state.content), request = ++epoch; setBusy(true); say("Saving personal draft…");
            try {
                const result = await rpc("mapping_draft_save", state.saveArgs(), true);
                const saved = draftValue(result.draft);
                if (disposed || request !== epoch) return false;
                // Backend normalization must not silently erase additional stored parameters.
                if (!state.matches(saved.content, submitted)) {
                    state.blocked = true; say("The saved content differs from your edits. Your local changes have been kept. Check the saved draft before continuing."); return false;
                }
                state.accept(saved); say(result.unchanged ? "No changes to save. Personal draft unchanged." : "Personal draft saved. Not validated or activated."); return true;
            } catch (error) {
                if (disposed || request !== epoch) return false;
                state.blocked = true;
                const kind = error?.responseJSON?.exc_type || error?.exc_type;
                if (kind === "TimestampMismatchError") say("The saved draft or active version changed. Your edits and Undo have been kept. Check the saved draft before continuing.");
                else if (error?.status >= 400 && error?.status < 500) { state.blocked = false; say(errorText(error)); }
                else {
                    try { const result = await rpc("mapping_draft_read", {definition_id: state.saved.definition_id});
                        if (disposed || request !== epoch) return false;
                        if (state.resolve(result.draft, submitted)) { say("Personal draft save confirmed by reading it again."); return true; }
                        if (!state.blocked) { say("Your changes are not saved. Choose Save draft to try again."); return false; }
                    } catch (_) { /* Keep unknown status and local content; never retry a write. */ }
                    say("Save status is uncertain. Your edits and Undo have been kept. Check the saved draft before continuing.");
                }
                return false;
            } finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); } }
        }
        async function reconcile() {
            if (locked() || !model) return; setBusy(true); const request = ++epoch;
            try { const result = await rpc("mapping_draft_read", {definition_id: model.saved.definition_id});
                if (disposed || request !== epoch) return;
                if (model.resolve(result.draft, model.content)) say("Personal draft matches your edits. Save status confirmed.");
                else if (!model.blocked) say("Your changes are not saved. Choose Save draft to try again.");
                else say("The saved draft differs. Your local edits remain. Reload only if you want to replace them.");
            } catch (error) { say(errorText(error)); }
            finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); } }
        }
        async function compareActiveChanges() {
            if (locked() || !model) return;
            const state = model, candidateJson = JSON.stringify(state.content), request = ++epoch;
            setBusy(true); say("Comparing the original base, current active mapping, and your local draft…");
            try {
                const result = await rpc("mapping_draft_reconcile_preview", {
                    definition_id: state.saved.definition_id, expected_generation: state.saved.generation,
                    expected_draft_revision: state.saved.content_revision, expected_active_revision: active.revision,
                    expected_reconciliation_revision: state.saved.reconciliation_revision || 0, candidate_json: candidateJson,
                }, true);
                if (disposed || request !== epoch || state !== model) return;
                showReconciliation(result, state, candidateJson);
            } catch (error) { if (!disposed && request === epoch) say(errorText(error)); }
            finally { if (!disposed && request === epoch) setBusy(false); }
        }
        function showReconciliation(result, state, candidateJson) {
            const candidate = parseContent(result.candidate_content_json);
            const differences = result.items.filter((item) => item.state !== "UNCHANGED");
            const suggestions = result.items.filter((item) => item.state === "ACTIVE_ONLY");
            const conflicts = result.items.filter((item) => item.state === "CONFLICT");
            toolbar.empty().append(button("Back to draft", () => renderDetail()));
            body.empty().append($("<h3></h3>").text(text("Reconcile active changes")),
                $("<p></p>").text(`${text("Original base")}: ${result.base.name} · ${text("Revision")} ${result.base.revision}`),
                $("<p></p>").text(`${text("Current active")}: ${result.active.name} · ${text("Revision")} ${result.active.revision}`),
                $("<p></p>").text(`${text("Your local draft")}: ${text("Revision")} ${result.draft_revision}`));
            if (!differences.length) body.append($("<p></p>").text(text("No differences from the original base were found. Save draft can record this checked active revision.")));
            const choices = {};
            differences.forEach((item) => {
                const detail = $("<details></details>").append($("<summary></summary>").text(`${item.identifier} · ${item.state}`));
                detail.append($("<p></p>").text(`${text("Original base")}:`), safeJson(parseContent(item.base_json)),
                    $("<p></p>").text(`${text("Current active")}:`), safeJson(parseContent(item.active_json)),
                    $("<p></p>").text(`${text("Your draft")}:`), safeJson(parseContent(item.draft_json)));
                if (item.identities?.length) detail.append($("<p class='text-muted'></p>").text(`${text("Referenced identities")}: ${item.identities.join(", ")}`));
                if (item.state === "CONFLICT") {
                    const select = $("<select class='form-control' aria-label='Conflict resolution'></select>")
                        .append($("<option value='DRAFT'></option>").text(text("Keep my draft")), $("<option value='ACTIVE'></option>").text(text("Use current active")));
                    select.on("change", () => { choices[item.identifier] = select.val(); });
                    choices[item.identifier] = "DRAFT"; detail.append($("<label></label>").text(text("Conflict resolution")).append(select));
                }
                body.append(detail);
            });
            const apply = button("Apply reconciliation choices locally", async () => {
                if (locked() || state !== model) return;
                if (stable(state.content) !== stable(candidate)) { say("Your local draft changed after this comparison. Compare again; your edits have been kept."); return; }
                const request = ++epoch; setBusy(true); say("Applying reconciliation choices locally…");
                try {
                    const applied = await rpc("mapping_draft_reconcile_apply", {
                        definition_id: state.saved.definition_id, expected_generation: result.generation,
                        expected_draft_revision: result.draft_revision, expected_active_revision: result.active.revision,
                        expected_reconciliation_revision: result.reconciliation_revision,
                        candidate_json: result.candidate_content_json,
                        accepted_suggestions_json: JSON.stringify({items: suggestions.map((item) => item.identifier)}),
                        conflict_choices_json: JSON.stringify(choices),
                    }, true);
                    if (disposed || request !== epoch || state !== model) return;
                    state.applyReconciliation(applied.result_content_json, applied.reconciliation_json,
                        applied.preview.active.revision, applied.preview.reconciliation_revision);
                    say("Reconciliation choices are local only. Save draft to persist them."); renderDetail();
                    root.find("[data-save-draft]").trigger("focus");
                } catch (error) { if (!disposed && request === epoch) say(errorText(error)); }
                finally { if (!disposed && request === epoch) setBusy(false); }
            }, true).attr("data-apply-reconciliation", "");
            body.append(apply); apply.trigger("focus");
        }
        function reloadDraft() {
            modal("Reload saved draft", "Replace local edits and Undo history with your currently saved draft?", [["Reload saved draft", async () => {
                if (locked()) return; const request = ++epoch; setBusy(true);
                try { const result = await rpc("mapping_draft_read", {definition_id: active.definition_id});
                    if (disposed || request !== epoch) return;
                    if (result.draft?.state === "OPEN") { model.accept(result.draft); say("Saved personal draft reloaded."); }
                    else say("There is no open saved draft. Your local changes remain.");
                } catch (error) { say(errorText(error)); }
                finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); } }
            }], ["Continue editing", () => {}]]);
        }
        function discard() {
            if (locked() || !model) return;
            modal("Discard saved personal draft", model.dirty ? "Discard your saved personal draft and all unsaved local changes? Saved revisions remain available." : "Discard your saved personal draft? Saved revisions remain available.", [
                ["Discard saved draft", async () => {
                    if (locked()) return; setBusy(true); const request = ++epoch;
                    try { await rpc("mapping_draft_discard", {definition_id: model.saved.definition_id, expected_generation: model.saved.generation,
                        expected_draft_revision: model.saved.content_revision, confirmed: true}, true);
                        if (disposed || request !== epoch) return; model = null; mode = "active"; say("Personal draft discarded.");
                    } catch (error) { say(errorText(error)); }
                    finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); } }
                }], ["Continue editing", () => {}],
            ]);
        }
        async function showRevisions(offset) {
            const request = ++epoch; revisionsStart = offset; say("Loading your saved revisions…");
            try { const result = await rpc("mapping_draft_revisions", {definition_id: active.definition_id, start: revisionsStart, page_size: 20});
                if (disposed || request !== epoch) return;
                toolbar.empty().append(button("Back to definition", () => { mode = model ? "draft" : "active"; renderDetail(); }));
                body.empty().append($("<h3></h3>").text(text("My saved revisions — read only")));
                if (!result.items.length) body.append($("<p></p>").text(text("No saved personal revisions.")));
                result.items.forEach((item) => body.append($("<details></details>").append($("<summary></summary>").text(`${text("Generation")} ${item.generation} · ${text("Revision")} ${item.revision}`), safeJson(typeof item.content_json === "string" ? parseContent(item.content_json) : item.content))));
                body.append(button("Previous revisions", () => showRevisions(Math.max(0, revisionsStart - 20))).prop("disabled", !revisionsStart),
                    button("Next revisions", () => showRevisions(revisionsStart + 20)).prop("disabled", revisionsStart + 20 >= result.total)); say("");
            } catch (error) { if (!disposed && request === epoch) say(errorText(error)); }
        }
        async function showBindings(offset) {
            if (!admin || !active?.definition_id) return;
            const request = ++epoch;
            toolbar.empty().append(button("Back to definition", () => { mode = model ? "draft" : "active"; renderDetail(); }));
            body.empty().append($("<h3></h3>").text(text("Bindings — read only")),
                $("<p class='text-muted'></p>").text(text("Existing binding identities, mappings and status. This view cannot create, update, synchronize or activate a binding.")));
            say("Loading bindings…");
            try {
                const result = await rpc("frontend_mapping_bindings", {definition_id: active.definition_id, start: offset, page_size: 20});
                if (disposed || request !== epoch) return;
                if (!result.items.length) body.append($("<p></p>").text(text("No existing bindings use this mapping definition.")));
                result.items.forEach((item) => {
                    const summary = `${item.direction} · ${item.source_instance}.${item.source_field} → ${item.target_doctype}.${item.target_field} · ${item.sync_state || "UNACKNOWLEDGED"}`;
                    const detail = $("<details class='hero-mapping-binding'></details>").append($("<summary></summary>").text(summary));
                    detail.append(safeJson({identity_key: item.identity_key, object_binding: item.object_binding,
                        mapping_definition_id: item.mapping_definition_id, mapping_revision: item.mapping_revision,
                        binding_revision: item.binding_revision, acknowledged_version: item.acknowledged_version,
                        object: item.object || null}));
                    body.append(detail);
                });
                const p = result.pagination;
                body.append($("<div class='hero-mapping-actions'></div>").append(
                    button("Previous bindings", () => showBindings(Math.max(0, p.start - p.page_size))).prop("disabled", !p.start),
                    $("<span></span>").text(`${text("Bindings")} ${p.total ? p.start + 1 : 0}–${Math.min(p.start + p.page_size, p.total)} / ${p.total}`),
                    button("Next bindings", () => showBindings(p.start + p.page_size)).prop("disabled", p.start + p.page_size >= p.total),
                ));
                say("");
            } catch (error) { if (!disposed && request === epoch) { say(errorText(error)); body.append(button("Retry", () => showBindings(offset))); } }
        }
        async function addReference() {
            const panel = $("<section class='hero-mapping-row-detail'></section>"); body.append(panel);
            const query = field("Search existing active field rules", "", () => {}, panel);
            const resultHost = $("<div></div>").appendTo(panel); let seq = 0, ruleStart = 0;
            const loadRules = async () => {
                const request = ++seq; resultHost.empty();
                try { const result = await rpc("frontend_mapping_list", {search: query.val().slice(0, 120), start: ruleStart, page_size: 100});
                    if (request !== seq || !panel[0].isConnected) return;
                    const available = result.frappe_mappings.filter((rule) => rule.schema_snapshot === active.schema_snapshot && rule.schema_fingerprint === active.schema_fingerprint && rule.operation === (model.content.target_operation || active.operation_name));
                    resultHost.append($("<p></p>").text(`${text("Available rules on this page")}: ${available.length}. ${text("Active search results")}: ${result.pagination.frappe_total}.`));
                    available.forEach((rule) => resultHost.append(button(`${rule.source_path} → ${rule.target_doctype}.${rule.target_field} · ${rule.transformation}`, () => {
                        if (!model.content.mapping_definition_ids.includes(rule.definition_id)) {
                            model.addReference(rule.definition_id);
                            if (!model.content.field_rules) model.content.field_rules = {};
                            if (!ownRule(model.content, rule.definition_id)) model.set(["field_rules", rule.definition_id], Object.fromEntries(
                                ["source_path", "source_type", "target_doctype", "target_field", "purpose", "transformation", "relation", "priority"].map((key) => [key, rule[key] ?? null])));
                        }
                        renderDetail();
                    })));
                    resultHost.append(button("Previous rules", () => { ruleStart = Math.max(0, ruleStart - 100); loadRules(); }).prop("disabled", !ruleStart),
                        button("Next rules", () => { ruleStart += 100; loadRules(); }).prop("disabled", ruleStart + 100 >= result.pagination.frappe_total || ruleStart >= 10000));
                } catch (error) { if (request === seq && panel[0].isConnected) resultHost.text(errorText(error)); }
            };
            panel.append(button("Search rules", () => { ruleStart = 0; loadRules(); }), button("Close field search", () => panel.remove())); query.trigger("focus");
        }
        async function picker(side, panel, row = {}) {
            const pickerHost = $("<section></section>"); panel.append(pickerHost);
            const query = field("Search fields by name, full path or type", "", () => {}, pickerHost);
            const pickerId = `hero-mapping-picker-${Math.random().toString(36).slice(2)}`;
            const app = $("<input class='form-control' aria-label='App' autocomplete='off'>").attr("list", `${pickerId}-apps`);
            const doctype = $("<input class='form-control' aria-label='DocType' autocomplete='off'>").attr("list", `${pickerId}-doctypes`);
            const appOptions = $("<datalist></datalist>").attr("id", `${pickerId}-apps`);
            const doctypeOptions = $("<datalist></datalist>").attr("id", `${pickerId}-doctypes`);
            const catalogState = $("<p class='text-muted'></p>");
            const results = $("<div></div>"); let seq = 0, fieldStart = 0, frappeCatalog = null;
            const source = row.reference ? side === "target" : side === "source"; pickerHost.append(app, appOptions, doctype, doctypeOptions, catalogState, results);
            const load = async () => {
                if (source && !doctype.val()) { results.text(text("Choose an app and DocType before searching fields.")); return; }
                const request = ++seq; results.text(text("Loading fields…"));
                try {
                    let fields;
                    if (source && mutationMapping() && !row.reference) {
                        const available = (frappeCatalog?.fields || []).filter((field) => field.app_name === app.val() && field.doctype === doctype.val() && `${field.path} ${field.label} ${field.fieldtype} ${field.description || ""}`.toLowerCase().includes(query.val().toLowerCase()));
                        fields = available.slice(fieldStart, fieldStart + 50);
                        results.text(`${available.length ? fieldStart + 1 : 0}–${Math.min(fieldStart + fields.length, available.length)} / ${available.length}`);
                        results.append(button("Previous fields", () => { fieldStart = Math.max(0, fieldStart - 50); load(); }).prop("disabled", !fieldStart), button("Next fields", () => { fieldStart += 50; load(); }).prop("disabled", fieldStart + 50 >= available.length));
                    } else if (source) { const result = await rpc("mapping_target_catalog_page", {app_name: app.val() || "", doctype: doctype.val() || "", search: query.val(), start: fieldStart, page_size: 50}); fields = result.items; if (request !== seq || !pickerHost[0].isConnected) return; results.text(`${result.total ? fieldStart + 1 : 0}–${Math.min(fieldStart + fields.length, result.total)} / ${result.total}`); results.append(button("Previous fields", () => { fieldStart = Math.max(0, fieldStart - 50); load(); }).prop("disabled", !fieldStart), button("Next fields", () => { fieldStart += 50; load(); }).prop("disabled", fieldStart + 50 >= result.total)); }
                    else if (row.reference) {
                        const result = await rpc("mapping_source_picker_page", {snapshot_name: active.schema_snapshot, operation: model.content.target_operation || active.operation_name,
                            search: query.val(), start: fieldStart, page_size: 50});
                        if (request !== seq || !pickerHost[0].isConnected) return;
                        fields = result.items; results.text(`${result.total ? fieldStart + 1 : 0}–${Math.min(fieldStart + fields.length, result.total)} / ${result.total}`);
                        results.append(button("Previous fields", () => { fieldStart = Math.max(0, fieldStart - 50); load(); }).prop("disabled", !fieldStart),
                            button("Next fields", () => { fieldStart += 50; load(); }).prop("disabled", fieldStart + 50 >= result.total));
                    }
                    else { const catalog = await rpc("schema_operation_catalog", {snapshot_name: active.schema_snapshot, operation_kind: active.operation_kind});
                        const operation = catalog.find((v) => v.operation_name === active.operation_name);
                        const all = Object.entries(operation?.input_fields || {}).flatMap(([argument, list]) => list.map((f) => ({...f, argument, fieldtype: f.type})));
                        const needle = query.val().toLowerCase(); const matched = all.filter((v) => `${v.argument}.${v.path} ${v.type}`.toLowerCase().includes(needle));
                        fields = matched.slice(fieldStart, fieldStart + 50); results.text(`${matched.length ? fieldStart + 1 : 0}–${Math.min(fieldStart + fields.length, matched.length)} / ${matched.length}`);
                        results.append(button("Previous fields", () => { fieldStart = Math.max(0, fieldStart - 50); load(); }).prop("disabled", !fieldStart), button("Next fields", () => { fieldStart += 50; load(); }).prop("disabled", fieldStart + 50 >= matched.length)); }
                    if (request !== seq || !pickerHost[0].isConnected) return;
                    fields.forEach((f) => results.append(button(`${f.argument ? f.argument + "." : ""}${f.path} · ${f.fieldtype || f.type}${f.nullable === false ? " · required" : ""}${f.select_options?.length ? ` · enum: ${f.select_options.join(", ")}` : ""}${f.selectable === false ? ` · unavailable: ${f.reason || "not permitted"}` : ""}`, () => {
                        if (row.reference) {
                            if (source) { model.set(["field_rules", row.id, "target_doctype"], f.target_doctype || doctype.val()); model.set(["field_rules", row.id, "target_field"], f.path); }
                            else { model.set(["field_rules", row.id, "source_path"], f.path); model.set(["field_rules", row.id, "source_type"], f.mapping_type || f.fieldtype); }
                        }
                        else if (source) { model.set(["source", "doctype"], f.target_doctype || doctype.val()); model.set(["source", "field"], f.path); }
                        else { model.set(["target", "argument"], f.argument); model.set(["target", "operation"], active.operation_name); model.set(["target", "field"], f.path); if (Object.hasOwn(model.content.target, "field_path")) model.set(["target", "field_path"], f.path); model.set(["target", "graphql_type"], f.type); }
                        renderDetail();
                    }).prop("disabled", f.selectable === false)));
                } catch (error) { if (request === seq && pickerHost[0].isConnected) results.text(errorText(error)); }
            };
            const searchFields = button("Search fields", () => { fieldStart = 0; load(); }).prop("disabled", source);
            pickerHost.append(searchFields, button("Close field search", () => pickerHost.remove()));
            if (source) results.text(text("Choose an app and DocType before searching fields."));
            if (!source) { app.hide(); doctype.hide(); query.trigger("focus"); return; }
            try { const catalogMethod = mutationMapping() && !row.reference ? "writeback_mapper_catalog" : "mapping_target_catalog";
                const result = await rpc(catalogMethod, catalogMethod === "writeback_mapper_catalog" ? {snapshot_name: active.schema_snapshot} : {});
                if (!pickerHost[0].isConnected) return;
                frappeCatalog = result;
                result.apps.forEach((item) => appOptions.append($("<option></option>").val(item.app_name || item)));
                catalogState.text(`${text("Searchable installed-app catalog")}: ${result.apps.length} ${text("apps")}, ${result.doctypes.length} ${text("DocTypes")}.`);
                app.on("input change", () => { fieldStart = 0; searchFields.prop("disabled", true); doctype.val(""); doctypeOptions.empty(); const matches = result.doctypes.filter((item) => item.app_name === app.val()); matches.forEach((item) => doctypeOptions.append($("<option></option>").val(item.doctype))); catalogState.text(`${text("Searchable DocTypes")}: ${matches.length}. ${text("Unavailable options remain listed and explain why when selected.")}`); });
                doctype.on("input change", () => { const item = result.doctypes.find((candidate) => candidate.app_name === app.val() && candidate.doctype === doctype.val()); searchFields.prop("disabled", !item?.selectable); catalogState.text(item ? `${item.doctype}: ${item.selectable ? text("available") : item.reason || text("unavailable")}` : text("Choose a listed app and DocType.")); });
                query.trigger("focus");
            } catch (error) { results.text(errorText(error)); }
        }
        const pageUrl = location.href, pageHistoryState = history.state;
        const router = frappe.router, originalRoute = router.route;
        let routePending = false;
        // Frappe starts its SPA route before later window popstate listeners.
        // Keep this adapter local to the mounted editor and restore its exact owner on destroy.
        function guardedRoute(...args) {
            if (!model?.dirty && !busy) return originalRoute.apply(this, args);
            const destination = location.href;
            history.pushState(pageHistoryState, "", pageUrl);
            if (busy || routePending) { say("Wait for the current request before leaving."); return Promise.resolve(false); }
            routePending = true;
            return new Promise((resolve) => leave(() => {
                routePending = false;
                history.replaceState(pageHistoryState, "", destination);
                resolve(originalRoute.apply(router, args));
            }, () => { routePending = false; resolve(false); }));
        }
        router.route = guardedRoute;
        const unload = (event) => { if (model?.dirty || busy) { event.preventDefault(); event.returnValue = ""; } };
        window.addEventListener("beforeunload", unload);
        // Capture links leaving the page before Frappe's delegated router handles them.
        const links = (event) => {
            const link = event.target.closest("a[href]");
            if (link && (model?.dirty || busy) && !event.defaultPrevented && !event.metaKey && !event.ctrlKey && link.origin === location.origin && link.href !== location.href) {
                event.preventDefault(); event.stopImmediatePropagation(); leave(() => { location.href = link.href; });
            }
        };
        document.addEventListener("click", links, true);
        const controller = {beforeLeave: leave, destroy() { shell.removeClass("hero-mapping-shell"); disposed = true; epoch++; window.removeEventListener("beforeunload", unload); if (router.route === guardedRoute) router.route = originalRoute; document.removeEventListener("click", links, true); }};
        current = controller; loadList(); return controller;
    }
    window.hero_integration_mapping_editor = Object.freeze({mount, DraftState, stable, readActiveRule});
})();
