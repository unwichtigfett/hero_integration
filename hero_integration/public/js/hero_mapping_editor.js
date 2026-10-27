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
    const exchangeExpectedStates = (preview) => {
        if (!Array.isArray(preview?.items)) throw new Error("Invalid directional import preview.");
        return preview.items.map((item) => {
            const expected = item?.expected;
            if (typeof item?.definition_id !== "string" || !expected ||
                !["generation", "draft_revision", "active_revision", "reconciliation_revision"].every((key) => Number.isInteger(expected[key]) && expected[key] >= 0)) {
                throw new Error("Invalid directional import preview.");
            }
            return {definition_id: item.definition_id, generation: expected.generation,
                draft_revision: expected.draft_revision, active_revision: expected.active_revision,
                reconciliation_revision: expected.reconciliation_revision};
        });
    };
    const exchangeCounts = (preview) => (preview?.items || []).reduce((result, item) => {
        const state = item?.state;
        if (["NEW", "REPLACE", "UNCHANGED", "REMOVE"].includes(state)) result[state]++;
        return result;
    }, {NEW: 0, REPLACE: 0, UNCHANGED: 0, REMOVE: 0});
    const exchangeCanAdopt = (entry, currentEntry, contextKey, locked) => !locked &&
        entry === currentEntry && entry?.context?.key === contextKey && entry?.preview?.can_save === true;
    const reviewedTargetLabel = (field) => {
        const target = field?.target;
        if (!target || ![target.operation, target.argument, target.field].every((value) => typeof value === "string" && value)) return "";
        return `${target.operation} / ${target.argument}.${target.field}`;
    };
    const writebackContractLabel = (catalog, operation) => {
        if (!Array.isArray(catalog?.contracts) || !operation) return "Technical contract status unavailable.";
        const matching = catalog.contracts.filter((item) => item.operation === operation);
        if (matching.length !== 1 || typeof matching[0].activation_ready !== "boolean") return "Technical contract status unavailable.";
        if (!matching[0].activation_ready) return matching[0].blocker || "Technical contract is not ready.";
        return "Technical contract is active; mapping validation and separate execution gates still apply.";
    };
    const legacyWritebackRows = (rows) => {
        if (!Array.isArray(rows) || rows.some((row) => !object(row) || typeof row.name !== "string" || !row.name ||
            typeof row.direction !== "string" || typeof row.operation_kind !== "string" || typeof row.operation_name !== "string" ||
            typeof row.status !== "string" || !Number.isInteger(row.mapping_revision) || row.mapping_revision < 0)) {
            throw new Error("Invalid shared mapping list.");
        }
        return rows.filter((row) => row.direction === "FRAPPE_TO_MUTATION" && row.operation_kind === "MUTATION");
    };
    const legacyWritebackDetailMatches = (doc, row, snapshot) => object(doc) && doc.doctype === "HERO GraphQL Mapping" &&
        doc.name === row.name && doc.schema_snapshot === snapshot && doc.direction === "FRAPPE_TO_MUTATION" &&
        doc.operation_kind === "MUTATION" && doc.operation_name === row.operation_name && doc.status === row.status &&
        doc.mapping_revision === row.mapping_revision && (doc.suggestion_reason || "") === (row.suggestion_reason || "") &&
        (doc.confidence ?? null) === (row.confidence ?? null) && typeof doc.mapping_json === "string";
    const sharedWritebackDraftAccess = (doc, row, snapshot, actor) => {
        if (!legacyWritebackDetailMatches(doc, row, snapshot)) return "STALE";
        if (doc.status !== "Draft") return "NOT_DRAFT";
        if (typeof actor !== "string" || !actor || typeof doc.modified_by !== "string" || doc.modified_by !== actor) return "OWNER_CONFLICT";
        if (!Number.isInteger(doc.mapping_revision) || doc.mapping_revision < 1 || typeof doc.definition_id !== "string" || !doc.definition_id) return "INVALID";
        return "EDITABLE";
    };
    const reviewedWritebackEditable = (mappingJson, catalog) => {
        if (typeof mappingJson !== "string" || !Array.isArray(catalog?.fields) || typeof JSON.rawJSON !== "function") return false;
        let rawVersion = false, mapping;
        try {
            mapping = JSON.parse(mappingJson, (key, value, context) => {
                if (typeof value === "number") {
                    if (key !== "version" || value !== 1 || context?.source !== "1") throw new Error("Unsafe number.");
                    rawVersion = true;
                }
                return value;
            });
        } catch (_) { return false; }
        // A round trip must preserve the original JSON tokens; this also rejects
        // duplicate keys and alternate escaped tokens before any edit is allowed.
        const compact = mappingJson.replace(/"(?:\\.|[^"\\])*"|\s+/g, (part) => part[0] === '"' ? part : "");
        if (JSON.stringify(mapping) !== compact) return false;
        const exactKeys = (value, keys) => object(value) && Object.keys(value).sort().join("\0") === [...keys].sort().join("\0");
        if (!rawVersion || !exactKeys(mapping, ["version", "source", "target", "transformation"]) ||
            !exactKeys(mapping.source, ["doctype", "field"]) ||
            !exactKeys(mapping.target, ["operation", "argument", "input_type", "field"]) ||
            ![mapping.source.doctype, mapping.source.field, mapping.target.operation, mapping.target.argument, mapping.target.input_type, mapping.target.field].every((v) => typeof v === "string" && v) ||
            !["Identity", "Trim"].includes(mapping.transformation)) return false;
        const matches = catalog.fields.filter((field) => field.doctype === mapping.source.doctype && field.path === mapping.source.field && field.selectable === true &&
            field.target && ["operation", "argument", "input_type", "field"].every((key) => field.target[key] === mapping.target[key]));
        return matches.length === 1;
    };
    const buildReviewedWriteback = (catalog, doctype, field, transformation) => {
        if (!Array.isArray(catalog?.fields) || !["Identity", "Trim"].includes(transformation)) throw new Error("Invalid reviewed writeback selection.");
        const matches = catalog.fields.filter((item) => item.doctype === doctype && item.path === field && item.selectable === true && item.target &&
            ["operation", "argument", "input_type", "field"].every((key) => typeof item.target[key] === "string" && item.target[key]));
        if (matches.length !== 1) throw new Error("Choose exactly one reviewed source and target.");
        const target = Object.fromEntries(["operation", "argument", "input_type", "field"].map((key) => [key, matches[0].target[key]]));
        return {version: 1, source: {doctype, field}, target, transformation};
    };
    const sharedWritebackRequest = ({doc, row, actor, catalog, snapshot, choice, transformation, reason, confidence}) => {
        if (typeof reason !== "string" || reason.length > 1000 || typeof confidence !== "string" || confidence === "") throw new Error("Invalid review values.");
        const score = Number(confidence);
        if (!Number.isFinite(score) || score < 0 || score > 1) throw new Error("Confidence must be between 0 and 1.");
        if (!["Identity", "Trim"].includes(transformation)) throw new Error("Invalid transformation.");
        let mapping, changedReason = true, changedConfidence = true;
        if (doc) {
            if (sharedWritebackDraftAccess(doc, row, snapshot, actor) !== "EDITABLE") throw new Error("Shared draft status, owner or revision changed.");
            if (!reviewedWritebackEditable(doc.mapping_json, catalog)) throw new Error("This shared draft cannot be edited without losing parameters.");
            const initial = JSON.parse(doc.mapping_json);
            changedReason = reason !== String(doc.suggestion_reason || "");
            changedConfidence = score !== Number(doc.confidence ?? 0);
            if (!changedReason && !changedConfidence && transformation === initial.transformation) return null;
            mapping = {...initial, transformation};
        } else {
            if (!choice) throw new Error("Choose a reviewed source and target.");
            mapping = buildReviewedWriteback(catalog, choice.doctype, choice.path, transformation);
        }
        const args = {snapshot_name:snapshot, operation_kind:"MUTATION", operation_name:mapping.target.operation,
            direction:"FRAPPE_TO_MUTATION", mapping_json:JSON.stringify(mapping), mapping_name:doc?.name || "",
            expected_revision:doc?.mapping_revision || 0, activate:0};
        if (changedReason) args.suggestion_reason = reason;
        if (changedConfidence) args.confidence = score;
        return args;
    };
    const sharedWritebackSaveAccepted = (result, doc) => Boolean(result && typeof result.name === "string" && result.name &&
        result.status === "Draft" && Number.isInteger(result.revision) && result.revision >= 1 && result.unchanged === false &&
        (!doc || (result.name === doc.name && result.revision > doc.mapping_revision)));
    const finishSharedWritebackSave = async (saved, current, release, refresh) => {
        release();
        if (saved && current()) await refresh();
    };
    const legacyWritebackCurrent = (admin, viewTab, currentTab, viewEpoch, currentEpoch, serial, currentSerial, snapshot, selected, connected, disposed) =>
        admin && viewTab === "Mutation" && currentTab === viewTab && viewEpoch === currentEpoch && serial === currentSerial &&
        Boolean(snapshot) && snapshot === selected && connected && !disposed;
    const legacyCandidateArgs = (snapshot, operation, source, doctype, target, sample) => {
        if (![snapshot, operation, source, doctype, target].every((v) => typeof v === "string" && v.trim()))
            throw new Error("Choose a validated snapshot, query, source path, target DocType and target field.");
        let record;
        try { record = JSON.parse(sample); } catch (_) { throw new Error("Enter a synthetic JSON object."); }
        if (!object(record)) throw new Error("Enter a synthetic JSON object.");
        return {snapshot_name: snapshot, operation, source_path: source.trim(), target_doctype: doctype.trim(),
            target_field: target.trim(), source_record_json: sample};
    };
    const legacyCandidateMatches = (result, args) => object(result) && result.writes === false &&
        result.operation === args.operation && result.source_path === args.source_path &&
        result.target_doctype === args.target_doctype && result.target_field === args.target_field && object(result.values);
    const legacyCandidateRawMessage = (raw) => {
        if (typeof raw !== "string" || typeof JSON.rawJSON !== "function") throw new Error("This browser cannot preserve preview numbers.");
        let supported = false;
        JSON.parse("1", (_key, value, context) => { supported = context?.source === "1"; return value; });
        if (!supported) throw new Error("This browser cannot preserve preview numbers.");
        const response = JSON.parse(raw, (_key, value, context) => {
            if (typeof value !== "number") return value;
            if (typeof context?.source !== "string") throw new Error("This browser cannot preserve preview numbers.");
            return JSON.rawJSON(context.source);
        });
        if (!object(response) || response.exc) throw new Error("Invalid legacy preview response.");
        return response.message;
    };
    async function legacyCandidateRpc(args, call = (options) => frappe.call(options)) {
        let raw;
        const response = await call({method: "hero_integration.api.preview_mapping_candidate", type: "POST", args,
            callback: (_data, responseText) => { raw = responseText; }});
        if (response?.exc) throw response;
        return legacyCandidateRawMessage(raw);
    }
    const legacyCandidatePreviewText = (result, args) => {
        if (!legacyCandidateMatches(result, args)) throw new Error("Invalid legacy preview response.");
        return JSON.stringify({operation: result.operation, source_path: result.source_path,
            target_doctype: result.target_doctype, target_field: result.target_field, target_app: result.target_app,
            values: result.values, writes: false}, null, 2);
    };
    const legacyPackageSelection = (preview, selectedIds) => {
        if (!Array.isArray(preview?.items) || !/^[a-f0-9]{64}$/.test(preview?.preview_revision || "") || !Array.isArray(selectedIds)) throw new Error("Invalid package preview.");
        const byId = new Map();
        for (const item of preview.items) {
            if (typeof item?.id !== "string" || !item.id || byId.has(item.id) || !["schema", "mapping"].includes(item.kind) ||
                typeof item.selectable !== "boolean" || typeof item.state !== "string" || !Array.isArray(item.dependencies) ||
                !item.dependencies.every((id) => typeof id === "string" && id)) throw new Error("Invalid package preview.");
            byId.set(item.id, item);
        }
        const selected = new Set(selectedIds);
        if (!selected.size || selected.size !== selectedIds.length) throw new Error("Select at least one valid change.");
        for (const id of selected) {
            const item = byId.get(id);
            if (!item?.selectable || !["New", "Changed"].includes(item.state)) throw new Error("The selection contains an unavailable item.");
            if (item.dependencies.some((dependency) => !selected.has(dependency) && byId.get(dependency)?.state !== "Unchanged"))
                throw new Error("Select all required dependencies first.");
        }
        return selectedIds.map((id) => byId.get(id));
    };
    const legacyPackageAdoptionArgs = (snapshot, payload, preview, ids) => {
        if (typeof snapshot !== "string" || !snapshot || typeof payload !== "string" || !payload) throw new Error("Invalid package context.");
        legacyPackageSelection(preview, ids);
        return {snapshot_name: snapshot, payload_json: payload, selected_ids_json: JSON.stringify(ids), confirmed: true, preview_revision: preview.preview_revision};
    };
    const isOwnedExportLink = (link, ownedLink) => Boolean(ownedLink) && link === ownedLink;
    const mappingTabs = Object.freeze(["Query", "Mutation", "Binding"]);
    async function readSnapshotPages(fetchPage, isCurrent) {
        const result = [], seen = new Set();
        for (let start = 0; ; start += 100) {
            const page = await fetchPage({start, page_size: 100});
            if (!isCurrent()) return null;
            if (!Array.isArray(page) || page.length > 100) throw new Error('Invalid snapshot page.');
            for (const item of page) {
                if (typeof item?.name !== 'string' || !item.name || seen.has(item.name)) throw new Error('Snapshot list changed or contains invalid identities.');
                seen.add(item.name);
            }
            result.push(...page);
            if (page.length < 100) return result;
            if (start > 2147483547) throw new Error('Snapshot list exceeds the supported range.');
        }
    }
    const directionForTab = (tab) => tab === "Query" ? "QUERY_TO_FRAPPE" : tab === "Mutation" ? "FRAPPE_TO_MUTATION" : null;
    const bindingAccess = (admin, definition) => !admin ? "ADMIN_ONLY" : definition?.definition_id ? "READ_ONLY" : "SELECT_DEFINITION";
    const resumePendingView = (pending, handlers) => {
        if (pending?.kind === "list") return handlers.list();
        if (pending?.kind === "detail") return handlers.detail(pending.item, pending.mappingType);
        return false;
    };
    const selectedPickerApp = (model, side, value, admin) =>
        model?.frappeSelection?.side === side && model.frappeSelection.doctype === value?.doctype
            ? model.frappeSelection.app : admin ? "" : value?.app || "";
    const hasCurrentDraftBase = (model, active) => Boolean(model && !model.detailStale && active &&
        active.definition_id === model.saved.definition_id &&
        (!model.saved.base_active_name || active.name === model.saved.base_active_name) &&
        active.revision === (model.reconciliation?.activeRevision ?? model.verifiedActiveRevision ?? model.saved.base_active_revision));
    const canSaveDraft = (model, active) => Boolean(model && !model.blocked && hasCurrentDraftBase(model, active));
    function tabKeyTarget(tab, key) {
        const index = mappingTabs.indexOf(tab);
        if (index < 0) return null;
        if (key === "Home") return mappingTabs[0];
        if (key === "End") return mappingTabs.at(-1);
        const delta = key === "ArrowRight" ? 1 : key === "ArrowLeft" ? -1 : 0;
        return delta ? mappingTabs[(index + delta + mappingTabs.length) % mappingTabs.length] : null;
    }
    class MappingTabStates {
        constructor() { this.states = new Map(); }
        set(tab, state) { this.states.set(tab, state); }
        get(tab) { return this.states.get(tab); }
        otherDirty(tab) { return [...this.states.entries()].some(([name, state]) => name !== tab && state.model?.dirty); }
    }
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
        setFrappeSource(app, doctype, field) {
            this.frappeSelection = {side: "source", app, doctype};
            this.set(["source", "doctype"], doctype);
            this.set(["source", "field"], field);
        }
        setFrappeTarget(app, doctype, field) {
            this.frappeSelection = {side: "target", app, doctype};
            this.set(["target", "doctype"], doctype);
            this.set(["target", "field"], field);
            if (Object.hasOwn(this.content.target, "field_path")) this.set(["target", "field_path"], field);
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
        let disposed = false, epoch = 0, busy = false, sharedDirty = false, model = null, active = null, mode = "active", selected = 0, ownedDownload = null;
        let search = String(context.mapping_search || ""), start = Number(context.mapping_start || 0), listing;
        let revisionsStart = 0, returnFocus = null, fieldFilter = "", detailRequest = 0, exchangeSequence = 0;
        const exchanges = new Map();
        const ruleCache = new Map();
        const button = (label, fn, primary = false) => $("<button type='button'></button>")
            .addClass(primary ? "btn btn-primary btn-sm" : "btn btn-default btn-sm").text(text(label)).on("click", fn);
        const status = $("<p role='status' aria-live='polite'></p>");
        const toolbar = $("<div class='hero-mapping-actions'></div>");
        const body = $("<div class='hero-mapping-body'></div>");
        const tabs = $("<div role='tablist' aria-label='Mapping sections' class='hero-mapping-actions'></div>");
        const panel = $("<section role='tabpanel' id='hero-mapping-tabpanel'></section>").append(toolbar, body);
        root.append($("<h2 tabindex='-1'></h2>").text(text("Mapping")), tabs, status, panel);
        const tabNames = mappingTabs, tabStates = new MappingTabStates();
        let tab = "Query", bindingOrigin = "Query", rowChangeHandler = null, pendingView = null;
        const tabButtons = tabNames.map((name) => button(name, () => switchTab(name)).attr({
            role: "tab", id: `hero-mapping-tab-${name.toLowerCase()}`, "aria-controls": "hero-mapping-tabpanel"
        }).appendTo(tabs));
        function updateTabs() {
            tabButtons.forEach((el, index) => el.attr({"aria-selected": String(tabNames[index] === tab), tabindex: tabNames[index] === tab ? "0" : "-1"}));
            panel.attr("aria-labelledby", `hero-mapping-tab-${tab.toLowerCase()}`);
        }
        tabs.on("keydown", "[role=tab]", (event) => {
            const next = tabKeyTarget(tab, event.key);
            if (!next) return;
            event.preventDefault(); switchTab(next); tabButtons[tabNames.indexOf(tab)].trigger("focus");
        });
        function otherDirty() { return tabStates.otherDirty(tab); }
        function anyDirty() { return Boolean(sharedDirty || model?.dirty || otherDirty()); }
        function switchTab(next) {
            if (next === tab || locked() || root.find("dialog").length) return;
            const previous = tab;
            epoch++; detailRequest++;
            tabStates.set(previous, {active, model, mode, selected, search, start, listing, revisionsStart, returnFocus, fieldFilter,
                rowChangeHandler, pendingView, status: status.text(), toolbar: toolbar.contents().detach(), body: body.contents().detach()});
            pendingView = null;
            root.off("hero-mapping-content-change"); rowChangeHandler = null;
            const bindingActive = active;
            tab = next; updateTabs();
            if (next === "Binding") {
                bindingOrigin = previous === "Binding" ? bindingOrigin : previous;
                active = bindingActive; model = null; mode = "active";
                toolbar.empty(); body.empty();
                const access = bindingAccess(admin, active);
                if (access === "ADMIN_ONLY") { say("Bindings are available to administrators."); body.append($("<p></p>").text(text("Employee read-only view"))); }
                else if (access === "SELECT_DEFINITION") { say("Select a mapping definition in Query or Mutation to view its bindings."); }
                else showBindings(0);
                return;
            }
            const state = tabStates.get(next);
            if (state) {
                ({active, model, mode, selected, search, start, listing, revisionsStart, returnFocus, fieldFilter, rowChangeHandler} = state);
                toolbar.empty().append(state.toolbar); body.empty().append(state.body); say(state.status);
                if (rowChangeHandler) root.on("hero-mapping-content-change", rowChangeHandler);
                body.find("[data-field-picker]").trigger("hero-mapping-picker-resume");
                resumePendingView(state.pendingView, {list: loadList, detail: open});
            } else {
                active = null; model = null; mode = "active"; selected = 0; search = ""; start = 0; listing = null;
                revisionsStart = 0; returnFocus = null; fieldFilter = ""; loadList();
            }
        }
        updateTabs();
        const say = (value) => status.text(text(value));
        const focusHeading = () => root.find("h2").trigger("focus");
        const locked = () => busy || disposed;
        const exchangeContext = () => {
            const direction = active?.direction;
            if (!admin || active?.mapping_type !== "graphql" || !["QUERY_TO_FRAPPE", "FRAPPE_TO_MUTATION"].includes(direction) ||
                ![active?.source_instance, active?.schema_snapshot].every((value) => typeof value === "string" && value)) return null;
            return {source_instance: active.source_instance, snapshot_name: active.schema_snapshot, direction,
                key: `${active.source_instance}\u0000${active.schema_snapshot}\u0000${direction}`};
        };
        const exchangeLabel = (context = exchangeContext()) => context?.direction === "FRAPPE_TO_MUTATION" ? "Mutation" : "Query";
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
        function leaveWorkspace(action, cancel = () => {}) {
            if (sharedDirty) { say("Close or save the shared draft editor before leaving."); return cancel(); }
            if (otherDirty()) { say("Save or discard the unsaved changes in the other mapping tab before leaving."); return cancel(); }
            return leave(action, cancel);
        }
        function updateDirty() {
            root.find("[data-draft-state]").text(text(model?.dirty ? "Local changes — not saved" : "Saved personal draft"));
            root.trigger("hero-mapping-content-change");
        }
        async function loadList() {
            const request = ++epoch; pendingView = {kind: "list"}; ruleCache.clear(); active = null; model = null; mode = "active";
            rowChangeHandler = null; root.off("hero-mapping-content-change"); toolbar.empty(); body.empty(); say("Loading active mappings…");
            const filter = field("Search active definitions and rules", search, (value) => { search = value; }, toolbar).attr("maxlength", 120);
            const apply = () => { search = filter.val(); start = 0; loadList(); };
            filter.on("keydown", (event) => { if (event.key === "Enter") apply(); });
            toolbar.append(button("Search", apply));
            try {
                const result = await rpc("frontend_mapping_list", {search, start, page_size: 20, direction: directionForTab(tab)});
                if (disposed || request !== epoch) return;
                listing = result; renderList(); say("");
                if (returnFocus) { root.find("[data-mapping-name]").filter((_, el) => el.dataset.mappingName === returnFocus).first().trigger("focus"); returnFocus = null; }
            } catch (error) { if (request === epoch && !disposed) { say(errorText(error)); body.append(button("Retry", loadList)); } }
            finally { if (request === epoch) pendingView = null; }
        }
        function renderSchemaExplorer() {
            if (!admin) return;
            let viewEpoch = epoch, request = 0, suggestionRequest = 0, packageRequest = 0;
            const viewTab = tab, section = $("<details class='hero-mapping-schema-explorer' data-field-picker></details>").append($('<summary></summary>').text(text('Inspect saved schema')));
            const snapshot = $("<select class='form-control' aria-label='Saved schema snapshot'></select>");
            const operations = $("<select class='form-control' aria-label='Schema operation'></select>");
            const info = $("<p role='status'></p>"), limitNotice = $("<p role='status'></p>"), details = $('<pre></pre>');
            const source = $("<input class='form-control' aria-label='HERO source path'>"), target = $("<input class='form-control' aria-label='Target DocType (optional)'>");
            const currentView = (number) => !disposed && epoch === viewEpoch && tab === viewTab && number === request && section[0].isConnected;
            const resetOperations = () => operations.empty().append($('<option></option>').val('').text(text('Choose an operation'))).prop('disabled', true);
            const resetSnapshots = () => snapshot.empty().append($('<option></option>').val('').text(text('Choose a validated snapshot'))).prop('disabled', true);
            resetSnapshots(); resetOperations();
            const suggestions = button('Show field suggestions', async () => {
                const snapshotName = snapshot.val(), operation = operations.val(), sourcePath = source.val().trim();
                if (!snapshotName || !operation || !sourcePath || snapshot.prop('disabled') || operations.prop('disabled')) return info.text(text('Choose a snapshot and query operation and enter a source path.'));
                const serial = request, suggestionSerial = ++suggestionRequest; info.text(text('Loading suggestions…'));
                try {
                    const result = await rpc('mapping_suggestions', {snapshot_name: snapshotName, operation, source_path: sourcePath, target_doctype: target.val().trim()});
                    if (currentView(serial) && suggestionSerial === suggestionRequest) { details.text(JSON.stringify(result, null, 2)); info.text(text('Suggestions are read only.')); }
                } catch (error) { if (currentView(serial) && suggestionSerial === suggestionRequest) info.text(errorText(error)); }
            });
            async function loadOperations(name, preferred = '') {
                const serial = ++request; suggestionRequest++; section.trigger('hero-mapping-candidate-invalidate'); resetOperations(); details.empty();
                if (!name) { info.text(text('Choose a validated snapshot.')); return; }
                info.text(text('Loading schema operations…'));
                try {
                    const result = await rpc('schema_operation_catalog', {snapshot_name: name, operation_kind: viewTab === 'Query' ? 'QUERY' : 'MUTATION'});
                    if (!currentView(serial)) return;
                    result.forEach((item) => operations.append($('<option></option>').val(item.operation_name).text(`${item.operation_name} → ${item.return_type || 'unknown'}`)));
                    operations.prop('disabled', false); info.text(text('Choose an operation to inspect its schema.'));
                    operations.off('change.schema').on('change.schema', () => { suggestionRequest++; const item = result.find((row) => row.operation_name === operations.val()); details.text(item ? JSON.stringify(item, null, 2) : ''); info.text(text('Suggestions are read only; select Show field suggestions to load them.')); });
                    if (preferred && result.some((item) => item.operation_name === preferred)) operations.val(preferred).trigger('change');
                } catch (error) { if (currentView(serial)) info.text(errorText(error)); }
            }
            async function loadSnapshots(preferredSnapshot = '', preferredOperation = '') {
                const serial = ++request; suggestionRequest++; section.trigger('hero-mapping-candidate-invalidate'); resetSnapshots(); resetOperations(); details.empty();
                info.text(text('Loading saved snapshots…')); limitNotice.empty();
                try {
                    const result = await readSnapshotPages((args) => rpc('schema_snapshot_list', args), () => currentView(serial));
                    if (!result) return;
                    limitNotice.text(text('Snapshots can change while pages load. Retry saved schema to refresh the list.'));
                    const valid = result.filter((row) => row.analysis_status === 'Validated');
                    if (preferredSnapshot && !valid.some((row) => row.name === preferredSnapshot)) {
                        limitNotice.text(`${limitNotice.text()} ${text('The previously selected snapshot is not available in this result. Select a validated snapshot again.')}`.trim());
                    }
                    valid.forEach((row) => snapshot.append($('<option></option>').val(row.name).text(row.name)));
                    snapshot.prop('disabled', false); info.text(text('Choose a validated snapshot.'));
                    if (preferredSnapshot && valid.some((row) => row.name === preferredSnapshot)) {
                        snapshot.val(preferredSnapshot); await loadOperations(preferredSnapshot, preferredOperation);
                        if (!disposed && epoch === viewEpoch && tab === viewTab && section[0].isConnected && snapshot.val() === preferredSnapshot)
                            section.trigger('hero-mapping-refresh-shared');
                    }
                } catch (error) { if (currentView(serial)) { limitNotice.text(text('The snapshot list could not be loaded completely. Retry before choosing a snapshot.')); info.text(errorText(error)); } }
            }
            snapshot.on('change', () => { packageRequest++; packagePreview.empty(); loadOperations(snapshot.val()); });
            source.add(target).on('input', () => { suggestionRequest++; details.empty(); info.text(text('Select Show field suggestions to load the current inputs.')); });
            section.append(snapshot, operations, info, limitNotice, details);
            if (viewTab === 'Query') {
                section.append(source, target, suggestions);
                let candidateSerial = 0;
                const candidateSource = $("<input class='form-control' aria-label='Legacy preview HERO source path'>");
                const candidateDoctype = $("<input class='form-control' aria-label='Legacy preview target DocType'>");
                const candidateField = $("<input class='form-control' aria-label='Legacy preview target field'>");
                const candidateSample = $("<textarea class='form-control' aria-label='Legacy preview synthetic JSON record'></textarea>");
                const candidateState = $("<p role='status'></p>"), candidateResult = $("<pre></pre>");
                const clearCandidate = () => { candidateSerial++; candidateResult.empty(); candidateState.text(text('Enter inputs and run the legacy preview.')); };
                snapshot.add(operations).on('change', clearCandidate);
                candidateSource.add(candidateDoctype).add(candidateField).add(candidateSample).on('input', clearCandidate);
                section.on('hero-mapping-picker-resume hero-mapping-candidate-invalidate', clearCandidate);
                const preview = button('Preview synthetic record (legacy)', async () => {
                    let args;
                    try {
                        if (snapshot.prop('disabled') || operations.prop('disabled')) throw new Error('Choose a validated snapshot and query.');
                        args = legacyCandidateArgs(snapshot.val(), operations.val(), candidateSource.val(), candidateDoctype.val(), candidateField.val(), candidateSample.val());
                    } catch (error) { candidateResult.empty(); candidateState.text(text(error.message)); return; }
                    const serial = ++candidateSerial, viewRequest = request;
                    candidateResult.empty(); candidateState.text(text('Loading legacy preview…'));
                    const current = () => currentView(viewRequest) && serial === candidateSerial;
                    try {
                        const result = await legacyCandidateRpc(args);
                        if (!current()) return;
                        let display;
                        try { display = legacyCandidatePreviewText(result, args); }
                        catch (error) { candidateState.text(text(error.message)); return; }
                        candidateState.text(text('Read-only legacy identity preview. No mapping or document was saved.'));
                        candidateResult.text(display);
                    } catch (error) { if (current()) candidateState.text(text(error?.message === 'This browser cannot preserve preview numbers.' ? error.message : errorText(error))); }
                });
                section.append($('<h4></h4>').text(text('Legacy synthetic record preview')),
                    $('<p></p>').text(text('Uses only historical approved source paths and the Identity transformation. This does not preview personal draft rules, save a mapping, or contact HERO. Enter synthetic data only.')),
                    $('<label></label>').text(text('HERO source path')).append(candidateSource),
                    $('<label></label>').text(text('Target DocType')).append(candidateDoctype),
                    $('<label></label>').text(text('Target field')).append(candidateField),
                    $('<label></label>').text(text('Synthetic JSON record')).append(candidateSample), preview, candidateState, candidateResult);
            }
            if (viewTab === 'Mutation') {
                let legacyRequest = 0, detailSerial = 0;
                const legacyState = $("<p role='status'></p>");
                const legacyRows = $("<div></div>");
                const legacyDetail = $("<div></div>");
                const currentLegacy = (serial, name) => legacyWritebackCurrent(admin, viewTab, tab, viewEpoch, epoch,
                    serial, legacyRequest, name, snapshot.val(), section[0].isConnected, disposed);
                const showSharedEditor = (doc, catalog, name, serial, row = null) => {
                    if (locked() || root.find('dialog').length || !currentLegacy(serial, name)) return;
                    const creating = !doc, opener = document.activeElement, dialog = $("<dialog class='hero-mapping-dialog'></dialog>");
                    const heading = $('<h3></h3>').text(text(creating ? 'Create shared writeback draft' : 'Edit shared writeback draft'));
                    dialog.attr('aria-label', text(creating ? 'Create shared writeback draft' : 'Edit shared writeback draft')).append(heading,
                        $('<p></p>').text(text('This is a shared draft, separate from personal drafts. Saving never activates it.')));
                    const state = $("<p role='status'></p>"), choices = $("<select class='form-control' aria-label='Reviewed shared source'></select>");
                    const candidates = creating ? catalog.fields.filter((item) => item.selectable === true && item.target) : [];
                    if (creating) {
                        choices.append($('<option></option>').val('').text(text('Choose a reviewed source and target')));
                        candidates.forEach((item, index) => choices.append($('<option></option>').val(String(index)).text(`${item.doctype}.${item.path} → ${reviewedTargetLabel(item)}`)));
                        dialog.append($('<label></label>').text(text('Reviewed source and target')).append(choices));
                    } else dialog.append($('<p></p>').text(`${text('Shared draft')}: ${doc.name} · ${text('Status')}: ${doc.status} · ${text('Revision')}: ${doc.mapping_revision} · ${text('Last editor')}: ${doc.modified_by}`));
                    const initial = doc ? JSON.parse(doc.mapping_json) : null;
                    const transform = $("<select class='form-control' aria-label='Shared draft transformation'><option value='Identity'>Identity</option><option value='Trim'>Trim</option></select>").val(initial?.transformation || 'Identity');
                    const reason = $("<textarea class='form-control' aria-label='Shared draft review reason' maxlength='1000'></textarea>").val(doc?.suggestion_reason || '');
                    const confidence = $("<input class='form-control' type='number' min='0' max='1' step='any' aria-label='Shared draft confidence'>").val(doc?.confidence ?? 0);
                    const initialReason = String(reason.val()), initialConfidence = String(confidence.val()), initialTransform = transform.val();
                    let consumed = false;
                    const update = () => { sharedDirty = creating ? Boolean(choices.val() || reason.val() || Number(confidence.val()) !== 0 || transform.val() !== 'Identity') :
                        reason.val() !== initialReason || confidence.val() !== initialConfidence || transform.val() !== initialTransform; };
                    choices.add(transform).add(reason).add(confidence).on('input change', update);
                    const close = () => { sharedDirty = false; dialog[0].close(); dialog.remove(); if (opener?.isConnected) opener.focus(); };
                    const cancel = () => { if (!sharedDirty || window.confirm(text('Discard unsaved shared draft changes?'))) close(); };
                    dialog.on('cancel', (event) => { event.preventDefault(); if (!busy) cancel(); });
                    const saveShared = button(creating ? 'Create shared draft' : 'Save shared draft', async () => {
                        if (consumed || locked() || !currentLegacy(serial, name)) return;
                        update(); if (!creating && !sharedDirty) { state.text(text('No changes to save.')); return; }
                        let args;
                        try {
                            const choice = creating && choices.val() !== '' ? candidates[Number(choices.val())] : null;
                            args = sharedWritebackRequest({doc, row, actor:frappe.session?.user, catalog, snapshot:name, choice,
                                transformation:transform.val(), reason:reason.val(), confidence:String(confidence.val())});
                            if (!args) { state.text(text('No changes to save.')); return; }
                        } catch (error) { state.text(text(error.message)); return; }
                        consumed = true;
                        const disabled = root.find('button,input,select,textarea').map((_, el) => ({el, value:el.disabled})).get();
                        setBusy(true); state.text(text('Saving shared draft…'));
                        let saved = false;
                        try {
                            const result = await rpc('graphql_mapping_save', args, true);
                            if (!currentLegacy(serial, name)) return;
                            if (!sharedWritebackSaveAccepted(result, doc)) throw new Error('Uncertain shared draft result.');
                            close(); saved = true;
                        } catch (_) {
                            if (currentLegacy(serial, name)) state.text(text('Save failed or its outcome is unknown. Status, owner or revision may have changed. Your inputs are kept; close this editor and reload before trying again. No retry was made.'));
                        } finally {
                            await finishSharedWritebackSave(saved, () => currentLegacy(serial, name), () => {
                                if (!disposed) { setBusy(false); disabled.forEach(({el,value}) => { if (el.isConnected) el.disabled = value; }); if (dialog[0].isConnected && consumed) saveShared.prop('disabled', true); }
                            }, loadLegacy);
                        }
                    }, true);
                    dialog.append($('<label></label>').text(text('Transformation')).append(transform), $('<label></label>').text(text('Review reason')).append(reason),
                        $('<label></label>').text(text('Confidence 0–1')).append(confidence), state, saveShared, button('Cancel', cancel));
                    root.append(dialog); dialog[0].showModal();
                };
                const loadLegacy = async () => {
                    const serial = ++legacyRequest, name = snapshot.val();
                    detailSerial++; legacyRows.empty(); legacyDetail.empty();
                    if (!name || snapshot.prop('disabled')) { legacyState.text(text('Choose a validated snapshot to inspect shared writeback mappings.')); return; }
                    legacyState.text(text('Loading shared writeback mappings…'));
                    try {
                        const result = await rpc('graphql_mapping_list', {snapshot_name: name, operation_kind: 'MUTATION'});
                        if (!currentLegacy(serial, name)) return;
                        const rows = legacyWritebackRows(result);
                        legacyState.text(`${text('Shared writeback mappings')}: ${rows.length}. ${text('This list is read only and is separate from personal drafts.')}`);
                        rows.forEach((row) => {
                            const line = $('<p></p>').text(`${row.name} · ${row.operation_name} · ${row.status} · ${text('Revision')}: ${row.mapping_revision} · ${text('Reason')}: ${row.suggestion_reason || '—'} · ${text('Confidence')}: ${row.confidence ?? '—'} `);
                            line.append(button('Inspect shared mapping', async () => {
                                const detailRequest = ++detailSerial; legacyDetail.empty().text(text('Loading shared mapping…'));
                                try {
                                    const response = await frappe.call({method: 'frappe.client.get', type: 'GET', args: {doctype: 'HERO GraphQL Mapping', name: row.name}});
                                    if (!currentLegacy(serial, name) || detailRequest !== detailSerial) return;
                                    if (response?.exc) throw response;
                                    const doc = response?.message;
                                    if (!legacyWritebackDetailMatches(doc, row, name)) {
                                        legacyDetail.text(text('Shared mapping changed. Refresh the list before inspecting it.')); return;
                                    }
                                    legacyDetail.empty().append($('<p></p>').text(`${text('Last editor')}: ${doc.modified_by || '—'} · ${text('Definition ID')}: ${doc.definition_id || '—'}`),
                                        $('<p></p>').text(text('Stored mapping JSON:')), $('<pre></pre>').text(doc.mapping_json));
                                    const access = sharedWritebackDraftAccess(doc, row, name, frappe.session?.user);
                                    if (access !== 'EDITABLE') return legacyDetail.append($('<p></p>').text(text(access === 'OWNER_CONFLICT' ? 'Only the last editor can change this shared draft.' : 'Only a current shared draft can be edited.')));
                                    const catalog = await rpc('writeback_mapper_catalog', {snapshot_name:name});
                                    if (!currentLegacy(serial, name) || detailRequest !== detailSerial) return;
                                    if (!reviewedWritebackEditable(doc.mapping_json, catalog)) return legacyDetail.append($('<p></p>').text(text('This shared draft remains read only because editing could discard parameters or change JSON tokens.')));
                                    legacyDetail.append(button('Edit my shared draft', () => showSharedEditor(doc, catalog, name, serial, row)));
                                } catch (error) { if (currentLegacy(serial, name) && detailRequest === detailSerial) legacyDetail.text(errorText(error)); }
                            }));
                            legacyRows.append(line);
                        });
                    } catch (error) { if (currentLegacy(serial, name)) legacyState.text(errorText(error)); }
                };
                snapshot.on('change', loadLegacy);
                section.on('hero-mapping-refresh-shared', loadLegacy);
                section.on('hero-mapping-picker-resume', () => { legacyRequest++; detailSerial++; legacyRows.empty(); legacyDetail.empty(); legacyState.text(text('Choose a validated snapshot again to refresh shared mappings.')); });
                section.append($('<h4></h4>').text(text('Shared writeback mappings')),
                    $('<p></p>').text(text('Shared mappings are separate from personal drafts. Inspection is read only; it does not establish activation eligibility or publish a personal draft.')),
                    legacyState, legacyRows, legacyDetail, button('Refresh shared writeback mappings', loadLegacy),
                    button('Prepare new shared writeback draft', async () => {
                        const serial = legacyRequest, name = snapshot.val();
                        if (!name) { legacyState.text(text('Choose a validated snapshot first.')); return; }
                        if (!currentLegacy(serial, name) || locked()) return;
                        try {
                            const catalog = await rpc('writeback_mapper_catalog', {snapshot_name:name});
                            if (currentLegacy(serial, name)) showSharedEditor(null, catalog, name, serial);
                        } catch (error) { if (currentLegacy(serial, name)) legacyState.text(errorText(error)); }
                    }));
            }
            const packagePreview = $("<div class='hero-mapping-package-preview'></div>");
            const packageState = $("<p role='status'></p>");
            const fileInput = $("<input type='file' accept='application/json,.json' aria-label='Mapping package'>");
            const packageCurrent = (serial, name) => !disposed && !busy && epoch === viewEpoch && tab === viewTab &&
                section[0].isConnected && serial === packageRequest && name === snapshot.val() && !snapshot.prop('disabled');
            fileInput.on('change', async () => {
                const file = fileInput[0].files?.[0], name = snapshot.val(); fileInput.val('');
                if (!file || !name || locked() || snapshot.prop('disabled')) { packageState.text(text('Choose a validated snapshot and a package.')); return; }
                const serial = ++packageRequest; packagePreview.empty(); packageState.text(text('Loading package preview…'));
                try {
                    const payload = await file.text();
                    if (!packageCurrent(serial, name)) return;
                    const preview = await rpc('preview_mapping_package', {snapshot_name: name, payload_json: payload}, true);
                    if (!packageCurrent(serial, name)) return;
                    if (!Array.isArray(preview?.items) || !/^[a-f0-9]{64}$/.test(preview.preview_revision || '')) throw new Error('Invalid package preview.');
                    const selected = new Set();
                    let submitted = false;
                    const update = () => packageState.text(`${text('Preview only; selected changes')}: ${selected.size}.`);
                    const choices = $('<div></div>');
                    preview.items.forEach((item) => {
                        const check = $("<input type='checkbox'>").prop('disabled', item.selectable !== true);
                        check.on('change', () => { if (check.prop('checked')) selected.add(item.id); else selected.delete(item.id); update(); });
                        choices.append($('<label class="d-block"></label>').append(check, document.createTextNode(
                            ` ${item.id} · ${item.group}: ${item.state} · ${item.label} · ${item.detail || ''} · ${text('Dependencies')}: ${(item.dependencies || []).join(', ') || '—'}`)));
                    });
                    const apply = button('Review selected adoption', () => {
                        if (submitted || !packageCurrent(serial, name)) return;
                        let chosen;
                        try { chosen = legacyPackageSelection(preview, [...selected]); }
                        catch (error) { packageState.text(text(error.message)); return; }
                        const ids = chosen.map((item) => item.id), mappings = chosen.filter((item) => item.kind === 'mapping').length;
                        modal('Confirm package adoption', `${text('Apply')} ${ids.length} ${text('selected changes')}. ${text('Selected schema changes may create Frappe schema and selected field mappings will be activated')}: ${mappings}. ${text('Schema changes may not be reversible. No HERO data migration is started.')}`, [
                            ['Confirm selected adoption', async () => {
                                if (submitted || !packageCurrent(serial, name)) return;
                                submitted = true;
                                const disabled = root.find('button,input,select,textarea').map((_, el) => ({el, value: el.disabled})).get();
                                setBusy(true); packagePreview.empty(); packageState.text(text('Applying selected adoption…'));
                                try {
                                    const outcome = await rpc('adopt_mapping_package', legacyPackageAdoptionArgs(name, payload, preview, ids), true);
                                    if (outcome?.status !== 'COMPLETED') throw new Error('Adoption outcome is incomplete.');
                                    if (!disposed && epoch === viewEpoch && tab === viewTab && serial === packageRequest && section[0].isConnected) {
                                        packageState.text(text('Adoption completed. Review the result.'));
                                        packagePreview.append(safeJson(outcome));
                                    }
                                } catch (error) {
                                    if (!disposed && epoch === viewEpoch && tab === viewTab && serial === packageRequest && section[0].isConnected)
                                        packageState.text(`${text('Adoption failed or its outcome is unknown. Schema changes may already exist. Check whether an adoption record exists and inspect the schema before starting a new preview; no retry was made.')} ${text('Submitted selection')}: ${ids.join(', ')}.`);
                                } finally {
                                    if (!disposed) { setBusy(false); disabled.forEach(({el, value}) => { if (el.isConnected) el.disabled = value; }); }
                                }
                            }, true], ['Cancel', () => {}],
                        ]);
                    });
                    packagePreview.append(choices, apply); update();
                } catch (error) { if (packageCurrent(serial, name)) packageState.text(errorText(error)); }
            });
            const exportPackage = button('Export saved field mapping package', async () => {
                const name = snapshot.val(); if (!name || locked() || snapshot.prop('disabled')) return packageState.text(text('Choose a validated snapshot.'));
                const serial = ++packageRequest; packagePreview.empty(); packageState.text(text('Exporting saved field mapping package…'));
                try {
                    const result = await rpc('export_mapping_json', {snapshot_name: name});
                    if (!packageCurrent(serial, name)) return;
                    downloadExport(result.filename, result.content); packageState.text(text('Saved field mapping package exported for this snapshot fingerprint, including Active, Draft and Disabled mappings. Personal drafts are not included.'));
                } catch (error) { if (packageCurrent(serial, name)) packageState.text(errorText(error)); }
            });
            section.append($('<h4></h4>').text(text('Saved field mapping package')),
                $('<p></p>').text(text('Export includes saved global field mappings for this snapshot fingerprint, including Active, Draft and Disabled mappings. Personal drafts are not included.')),
                $('<p></p>').text(text('Select changes explicitly. Adoption may create schema and activates selected field mappings; it does not migrate HERO data.')),
                fileInput, exportPackage, packageState, packagePreview);
            section.append(button('Retry saved schema', () => { packageRequest++; packagePreview.empty(); loadSnapshots(snapshot.val(), operations.val()); }));
            section.append(button('Open Graph Viewer', () => {
                const name = snapshot.val(); if (!name || snapshot.prop('disabled')) return info.text(text('Choose a validated snapshot.'));
                leaveWorkspace(() => window.hero_integration_ui.setRoute('api', {hero_tool: 'graph', snapshot: name}));
            }));
            section.on('hero-mapping-picker-resume', () => {
                const name = snapshot.val() || '', operation = operations.val() || '';
                packageRequest++; packagePreview.empty(); packageState.text(text('Choose a package again to preview it.'));
                viewEpoch = epoch; loadSnapshots(name, operation);
            });
            body.append(section); loadSnapshots();
        }
        function renderList() {
            body.empty();
            const p = listing.pagination;
            const direction = directionForTab(tab);
            const definitions = listing.graphql_mappings.filter((item) => item.direction === direction);
            const groups = new Map();
            definitions.forEach((item) => { const key = `${item.operation_kind} ${item.operation_name}`; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(item); });
            body.append($("<h3></h3>").text(text("HERO operations and mapping definitions")),
                $("<p class='text-muted'></p>").text(`${text("Definitions in this direction on this page")}: ${definitions.length} / ${p.graphql_total}`));
            if (!definitions.length) body.append($("<p></p>").text(text(search ? "No active definitions match your search." : "No active mapping definitions in this direction.")));
            groups.forEach((items, label) => {
                const group = $("<details open class='hero-mapping-operation'></details>").append($("<summary></summary>").text(label));
                items.forEach((item) => group.append(button(`${item.name} · ${text("Active")} · ${text("Revision")} ${item.revision}`, () => open(item, "graphql"))
                    .attr("data-mapping-name", item.name)));
                body.append(group);
            });
            if (tab === "Query") body.append($("<h3></h3>").text(text("Active field rules")), $("<p class='text-muted'></p>").text(`${text("Rules on this page")}: ${listing.frappe_mappings.length} / ${p.frappe_total}`));
            if (tab === "Query") listing.frappe_mappings.forEach((item) => body.append(button(`${item.operation} · ${item.source_path} → ${item.target_doctype}.${item.target_field}`, () => open(item, "frappe"))));
            if (tab === "Query" && !listing.frappe_mappings.length) body.append($("<p></p>").text(text(search ? "No active field rules match your search." : "No active field rules.")));
            const pager = $("<div class='hero-mapping-actions'></div>");
            pager.append(button("Previous page", () => { start = Math.max(0, start - 20); loadList(); }).prop("disabled", start === 0),
                $("<span></span>").text(`${text("Page")} ${Math.floor(start / 20) + 1}`),
                button("Next page", () => { start += 20; loadList(); }).prop("disabled", start + 20 >= Math.max(p.graphql_total, p.frappe_total) || start >= 10000));
            body.append(pager);
            renderSchemaExplorer();
        }
        async function open(item, kind) {
            if (locked()) return;
            const request = ++epoch; pendingView = {kind: "detail", item, mappingType: kind}; ruleCache.clear(); body.empty(); toolbar.empty(); say("Loading active definition…");
            toolbar.append(button("Back to mappings", () => leave(loadList)));
            try {
                const detail = await rpc("frontend_mapping_detail", {mapping_type: kind, name: item.name});
                if (disposed || request !== epoch) return;
                active = {...detail, mapping_type: kind}; mode = "active"; model = null; selected = 0; fieldFilter = ""; returnFocus = item.name;
                renderDetail(); say(""); focusHeading();
            } catch (error) { if (request === epoch && !disposed) say(errorText(error)); }
            finally { if (request === epoch) pendingView = null; }
        }
        function content() { return mode === "draft" ? model.content : active.definition || active; }
        function exchangePreviewDialog(entry, originalOpener = document.activeElement) {
            if (root.find("dialog").length) return;
            const opener = originalOpener, preview = entry.preview, counts = exchangeCounts(preview);
            const canAdopt = () => exchangeCanAdopt(entry, exchanges.get(entry.context.key), exchangeContext()?.key, locked());
            const dialog = $("<dialog class='hero-mapping-dialog'></dialog>");
            const heading = $("<h3 id='hero-mapping-exchange-title'></h3>").text(text(`Import ${exchangeLabel(entry.context)} mappings`));
            let closed = false;
            const close = () => {
                if (closed) return;
                closed = true;
                if (dialog[0].open) dialog[0].close();
                dialog.remove();
                const replacement = exchangeContext()?.key === entry.context.key
                    ? root.find("[data-import-direction]").filter((_, el) => el.dataset.importDirection === entry.context.direction)[0] : null;
                const usable = (element) => element?.isConnected && !element.disabled && element.tabIndex >= 0 && element.getClientRects().length;
                const target = usable(opener) ? opener : usable(replacement) ? replacement : root.find("h2")[0];
                if (target?.isConnected && !target.disabled) target.focus();
            };
            dialog.on("cancel", (event) => { event.preventDefault(); close(); });
            dialog.attr("aria-labelledby", "hero-mapping-exchange-title").append(heading,
                $("<p></p>").text(text(`Preview only — no personal drafts or schema were changed. Only ${exchangeLabel(entry.context)} mappings for this source and snapshot can be replaced.`)),
                $("<p></p>").text(`${text("Definitions")}: ${preview.items.length} · ${text("New")}: ${counts.NEW} · ${text("Changed")}: ${counts.REPLACE} · ${text("Unchanged")}: ${counts.UNCHANGED} · ${text("Removed")}: ${counts.REMOVE}`));
            const items = $("<table class='table table-sm hero-mapping-table'><thead><tr><th>Definition</th><th>Result</th></tr></thead><tbody></tbody></table>");
            preview.items.forEach((item) => items.find("tbody").append($("<tr></tr>").append($("<td></td>").text(item.definition_id), $("<td></td>").text(item.state))));
            dialog.append(items);
            const schema = preview.schema?.items || [];
            dialog.append($("<details></details>").append($("<summary></summary>").text(`${text("Schema provisioning preview")} (${schema.length})`), safeJson(preview.schema || {})));
            if (!preview.can_save) dialog.append($("<p role='alert'></p>").text(text("This package cannot be saved because the schema preview has a conflict or invalid definition.")));
            const adopt = async () => {
                const adoptEntry = () => { if (!canAdopt()) return; exchanges.set(entry.context.key, {...entry, adopted: true}); say(`Imported ${exchangeLabel(entry.context)} package adopted locally. Save imported ${exchangeLabel(entry.context)} drafts to persist this direction.`); renderDetail(); };
                if (!model?.dirty) return adoptEntry();
                modal("Unsaved changes", "Adopting this directional package replaces the local working draft collection. Save or discard the current local edits first.", [
                    ["Save and adopt", async () => { if (await save()) adoptEntry(); }, true],
                    ["Discard local edits and adopt", () => { model.content = clone(model.saved.content); model.undo = []; adoptEntry(); }],
                    ["Continue editing", () => {}],
                ]);
            };
            dialog.append(button("Adopt locally", async () => { if (!canAdopt()) return; close(); await adopt(); }, true).prop("disabled", !canAdopt()),
                button("Close", close));
            root.append(dialog); dialog[0].showModal(); dialog.find("button").first().trigger("focus");
        }
        async function previewImport(file, opener) {
            const context = exchangeContext();
            if (locked() || !context || !file) return;
            const request = ++epoch, sequence = ++exchangeSequence;
            let entry;
            setBusy(true); say(`Reading ${exchangeLabel(context)} import file…`);
            try {
                const payload = await file.text();
                if (disposed || request !== epoch || context.key !== exchangeContext()?.key) return;
                const preview = await rpc("mapping_draft_exchange_preview", {payload_json: payload,
                    source_instance: context.source_instance, snapshot_name: context.snapshot_name, direction: context.direction}, true);
                if (disposed || request !== epoch || sequence !== exchangeSequence || context.key !== exchangeContext()?.key) return;
                entry = {context, payload, preview, adopted: false};
                exchanges.set(context.key, entry); say("Import preview is ready. No personal drafts or schema were changed.");
            } catch (error) {
                if (!disposed && request === epoch) say(denied(error) ? errorText(error) : "The import preview failed. Your current work and saved drafts have been kept.");
            } finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); if (entry) exchangePreviewDialog(entry, opener); } }
        }
        function chooseImport() {
            const context = exchangeContext();
            if (locked() || !context) return;
            const opener = document.activeElement;
            const input = $("<input type='file' accept='application/json,.json' class='sr-only'>");
            input.on("change", () => { const file = input[0].files?.[0]; input.remove(); if (file) previewImport(file, opener); });
            root.append(input); input.trigger("click");
        }
        function downloadExport(filename, content) {
            if (typeof filename !== "string" || !filename || typeof content !== "string") throw new Error("Invalid directional export response.");
            const href = URL.createObjectURL(new Blob([content], {type: "application/json"}));
            const link = document.createElement("a"); link.href = href; link.download = filename; link.hidden = true;
            root[0].append(link);
            try { ownedDownload = link; link.click(); }
            finally { ownedDownload = null; link.remove(); setTimeout(() => URL.revokeObjectURL(href), 1000); }
        }
        async function exportDirection() {
            const context = exchangeContext();
            if (locked() || !context) return;
            const request = ++epoch; setBusy(true); say(`Exporting saved ${exchangeLabel(context)} drafts…`);
            try {
                const result = await rpc("mapping_draft_exchange_export", {source_instance: context.source_instance,
                    snapshot_name: context.snapshot_name, direction: context.direction});
                if (disposed || request !== epoch || context.key !== exchangeContext()?.key) return;
                downloadExport(result.filename, result.content);
                say(`Saved ${exchangeLabel(context)} drafts exported. Local unsaved edits were not included.`);
            } catch (error) { if (!disposed && request === epoch) say(errorText(error)); }
            finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); } }
        }
        async function saveImportedDirection() {
            const context = exchangeContext(), entry = context && exchanges.get(context.key);
            if (locked() || !entry?.adopted || !entry.preview?.can_save) return;
            const request = ++epoch, expectedStates = exchangeExpectedStates(entry.preview);
            setBusy(true); say(`Saving imported ${exchangeLabel(context)} drafts…`);
            try {
                const result = await rpc("mapping_draft_exchange_save", {payload_json: entry.payload,
                    source_instance: context.source_instance, snapshot_name: context.snapshot_name, direction: context.direction,
                    expected_states_json: JSON.stringify(expectedStates), confirmed: true}, true);
                if (disposed || request !== epoch || context.key !== exchangeContext()?.key) return;
                exchanges.delete(context.key); model = null; mode = "active";
                const created = result.schema?.created || {};
                const createdCount = Object.values(created).flat().length;
                say(`Imported ${exchangeLabel(context)} drafts saved. ${createdCount ? `${createdCount} schema element(s) created.` : "Existing schema was reused; no schema elements were created."} Active mappings were not changed.`);
                ruleCache.clear();
            } catch (error) {
                if (!disposed && request === epoch) say("Import save failed or conflicted. The imported local package and current editor context have been kept. Check the saved drafts before retrying.");
            } finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); } }
        }
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
                const context = exchangeContext(), imported = context && exchanges.get(context.key);
                if (context) {
                    toolbar.append(button(`Import ${exchangeLabel(context)} mappings`, chooseImport).attr("data-import-direction", context.direction),
                        button(`Export ${exchangeLabel(context)} mappings`, exportDirection));
                    if (imported) toolbar.append(button(`Review imported ${exchangeLabel(context)} package`, () => exchangePreviewDialog(imported)),
                        button(`Save imported ${exchangeLabel(context)} drafts (${imported.preview.items.length})`, saveImportedDirection, true)
                            .prop("disabled", !imported.adopted || !imported.preview.can_save));
                }
                if (mode === "draft") {
                    toolbar.append(button("Compare active changes", compareActiveChanges).prop("disabled", Boolean(model.detailStale)), button("Save draft", save, true).attr("data-save-draft", ""), button("Discard saved draft", discard));
                    const reconciliationState = model.reconciliation ? "Local reconciliation changes — not saved"
                        : model.saved.reconciliation_json ? "Saved reconciliation evidence is current only for its recorded draft content."
                            : "No saved reconciliation evidence.";
                    body.append($("<p data-draft-state></p>"), $("<p class='text-muted'></p>").text(`${text("Personal draft revision")}: ${model.saved.content_revision} · ${text("Generation")}: ${model.saved.generation}`),
                        $("<p class='text-muted' data-reconciliation-state></p>").text(text(reconciliationState)));
                    if (model.blocked) toolbar.append(button("Check saved draft", reconcile), button("Reload saved draft", reloadDraft));
                }
            }
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
                    const target = row.reference ? (linked ? `${linked.target_doctype}.${linked.target_field}` : text("Existing active field rule"))
                        : mutationMapping(v) ? `${v.target?.operation || ""} / ${v.target?.argument || ""}.${v.target?.field_path ?? v.target?.field ?? ""}`
                            : `${v.target?.doctype || ""}.${v.target?.field_path ?? v.target?.field ?? ""}`;
                    const rule = row.reference ? (linked?.transformation || text("Referenced rule")) : v.transformation || v.transform || text("No transformation configured");
                    if (needle && !`${source} ${target} ${rule}`.toLowerCase().includes(needle)) return;
                    const tr = $("<tr></tr>").toggleClass("is-selected", selected === index);
                    tr.append($("<td></td>").append(button(source, () => { selected = index; drawRows(); renderRow(row, detail); body.find(".hero-mapping-context").trigger("hero-mapping-selection-change"); }).attr("data-row-selection", index)), $("<td></td>").text(target), $("<td></td>").text(typeof rule === "string" ? rule : text("Configured special rule")));
                    const actions = $("<td></td>");
                    if (editable) actions.append(button("Remove", () => { model.remove(row.index); selected = 0; renderDetail(); const next = root.find("[data-row-selection]").eq(Math.min(row.index, rows(model.content).length - 1)); if (next.length) next.trigger("focus"); else root.find("button").filter((_, el) => el.textContent.startsWith(text("Undo removal"))).trigger("focus"); say("Mapping removed locally. Undo is available until a successful save."); }));
                    tr.append(actions); table.find("tbody").append(tr);
                });
                if (list.length && !table.find("tbody").children().length) table.find("tbody").append($("<tr><td colspan='4'></td></tr>").find("td").text(text("No field mappings match this filter.")).end());
                body.find(".hero-mapping-context").trigger("hero-mapping-summary-refresh");
                if (focusedRow !== undefined) table.find(`[data-row-selection="${focusedRow}"]`).trigger("focus");
            };
            rowChangeHandler = drawRows;
            root.off("hero-mapping-content-change").on("hero-mapping-content-change", rowChangeHandler);
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
            if (model && !canSaveDraft(model, active)) root.find("[data-save-draft]").prop("disabled", true);
        }
        function renderContext(value) {
            const mutation = mutationMapping(value);
            const sourceTitle = mutation ? "Source Frappe" : "Source HERO";
            const targetTitle = mutation ? "Target HERO" : "Target Frappe";
            const frappeSide = mutation ? "source" : "target";
            const frappeValue = value[frappeSide] || {};
            const selectedApp = selectedPickerApp(model, frappeSide, frappeValue, admin);
            const sourceLines = mutation
                ? [["Installed app", selectedApp || "—"], ["DocType", value.source?.doctype || "—"], ["Field", value.source?.field || "—"]]
                : [["Validated schema snapshot", active.schema_snapshot || "—"], ["Query / operation", active.operation_name || value.operation_name || "—"], ["Object / path", value.records_path || value.record_path || value.source_path || "—"]];
            const targetLines = mutation
                ? [["Mutation", active.operation_name || value.target?.operation || "—"], ["Argument / input path", value.target?.argument || "—"], ["Input field", value.target?.field_path || value.target?.field || "—"]]
                : [["Installed app", selectedApp || "—"], ["DocType", value.target?.doctype || "—"], ["Field", value.target?.field_path || value.target?.field || value.target_field || "—"]];
            const context = $("<section class='hero-mapping-context' aria-label='Mapping context'></section>")
                .append($("<h4></h4>").text(text(mutation ? "Mutation mapping context" : "Query mapping context")));
            const pane = (title, lines) => {
                const section = $("<section class='hero-mapping-context-pane'></section>").append($("<h5></h5>").text(text(title)));
                lines.forEach(([label, value]) => section.append($("<p></p>").append($("<strong></strong>").text(`${text(label)}: `), document.createTextNode(String(value)))));
                return section;
            };
            const sourcePane = pane(sourceTitle, sourceLines), targetPane = pane(targetTitle, targetLines);
            if (Array.isArray(value.mapping_definition_ids)) {
                const heroPane = mutation ? targetPane : sourcePane;
                const frappePane = mutation ? sourcePane : targetPane;
                heroPane.append($('<p></p>').append($('<strong></strong>').text(`${text("Selected field path")}: `), document.createTextNode("—")),
                    $('<p></p>').append($('<strong></strong>').text(`${text("Selected field type")}: `), document.createTextNode("—")));
                const showLine = (pane, index, value) => pane.children("p").eq(index).contents().last().replaceWith(document.createTextNode(String(value || "—")));
                let referenceCatalog;
                const refreshSummary = () => {
                    const row = rows(content())[selected];
                    const rule = row?.reference ? ownRule(content(), row.id) || ruleCache.get(row.id) : null;
                    const type = rule?.source_type || "", path = rule?.source_path || "";
                    const doctype = rule?.target_doctype || "", field = rule?.target_field || "";
                    const savedApp = model?.referenceApps?.[row?.id];
                    const app = savedApp?.doctype === doctype ? savedApp.app : "";
                    showLine(heroPane, 3, path); showLine(heroPane, 4, type);
                    showLine(frappePane, 0, app); showLine(frappePane, 1, doctype); showLine(frappePane, 2, field);
                    if (!admin || !doctype || app || !row?.id || !context[0].isConnected) return;
                    const requestEpoch = epoch, rowId = row.id, definition = active, currentModel = model;
                    referenceCatalog ||= rpc("mapping_target_catalog", {}).catch(() => null);
                    referenceCatalog.then((catalog) => {
                        if (!catalog || disposed || epoch !== requestEpoch || active !== definition || model !== currentModel || !context[0].isConnected) return;
                        const selectedRow = rows(content())[selected];
                        const currentRule = selectedRow?.reference ? ownRule(content(), selectedRow.id) || ruleCache.get(selectedRow.id) : null;
                        if (selectedRow?.id !== rowId || currentRule?.target_doctype !== doctype) return;
                        const entry = catalog.doctypes.find((item) => item.doctype === doctype && item.selectable);
                        if (!entry) return;
                        if (model) { model.referenceApps ||= Object.create(null); model.referenceApps[rowId] = {app: entry.app_name, doctype}; }
                        showLine(frappePane, 0, entry.app_name);
                    });
                };
                context.on("hero-mapping-summary-refresh", refreshSummary);
            }
            if (admin && mode === "draft") {
                const currentRow = () => rows(model.content)[selected] || {};
                const available = () => !Array.isArray(model.content.mapping_definition_ids) || Boolean(ownRule(model.content, currentRow().id));
                const sourcePicker = button("Choose source fields", () => { if (available()) picker("source", sourcePane, currentRow()); });
                const targetPicker = button("Choose target fields", () => { if (available()) picker("target", targetPane, currentRow()); });
                const updatePickers = () => { const disabled = !available(); sourcePicker.prop("disabled", disabled); targetPicker.prop("disabled", disabled); };
                context.on("hero-mapping-selection-change", () => { sourcePane.find("[data-field-picker]").remove(); targetPane.find("[data-field-picker]").remove(); updatePickers(); });
                sourcePane.append(sourcePicker); targetPane.append(targetPicker); updatePickers();
            }
            context.append(sourcePane, targetPane);
            body.append(context);
            context.trigger("hero-mapping-summary-refresh");
            if (admin && !selectedApp && frappeValue.doctype && active.mapping_type === "graphql") {
                const requestEpoch = epoch, definition = active, currentModel = model;
                const selectedDoctype = frappeValue.doctype;
                const method = mutation ? "writeback_mapper_catalog" : "mapping_target_catalog";
                const args = mutation ? {snapshot_name: active.schema_snapshot, source_doctype: selectedDoctype} : {};
                rpc(method, args).then((result) => {
                    if (disposed || epoch !== requestEpoch || active !== definition || model !== currentModel || !context[0].isConnected || content()[frappeSide]?.doctype !== selectedDoctype) return;
                    const entry = result.doctypes.find((item) => item.doctype === selectedDoctype && item.selectable);
                    if (!entry) return;
                    if (model) model.frappeSelection = {side: frappeSide, app: entry.app_name, doctype: selectedDoctype};
                    const pane = mutation ? sourcePane : targetPane;
                    pane.find("p").first().contents().last().replaceWith(document.createTextNode(String(entry.app_name)));
                }).catch(() => { /* The picker will display a catalog error when opened. */ });
            }
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
        async function refreshDraftActive(state, request) {
            const selected = active;
            const detail = await rpc("frontend_mapping_detail", {mapping_type: "graphql", name: selected.name});
            if (disposed || request !== epoch || model !== state) return false;
            if (detail.name !== selected.name || detail.definition_id !== state.saved.definition_id || !Number.isInteger(detail.revision) || detail.revision < 1) {
                throw new Error("Active mapping identity changed.");
            }
            active = {...detail, mapping_type: "graphql"};
            state.detailStale = false;
            state.blocked = !hasCurrentDraftBase(state, active);
            return true;
        }
        async function openDraft() {
            leave(async () => {
                if (locked()) return; const request = ++epoch; setBusy(true); say("Loading your saved draft…");
                try {
                    const result = await rpc("mapping_draft_read", {definition_id: active.definition_id});
                    if (disposed || request !== epoch) return;
                    if (result.draft?.state === "OPEN") {
                        model = new DraftState(result.draft); mode = "draft"; model.detailStale = true; model.blocked = true;
                        if (await refreshDraftActive(model, request)) say(model.blocked ? "The active mapping changed. Compare active changes before saving." : "");
                    }
                    else modal("Create personal draft", "No open personal draft exists. Create one from the active definition?", [
                        ["Create my draft", createDraft, true], ["Cancel", () => {}],
                    ]);
                } catch (error) { say(model?.detailStale ? "The active mapping could not be refreshed. Return to mappings and reopen the draft before editing." : errorText(error)); }
                finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); } }
            });
        }
        async function createDraft() {
            if (locked()) return; setBusy(true); const request = ++epoch;
            try { const result = await rpc("mapping_draft_create", {definition_id: active.definition_id}, true);
                if (disposed || request !== epoch) return; model = new DraftState(result.draft); mode = "draft"; model.detailStale = true; model.blocked = true;
                if (await refreshDraftActive(model, request)) say(model.blocked ? "The active mapping changed. Compare active changes before saving." : "Personal draft opened.");
            } catch (error) { say(model?.detailStale ? "The active mapping could not be refreshed. Return to mappings and reopen the draft before editing." : errorText(error)); }
            finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); } }
        }
        async function save() {
            if (locked() || !canSaveDraft(model, active)) return false;
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
            if (model.detailStale) { setBusy(false); say("Return to mappings and reopen the draft to refresh the active definition."); renderDetail(); return; }
            try { const result = await rpc("mapping_draft_read", {definition_id: model.saved.definition_id});
                if (disposed || request !== epoch) return;
                if (model.resolve(result.draft, model.content)) say("Personal draft matches your edits. Save status confirmed.");
                else if (!model.blocked) say("Your changes are not saved. Choose Save draft to try again.");
                else say("The saved draft differs. Your local edits remain. Reload only if you want to replace them.");
                if (!hasCurrentDraftBase(model, active)) { model.blocked = true; say("The active mapping changed. Compare active changes before saving."); }
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
            const request = ++epoch; pendingView = {kind: "binding"};
            toolbar.empty().append(button("Back to definition", () => switchTab(bindingOrigin)));
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
            finally { if (request === epoch) pendingView = null; }
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
            panel.find("[data-field-picker]").remove();
            const pickerHost = $("<section data-field-picker></section>"); panel.append(pickerHost);
            let seq = 0, fieldStart = 0, frappeCatalog = null, fieldsPending = false, catalogPending = false;
            let pickerEpoch = epoch;
            const currentPicker = (request) => request === seq && pickerEpoch === epoch && pickerHost[0].isConnected;
            const query = field("Search fields by name, full path or type", "", () => { seq++; results.empty(); }, pickerHost);
            const pickerId = `hero-mapping-picker-${Math.random().toString(36).slice(2)}`;
            const app = $("<input class='form-control' aria-label='App' autocomplete='off'>").attr("list", `${pickerId}-apps`).prop("disabled", true);
            const doctype = $("<input class='form-control' aria-label='DocType' autocomplete='off'>").attr("list", `${pickerId}-doctypes`).prop("disabled", true);
            const appOptions = $("<datalist></datalist>").attr("id", `${pickerId}-apps`);
            const doctypeOptions = $("<datalist></datalist>").attr("id", `${pickerId}-doctypes`);
            const catalogState = $("<p class='text-muted'></p>");
            const contractState = $("<p class='text-muted' role='status'></p>");
            const results = $("<div></div>");
            const mutation = mutationMapping();
            const source = row.reference ? side === "target" : mutation ? side === "source" : side === "target";
            query.prop("disabled", source);
            pickerHost.append(app, appOptions, doctype, doctypeOptions, catalogState, contractState, results);
            const load = async () => {
                if (source && !doctype.val()) { results.text(text("Choose an app and DocType before searching fields.")); return; }
                const request = ++seq; fieldsPending = true; results.text(text("Loading fields…"));
                try {
                    let fields;
                    if (source && mutationMapping() && !row.reference) {
                        const available = (frappeCatalog?.fields || []).filter((field) => field.app_name === app.val() && field.doctype === doctype.val() && `${field.path} ${field.label} ${field.fieldtype} ${field.description || ""}`.toLowerCase().includes(query.val().toLowerCase()));
                        fields = available.slice(fieldStart, fieldStart + 50);
                        results.text(`${available.length ? fieldStart + 1 : 0}–${Math.min(fieldStart + fields.length, available.length)} / ${available.length}`);
                        results.append(button("Previous fields", () => { fieldStart = Math.max(0, fieldStart - 50); load(); }).prop("disabled", !fieldStart), button("Next fields", () => { fieldStart += 50; load(); }).prop("disabled", fieldStart + 50 >= available.length));
                    } else if (source) { const result = await rpc("mapping_target_catalog_page", {app_name: app.val() || "", doctype: doctype.val() || "", search: query.val(), start: fieldStart, page_size: 50}); fields = result.items; if (!currentPicker(request)) return; results.text(`${result.total ? fieldStart + 1 : 0}–${Math.min(fieldStart + fields.length, result.total)} / ${result.total}`); results.append(button("Previous fields", () => { fieldStart = Math.max(0, fieldStart - 50); load(); }).prop("disabled", !fieldStart), button("Next fields", () => { fieldStart += 50; load(); }).prop("disabled", fieldStart + 50 >= result.total)); }
                    else if (row.reference || !mutation) {
                        const result = await rpc("mapping_source_picker_page", {snapshot_name: active.schema_snapshot, operation: model.content.target_operation || active.operation_name,
                            search: query.val(), start: fieldStart, page_size: 50});
                        if (!currentPicker(request)) return;
                        fields = result.items; results.text(`${result.total ? fieldStart + 1 : 0}–${Math.min(fieldStart + fields.length, result.total)} / ${result.total}`);
                        results.append(button("Previous fields", () => { fieldStart = Math.max(0, fieldStart - 50); load(); }).prop("disabled", !fieldStart),
                            button("Next fields", () => { fieldStart += 50; load(); }).prop("disabled", fieldStart + 50 >= result.total));
                    }
                    else { const catalog = await rpc("schema_operation_catalog", {snapshot_name: active.schema_snapshot, operation_kind: active.operation_kind});
                        if (!currentPicker(request)) return;
                        const operation = catalog.find((v) => v.operation_name === active.operation_name);
                        const all = Object.entries(operation?.input_fields || {}).flatMap(([argument, list]) => list.map((f) => ({...f, argument, fieldtype: f.type})));
                        const needle = query.val().toLowerCase(); const matched = all.filter((v) => `${v.argument}.${v.path} ${v.type}`.toLowerCase().includes(needle));
                        fields = matched.slice(fieldStart, fieldStart + 50); results.text(`${matched.length ? fieldStart + 1 : 0}–${Math.min(fieldStart + fields.length, matched.length)} / ${matched.length}`);
                        results.append(button("Previous fields", () => { fieldStart = Math.max(0, fieldStart - 50); load(); }).prop("disabled", !fieldStart), button("Next fields", () => { fieldStart += 50; load(); }).prop("disabled", fieldStart + 50 >= matched.length)); }
                    if (!currentPicker(request)) return;
                    fields.forEach((f) => results.append(button(`${f.argument ? f.argument + "." : ""}${f.path} · ${f.fieldtype || f.type}${f.nullable === false ? " · required" : ""}${f.select_options?.length ? ` · enum: ${f.select_options.join(", ")}` : ""}${source && mutation && !row.reference && reviewedTargetLabel(f) ? ` · ${text("Reviewed HERO target")}: ${reviewedTargetLabel(f)}` : ""}${f.selectable === false ? ` · unavailable: ${f.reason || "not permitted"}` : ""}`, () => {
                        if (row.reference) {
                            if (source) { const selectedDoctype = f.target_doctype || doctype.val(); model.referenceApps ||= Object.create(null); model.referenceApps[row.id] = {app: app.val(), doctype: selectedDoctype}; model.set(["field_rules", row.id, "target_doctype"], selectedDoctype); model.set(["field_rules", row.id, "target_field"], f.path); }
                            else { model.set(["field_rules", row.id, "source_path"], f.path); model.set(["field_rules", row.id, "source_type"], f.mapping_type || f.fieldtype); }
                        }
                        else if (source && mutation) { model.setFrappeSource(app.val(), f.target_doctype || doctype.val(), f.path); }
                        else if (source) { model.setFrappeTarget(app.val(), f.target_doctype || doctype.val(), f.path); }
                        else if (!mutation) { model.set(["source", "field"], f.path); if (Object.hasOwn(model.content.source, "path")) model.set(["source", "path"], f.path); }
                        else { model.set(["target", "argument"], f.argument); model.set(["target", "operation"], active.operation_name); model.set(["target", "field"], f.path); if (Object.hasOwn(model.content.target, "field_path")) model.set(["target", "field_path"], f.path); model.set(["target", "graphql_type"], f.type); }
                        renderDetail();
                    }).prop("disabled", f.selectable === false)));
                } catch (error) { if (currentPicker(request)) results.text(errorText(error)); }
                finally { if (currentPicker(request)) fieldsPending = false; }
            };
            const searchFields = button("Search fields", () => { fieldStart = 0; load(); }).prop("disabled", source);
            pickerHost.append(searchFields, button("Close field search", () => pickerHost.remove()));
            const loadCatalog = async () => {
                const request = ++seq; catalogPending = true;
                catalogState.text(text("Loading installed-app catalog…"));
                try { const catalogMethod = mutationMapping() && !row.reference ? "writeback_mapper_catalog" : "mapping_target_catalog";
                const result = await rpc(catalogMethod, catalogMethod === "writeback_mapper_catalog" ? {snapshot_name: active.schema_snapshot} : {});
                if (!currentPicker(request)) return;
                frappeCatalog = result;
                if (catalogMethod === "writeback_mapper_catalog") contractState.text(text(writebackContractLabel(result, active.operation_name)));
                app.prop("disabled", false); doctype.prop("disabled", false); query.prop("disabled", false);
                appOptions.empty();
                result.apps.forEach((item) => appOptions.append($("<option></option>").val(item.app_name || item)));
                catalogState.text(`${text("Searchable installed-app catalog")}: ${result.apps.length} ${text("apps")}, ${result.doctypes.length} ${text("DocTypes")}.`);
                app.on("input change", () => { seq++; results.empty(); fieldStart = 0; searchFields.prop("disabled", true); doctype.val(""); doctypeOptions.empty(); const matches = result.doctypes.filter((item) => item.app_name === app.val()); matches.forEach((item) => doctypeOptions.append($("<option></option>").val(item.doctype))); catalogState.text(`${text("Searchable DocTypes")}: ${matches.length}. ${text("Unavailable options remain listed and explain why when selected.")}`); });
                doctype.on("input change", () => { seq++; results.empty(); fieldStart = 0; const item = result.doctypes.find((candidate) => candidate.app_name === app.val() && candidate.doctype === doctype.val()); searchFields.prop("disabled", !item?.selectable); catalogState.text(item ? `${item.doctype}: ${item.selectable ? text("available") : item.reason || text("unavailable")}` : text("Choose a listed app and DocType.")); });
                query.trigger("focus");
                } catch (error) { if (currentPicker(request)) { query.prop("disabled", false); catalogState.text(errorText(error)); results.text(errorText(error)); } }
                finally { if (currentPicker(request)) catalogPending = false; }
            };
            pickerHost.on("hero-mapping-picker-resume", () => {
                pickerEpoch = epoch; seq++;
                if (catalogPending) { catalogPending = false; loadCatalog(); }
                else if (fieldsPending) { fieldsPending = false; load(); }
            });
            if (source) { results.text(text("Choose an app and DocType before searching fields.")); loadCatalog(); }
            else { app.hide(); doctype.hide(); query.trigger("focus"); }
        }
        const pageUrl = location.href, pageHistoryState = history.state;
        const router = frappe.router, originalRoute = router.route;
        let routePending = false;
        // Frappe starts its SPA route before later window popstate listeners.
        // Keep this adapter local to the mounted editor and restore its exact owner on destroy.
        function guardedRoute(...args) {
            if (!anyDirty() && !busy) return originalRoute.apply(this, args);
            const destination = location.href;
            history.pushState(pageHistoryState, "", pageUrl);
            if (busy || routePending) { say("Wait for the current request before leaving."); return Promise.resolve(false); }
            routePending = true;
            return new Promise((resolve) => leaveWorkspace(() => {
                routePending = false;
                history.replaceState(pageHistoryState, "", destination);
                resolve(originalRoute.apply(router, args));
            }, () => { routePending = false; resolve(false); }));
        }
        router.route = guardedRoute;
        const unload = (event) => { if (anyDirty() || busy) { event.preventDefault(); event.returnValue = ""; } };
        window.addEventListener("beforeunload", unload);
        // Capture links leaving the page before Frappe's delegated router handles them.
        const links = (event) => {
            const link = event.target.closest("a[href]");
            if (isOwnedExportLink(link, ownedDownload)) return;
            if (link && (anyDirty() || busy) && !event.defaultPrevented && !event.metaKey && !event.ctrlKey && link.origin === location.origin && link.href !== location.href) {
                event.preventDefault(); event.stopImmediatePropagation(); leaveWorkspace(() => { location.href = link.href; });
            }
        };
        document.addEventListener("click", links, true);
        const controller = {beforeLeave: leaveWorkspace, destroy() { shell.removeClass("hero-mapping-shell"); disposed = true; epoch++; window.removeEventListener("beforeunload", unload); if (router.route === guardedRoute) router.route = originalRoute; document.removeEventListener("click", links, true); }};
        current = controller; loadList(); return controller;
    }
    window.hero_integration_mapping_editor = Object.freeze({mount, DraftState, stable, readActiveRule, readSnapshotPages, exchangeExpectedStates, exchangeCounts, exchangeCanAdopt, reviewedTargetLabel, writebackContractLabel, legacyWritebackRows, legacyWritebackDetailMatches, sharedWritebackDraftAccess, reviewedWritebackEditable, buildReviewedWriteback, sharedWritebackRequest, sharedWritebackSaveAccepted, finishSharedWritebackSave, legacyWritebackCurrent, legacyPackageSelection, legacyPackageAdoptionArgs, legacyCandidateArgs, legacyCandidateMatches, legacyCandidateRawMessage, legacyCandidateRpc, legacyCandidatePreviewText, isOwnedExportLink, mappingTabs, directionForTab, bindingAccess, selectedPickerApp, hasCurrentDraftBase, canSaveDraft, tabKeyTarget, MappingTabStates, resumePendingView});
})();
