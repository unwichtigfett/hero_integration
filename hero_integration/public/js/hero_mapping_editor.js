(() => {
    "use strict";
    if (window.hero_integration_mapping_editor) return;
    const text = (value) => window.hero_integration_i18n?.text(value) ?? String(value);
    const format = (message, ...values) => text(message).replace(/\{(\d+)\}/g, (_, index) => String(values[index] ?? ""));
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
    // Snapshots keep their internal name (UID); the UI shows the server display name S-ddmmyy-NN.
    const snapshotLabels = new Map();
    // Parent links (previous snapshot of the same HERO source); used for mapping inheritance.
    const snapshotParents = new Map();
    const snapshotLabel = (name) => (name && snapshotLabels.get(name)) || name;
    const snapshotTime = (row) => String(row?.creation || row?.captured_at || row?.modified || "");
    const newestOf = (rows) => rows.reduce((best, row) => !best || snapshotTime(row) > snapshotTime(best) || (snapshotTime(row) === snapshotTime(best) && row.name > best.name) ? row : best, null);
    // The newest validated snapshot of the configured HERO source; other sources (e.g. test sources) only as a fallback.
    const newestValidatedSnapshot = (rows) => {
        const validated = (rows || []).filter((row) => row?.analysis_status === "Validated");
        return newestOf(validated.filter((row) => row.configured_source === true)) || newestOf(validated);
    };
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
            page.forEach((item) => {
                snapshotLabels.set(item.name, typeof item.display_name === "string" && item.display_name ? item.display_name : item.name);
                if (typeof item.parent_snapshot === "string" && item.parent_snapshot) snapshotParents.set(item.name, item.parent_snapshot);
                else snapshotParents.delete(item.name);
            });
            if (page.length < 100) return result;
            if (start > 2147483547) throw new Error('Snapshot list exceeds the supported range.');
        }
    }
    const directionForTab = (tab) => tab === "Query" ? "QUERY_TO_FRAPPE" : tab === "Mutation" ? "FRAPPE_TO_MUTATION" : null;
    const definitionChoice = (item, index) => ({...item, label: item.operation_name || item.operation || "",
        context: [index == null ? "" : `Definition ${index + 1}`, item.kind, item.target_doctype, snapshotLabel(item.schema_snapshot),
            item.revision == null ? "" : `Revision ${item.revision}`].filter(Boolean).join(" · ")});
    const objectFieldContext = (field) => {
        const args = (items) => (items || []).map((arg) => `${arg.name}: ${arg.type}`).join(", ");
        const relations = (field.relations || []).map((rel) => `${rel.owner_type}.${rel.name} → ${rel.target_type}${rel.is_list ? " [list]" : ""}${rel.arguments?.length ? ` (${args(rel.arguments)})` : ""}`);
        return [field.fieldtype || field.type || "", ...relations, field.arguments?.length ? `Arguments: ${args(field.arguments)}` : "", field.reason || ""].filter(Boolean).join(" · ");
    };
    const mappingAreas = Object.freeze(["All areas", "Customers", "Projects", "Tasks", "Calendar"]);
    const objectsInArea = (objects, area) => (objects || []).filter((item) =>
        area === "All areas" || (mappingAreas.includes(area) && Array.isArray(item.areas) && item.areas.includes(area)));
    const relationshipDescription = (rule, operation, fieldContext) => {
        const matchesObject = (name) => !fieldContext?.object_type || fieldContext.object_type === name;
        if (rule && operation === "HeroProjectMatches" && matchesObject("ProjectMatch") && rule.source_path === "customer_id" &&
            rule.target_doctype === "Project" && rule.target_field === "customer" &&
            rule.purpose === "Customer Relation" && rule.transformation === "HERO Customer Link" && rule.relation === "HERO Customer Link")
            return {source: "ProjectMatch.customer_id", target: "Project.customer", role: "Customer of the project",
                identity: "Resolve the Customer using the source instance and external ID through HERO Integration Link.",
                cardinality: "One Customer link per Project", state: "Existing reviewed relationship; unresolved identity requires review."};
        if (rule && operation === "HeroTasks" && matchesObject("Task") && rule.source_path === "target_project_match_id" &&
            rule.target_doctype === "Task" && rule.target_field === "project" &&
            rule.purpose === "Native Binding" && rule.transformation === "Native Adapter")
            return {source: "Task.target_project_match_id", target: "Task.project", role: "Project of the task",
                identity: "Resolve the Project using the source instance and external ID through HERO Integration Link.",
                cardinality: "One Project link per Task", state: "Existing reviewed relationship; unresolved identity requires review."};
        if (rule && operation === "HeroCalendarEvents" && matchesObject("CalendarEvent") && rule.source_path === "project_match_id" &&
            rule.target_doctype === "Event" && rule.target_field === "custom_hero_project" &&
            rule.purpose === "Project Relation" && rule.transformation === "HERO Project Link" && rule.relation === "HERO Project Link")
            return {source: "CalendarEvent.project_match_id", target: "Event.custom_hero_project", role: "Project of the event",
                identity: "Resolve the Project using the source instance and external ID through HERO Integration Link.",
                cardinality: "One Project link per Event", state: "Existing reviewed relationship; unresolved identity requires review."};
        try {
            const parsed = JSON.parse(fieldContext?.schema_context || "null");
            if (parsed && Array.isArray(parsed.segments) && parsed.segments.length > 1) {
                const steps = parsed.segments.slice(0, -1);
                return {source: `${parsed.object_type}.${parsed.segments.map((step) => step.name).join(".")}`,
                    target: rule?.target_doctype && rule?.target_field ? `${rule.target_doctype}.${rule.target_field}` : "Choose a Frappe target",
                    role: steps.map((step) => `${step.owner_type}.${step.name}`).join(" → "),
                    cardinality: steps.map((step) => `${step.name}: ${step.type}`).join("; "),
                    identity: "Identity and resolver have not been confirmed for this path.",
                    state: "Relationship path is draft-only and requires review before publication."};
            }
        } catch (_) { /* A malformed context is rejected by the server. */ }
        if (rule?.relation) return {source: rule.source_path || "Not provided", target: [rule.target_doctype, rule.target_field].filter(Boolean).join("."),
            role: rule.relation, identity: "Review the existing resolver and identity contract.",
            cardinality: "Review the existing relationship contract.", state: "Configured relationship; authority is not inferred."};
        return null;
    };
    const actionInputType = (operation, argument) =>
        (operation?.arguments?.find((item) => item.name === argument)?.type || "").replace(/[!\[\]]/g, "");
    const bindingAccess = (admin, definition) => !admin ? "ADMIN_ONLY" : definition?.definition_id ? "READ_ONLY" : "SELECT_DEFINITION";
    const resumePendingView = (pending, handlers) => {
        if (pending?.kind === "list") return handlers.list(pending.selectFirst ?? true);
        if (pending?.kind === "detail") return handlers.detail(pending.item, pending.mappingType, pending.focus ?? true);
        return false;
    };
    const selectedPickerApp = (model, side, value, admin) =>
        model?.frappeSelection?.side === side && model.frappeSelection.doctype === value?.doctype
            ? model.frappeSelection.app : admin ? "" : value?.app || "";
    const hasCurrentDraftBase = (model, active) => Boolean(model && !model.detailStale && active &&
        active.definition_id === model.saved.definition_id &&
        (!model.saved.base_active_name || active.name === model.saved.base_active_name) &&
        active.revision === (model.reconciliation?.activeRevision ?? model.verifiedActiveRevision ?? model.saved.base_active_revision));
    const canSaveDraft = (model, active) => Boolean(model && !model.creation && !model.blocked && hasCurrentDraftBase(model, active));
    const publicationResultName = (result, selected) => {
        if (!result || typeof result.name !== 'string' || !result.name || !Number.isInteger(result.revision)
            || result.revision < 1 || (selected.parentless ? result.revision !== 1 : result.name !== selected.name))
            throw new Error('Invalid publication result.');
        return result.name;
    };
    const publicationReadbackMatches = (observed, result, selected, definitionId) => Boolean(observed &&
        observed.name === result.name && observed.definition_id === definitionId && observed.revision === result.revision &&
        observed.status === 'Active' && (!selected.parentless ||
        ['source_instance', 'schema_snapshot', 'schema_fingerprint', 'direction', 'operation_kind', 'operation_name']
            .every((field) => observed[field] === selected[field])));
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
        accept(draft) { this.saved = draftValue(draft); this.verifiedActiveRevision = verifiedActiveRevision(this.saved); this.content = clone(this.saved.content); this.referenceOrder = [...(this.content.mapping_definition_ids || [])]; this.undo = []; this.reconciliation = null; this.blocked = false; this.creation = null; }
        get dirty() { return Boolean(this.creation) || stable(this.content) !== stable(this.saved.content); }
        createAdditional() {
            const value = this.creation;
            if (!value?.source?.field || !value?.target?.field) throw new Error('Choose both source and target fields first.');
            const items = this.content.additional_mappings || [];
            if (items.some((item) => stable(item.source) === stable(value.source) && stable(item.target) === stable(value.target))) throw new Error('This field mapping already exists in your draft.');
            this.invalidateReconciliation();
            this.content.additional_mappings = [...items, {id: window.crypto.randomUUID(), source: clone(value.source), target: clone(value.target), transformation: 'Identity'}];
            this.creation = null;
        }
        removeAdditional(index) {
            this.invalidateReconciliation();
            this.undo.push({kind: 'additional', index, value: this.content.additional_mappings[index]});
            this.content.additional_mappings.splice(index, 1);
            if (!this.content.additional_mappings.length && !Object.hasOwn(this.saved.content, 'additional_mappings')) delete this.content.additional_mappings;
        }
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
        setHeroSource(field) {
            this.set(["source", "field"], field.path);
            this.set(["source", "object_type"], field.object_type);
            this.set(["source", "schema_context"], field.schema_context);
            for (const alias of ["path", "source_path"]) {
                if (Object.hasOwn(this.content.source, alias)) this.set(["source", alias], field.path);
            }
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
                const id = this.content.mapping_definition_ids[index];
                const fieldContext = this.content.field_contexts?.[id];
                this.undo.push({kind: "reference", index, value: id, ...(fieldContext ? {fieldContext: clone(fieldContext)} : {})});
                if (fieldContext) {
                    delete this.content.field_contexts[id];
                    if (!Object.keys(this.content.field_contexts).length && !Object.hasOwn(this.saved.content, 'field_contexts')) delete this.content.field_contexts;
                }
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
            if (removed.fieldContext) {
                this.content.field_contexts ||= {};
                this.content.field_contexts[removed.value] = clone(removed.fieldContext);
            }
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
            else if (removed.kind === 'additional') { this.content.additional_mappings ||= []; this.content.additional_mappings.splice(removed.index, 0, removed.value); }
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
    const exchangeFailureText = (error) => {
        if (denied(error)) return errorText(error);
        try {
            const messages = JSON.parse(error?.responseJSON?._server_messages || error?._server_messages || "[]");
            const first = typeof messages[0] === "string" ? JSON.parse(messages[0]) : messages[0];
            if (typeof first?.message === "string" && first.message.length <= 1500) return first.message;
        } catch (_) { /* Keep the safe generic message for malformed responses. */ }
        return errorText(error);
    };
    let current;
    function mount(page, context = {}) {
        current?.destroy();
        const host = page.main.empty().addClass("hero-integration-ui");
        const root = $("<section class='hero-mapping-core'></section>").appendTo(host);
        const shell = host.closest(".admin-workspace").addClass("hero-mapping-shell");
        const admin = Boolean(window.hero_integration_ui?.isSystemManager);
        let disposed = false, epoch = 0, busy = false, sharedDirty = false, model = null, active = null, mode = "active", selected = 0, ownedDownload = null;
        let search = String(context.mapping_search || ""), start = Number(context.mapping_start || 0), listing, snapshotFilter = "";
        let revisionsStart = 0, returnFocus = null, fieldFilter = "", detailRequest = 0, exchangeSequence = 0, snapshotDefaultApplied = false;
        const exchanges = new Map();
        const exchangeSelections = new Map();
        const browsePaths = new Map();
        const browseObjects = new Map();
        const browseActions = new Map();
        const browseAreas = new Map();
        const ruleCache = new Map();
        let appCatalog;
        const readableApps = () => admin ? appCatalog ||= rpc("mapping_target_catalog", {}).catch((error) => { appCatalog = null; throw error; }) : Promise.resolve(null);
        const button = (label, fn, primary = false) => $("<button type='button'></button>")
            .addClass(primary ? "btn btn-primary btn-sm" : "btn btn-default btn-sm").text(text(label)).on("click", fn);
        const status = $("<p role='status' aria-live='polite'></p>");
        const toolbar = $("<div class='hero-mapping-actions'></div>");
        const body = $("<div class='hero-mapping-body'></div>");
        const tabs = $("<div role='tablist' class='hero-mapping-actions'></div>").attr("aria-label", text("Mapping sections"));
        const panel = $("<section role='tabpanel' id='hero-mapping-tabpanel'></section>").append(toolbar, body);
        const heading = $("<header class='hero-mapping-title'></header>").append($("<h2 tabindex='-1'></h2>").text(text("HERO Mapping Editor")), tabs);
        root.append(heading, status, panel);
        const tabNames = mappingTabs, tabStates = new MappingTabStates();
        let tab = "Query", bindingOrigin = "Query", rowChangeHandler = null, pendingView = null, entryInteracted = false;
        root.on("pointerdown keydown", () => { if (pendingView?.kind === "list") entryInteracted = true; });
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
            closeChoice(false);
            if (next === tab || locked() || root.find("dialog").length) return;
            const previous = tab;
            toolbar.detach();
            epoch++; detailRequest++;
            tabStates.set(previous, {active, model, mode, selected, search, start, listing, revisionsStart, returnFocus, fieldFilter, snapshotFilter,
                rowChangeHandler, pendingView, status: status.text(), toolbar: toolbar.contents().detach(), body: body.contents().detach()});
            pendingView = null;
            root.off("hero-mapping-content-change"); rowChangeHandler = null;
            const bindingActive = active;
            tab = next; updateTabs();
            if (next === "Binding") {
                bindingOrigin = previous === "Binding" ? bindingOrigin : previous;
                active = bindingActive; model = null; mode = "active";
                toolbar.detach().empty(); body.empty();
                const access = bindingAccess(admin, active);
                if (access === "ADMIN_ONLY") { say("Bindings are available to administrators."); body.append($("<p></p>").text(text("Employee read-only view"))); }
                else if (access === "SELECT_DEFINITION") { showBindings(0); }
                else showBindings(0);
                return;
            }
            const state = tabStates.get(next);
            if (state) {
                ({active, model, mode, selected, search, start, listing, revisionsStart, returnFocus, fieldFilter, snapshotFilter, rowChangeHandler} = state);
                toolbar.empty().append(state.toolbar); body.empty().append(state.body);
                const anchor = body.find(".hero-mapping-active").first();
                if (anchor.length) anchor.after(toolbar); else panel.prepend(toolbar);
                say(state.status);
                if (rowChangeHandler) root.on("hero-mapping-content-change", rowChangeHandler);
                body.find("[data-field-picker]").trigger("hero-mapping-picker-resume");
                resumePendingView(state.pendingView, {list: loadList, detail: open});
            } else {
                active = null; model = null; mode = "active"; selected = 0; search = ""; start = 0; listing = null;
                revisionsStart = 0; returnFocus = null; fieldFilter = ""; snapshotFilter = ""; snapshotDefaultApplied = false; loadList();
            }
        }
        updateTabs();
        const say = (value) => status.text(text(value));
        const focusHeading = () => root.find("h2").trigger("focus");
        const locked = () => busy || disposed;
        const exchangeContext = () => {
            if (!admin) return null;
            const selected = active?.mapping_type === "graphql"
                ? {source_instance: active.source_instance, snapshot_name: active.schema_snapshot, direction: active.direction}
                : exchangeSelections.get(directionForTab(tab));
            if (!selected || !["QUERY_TO_FRAPPE", "FRAPPE_TO_MUTATION"].includes(selected.direction) ||
                ![selected.source_instance, selected.snapshot_name].every((value) => typeof value === "string" && value)) return null;
            return {...selected, key: `${selected.source_instance}\u0000${selected.snapshot_name}\u0000${selected.direction}`};
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
                ["Discard changes", () => { model.content = clone(model.saved.content); model.undo = []; model.creation = null; action(); }],
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
            root.find('[data-activate-draft]').prop('disabled', !model || model.dirty || Boolean(model.reconciliation)
                || !model.saved.content_revision || !canSaveDraft(model, active) || busy);
            root.trigger("hero-mapping-content-change");
        }
        async function loadList(selectFirst = false) {
            const request = ++epoch; entryInteracted = false; pendingView = {kind: "list", selectFirst}; ruleCache.clear(); active = null; model = null; mode = "active";
            rowChangeHandler = null; root.off("hero-mapping-content-change"); toolbar.empty(); body.empty(); say("Loading active mappings…");
            if (admin && !snapshotDefaultApplied) {
                // The newest validated snapshot is selected by default; "All readable snapshots" stays an explicit choice.
                snapshotDefaultApplied = true;
                try {
                    const snapshots = await readSnapshotPages((args) => rpc("schema_snapshot_list", args), () => !disposed && request === epoch);
                    if (disposed || request !== epoch) return;
                    const latest = newestValidatedSnapshot(snapshots);
                    if (latest && !snapshotFilter) snapshotFilter = latest.name;
                } catch (_) { /* Without the snapshot list all readable snapshots stay visible. */ }
            }
            renderList();
            try {
                const result = await rpc("frontend_mapping_list", {search, start, page_size: 20, direction: directionForTab(tab)});
                if (disposed || request !== epoch) return;
                listing = result;
                const entry = selectFirst && !entryInteracted && result.graphql_mappings.find((item) => item.direction === directionForTab(tab) && (!snapshotFilter || item.schema_snapshot === snapshotFilter));
                if (entry) { await open(entry, "graphql", false); return; }
                say("");
                if (returnFocus) { root.find("[data-mapping-name]").filter((_, el) => el.dataset.mappingName === returnFocus).first().trigger("focus"); returnFocus = null; }
            } catch (error) { if (request === epoch && !disposed) { say(errorText(error)); body.append(button("Retry", () => loadList(selectFirst))); } }
            finally { if (request === epoch) pendingView = null; }
        }
        function renderSchemaExplorer() {
            if (!admin) return;
            let viewEpoch = epoch, request = 0;
            const viewTab = tab, section = $("<section data-field-picker></section>"), explorer = $("<details class='hero-mapping-schema-explorer'></details>").append($('<summary></summary>').text(text('Inspect saved schema')));
            section.append(explorer);
            const snapshot = $("<select class='form-control'></select>").attr("aria-label", text("Saved schema snapshot"));
            const operations = $("<select class='form-control'></select>").attr("aria-label", text("Schema operation"));
            const info = $("<p role='status'></p>"), limitNotice = $("<p role='status'></p>"), details = $('<pre></pre>');
            const currentView = (number) => !disposed && epoch === viewEpoch && tab === viewTab && number === request && section[0].isConnected;
            const resetOperations = () => operations.empty().append($('<option></option>').val('').text(text('Choose an operation'))).prop('disabled', true);
            const resetSnapshots = () => snapshot.empty().append($('<option></option>').val('').text(text('Choose a validated snapshot'))).prop('disabled', true);
            resetSnapshots(); resetOperations();
            async function loadOperations(name, preferred = '') {
                const serial = ++request; resetOperations(); details.empty();
                if (!name) { info.text(text('Choose a validated snapshot.')); return; }
                info.text(text('Loading schema operations…'));
                try {
                    const result = await rpc('schema_operation_catalog', {snapshot_name: name, operation_kind: viewTab === 'Query' ? 'QUERY' : 'MUTATION'});
                    if (!currentView(serial)) return;
                    result.forEach((item) => operations.append($('<option></option>').val(item.operation_name).text(`${item.operation_name} → ${item.return_type || 'unknown'}`)));
                    operations.prop('disabled', false); info.text(text('Choose an operation to inspect its schema.'));
                    operations.off('change.schema').on('change.schema', () => { const item = result.find((row) => row.operation_name === operations.val()); details.text(item ? JSON.stringify(item, null, 2) : ''); info.text(text('Saved schema operation details.')); });
                    if (preferred && result.some((item) => item.operation_name === preferred)) operations.val(preferred).trigger('change');
                } catch (error) { if (currentView(serial)) info.text(errorText(error)); }
            }
            async function loadSnapshots(preferredSnapshot = '', preferredOperation = '') {
                const serial = ++request; resetSnapshots(); resetOperations(); details.empty();
                info.text(text('Loading saved snapshots…')); limitNotice.empty();
                try {
                    const result = await readSnapshotPages((args) => rpc('schema_snapshot_list', args), () => currentView(serial));
                    if (!result) return;
                    limitNotice.text(text('Snapshots can change while pages load. Retry saved schema to refresh the list.'));
                    const valid = result.filter((row) => row.analysis_status === 'Validated');
                    if (preferredSnapshot && !valid.some((row) => row.name === preferredSnapshot)) {
                        limitNotice.text(`${limitNotice.text()} ${text('The previously selected snapshot is not available in this result. Select a validated snapshot again.')}`.trim());
                    }
                    valid.forEach((row) => snapshot.append($('<option></option>').val(row.name).text(snapshotLabel(row.name))));
                    snapshot.prop('disabled', false); info.text(text('Choose a validated snapshot.'));
                    if (preferredSnapshot && valid.some((row) => row.name === preferredSnapshot)) {
                        snapshot.val(preferredSnapshot); await loadOperations(preferredSnapshot, preferredOperation);
                        if (!disposed && epoch === viewEpoch && tab === viewTab && section[0].isConnected && snapshot.val() === preferredSnapshot)
                            section.trigger('hero-mapping-refresh-shared');
                    }
                } catch (error) { if (currentView(serial)) { limitNotice.text(text('The snapshot list could not be loaded completely. Retry before choosing a snapshot.')); info.text(errorText(error)); } }
            }
            snapshot.on('change', () => { loadOperations(snapshot.val()); });
            explorer.append(snapshot, operations, info, limitNotice, details);
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
                    const state = $("<p role='status'></p>"), choices = $("<select class='form-control'></select>").attr("aria-label", text("Reviewed shared source"));
                    const candidates = creating ? catalog.fields.filter((item) => item.selectable === true && item.target) : [];
                    if (creating) {
                        choices.append($('<option></option>').val('').text(text('Choose a reviewed source and target')));
                        candidates.forEach((item, index) => choices.append($('<option></option>').val(String(index)).text(`${item.doctype}.${item.path} → ${reviewedTargetLabel(item)}`)));
                        dialog.append($('<label></label>').text(text('Reviewed source and target')).append(choices));
                    } else dialog.append($('<p></p>').text(`${text('Shared draft')}: ${doc.name} · ${text('Status')}: ${doc.status} · ${text('Revision')}: ${doc.mapping_revision} · ${text('Last editor')}: ${doc.modified_by}`));
                    const initial = doc ? JSON.parse(doc.mapping_json) : null;
                    const transform = $("<select class='form-control'><option value='Identity'>Identity</option><option value='Trim'>Trim</option></select>").attr("aria-label", text("Shared draft transformation")).val(initial?.transformation || 'Identity');
                    const reason = $("<textarea class='form-control' maxlength='1000'></textarea>").attr("aria-label", text("Shared draft review reason")).val(doc?.suggestion_reason || '');
                    const confidence = $("<input class='form-control' type='number' min='0' max='1' step='any'>").attr("aria-label", text("Shared draft confidence")).val(doc?.confidence ?? 0);
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
            // Import/export lives on the Settings page (Configuration package); the mapping page shows none.
            explorer.append(button('Retry saved schema', () => { loadSnapshots(snapshot.val(), operations.val()); }));
            section.append(button('Open Graph Viewer', () => {
                const name = snapshot.val(); if (!name || snapshot.prop('disabled')) return info.text(text('Choose a validated snapshot.'));
                leaveWorkspace(() => window.hero_integration_ui.setRoute('api', {hero_tool: 'graph', snapshot: name}));
            }));
            section.on('hero-mapping-picker-resume', () => {
                const name = snapshot.val() || '', operation = operations.val() || '';
                viewEpoch = epoch; loadSnapshots(name, operation);
            });
            renderSavedNodes(section, {viewTab, snapshot, operations, explorer, info, loadSnapshots, current: () => !disposed && epoch === viewEpoch && tab === viewTab && section[0].isConnected});
            // Preselect the workspace snapshot (the newest validated one by default).
            body.append(section); loadSnapshots(snapshotFilter);
        }
        function renderSavedNodes(section, view) {
            // Saved HERO Nodes: personal bookmarks from API Analyzer / Graph Viewer.
            // Selecting one only preselects explorer inputs; it never edits a draft,
            // bumps a revision, activates a mapping, syncs or calls HERO.
            const store = window.hero_integration_bookmarks;
            if (!store) return;
            const box = $("<details class='hero-mapping-saved-nodes'></details>").append($('<summary></summary>').text(text('Saved HERO Nodes')));
            const search = $("<input class='form-control'>").attr('aria-label', text('Search saved HERO nodes')).attr('placeholder', text('Search alias or original name'));
            const list = $("<div class='hero-mapping-saved-node-list' role='list'></div>"), state = $("<p role='status'></p>");
            let request = 0;
            const wanted = view.viewTab === 'Query' ? 'QUERY' : 'MUTATION';
            async function refresh() {
                const serial = ++request; state.text(text('Loading saved HERO nodes…'));
                try {
                    const result = await store.list(search.val().trim());
                    if (serial !== request || !view.current()) return;
                    list.empty();
                    if (!result.bookmarks.length) state.text(text('No saved HERO nodes. Use “Remember for Mapping” in the API Analyzer or Graph Viewer.'));
                    else state.text('');
                    result.bookmarks.forEach((item) => list.append(row(item)));
                } catch (error) { if (serial === request) state.text(errorText(error)); }
            }
            function preselect(snapshotName, context) {
                if (context.operation_kind !== wanted) {
                    view.info.text(format('This node belongs to the {0} view. Open that mapping tab to use it.', context.operation_kind === 'QUERY' ? 'Query' : 'Mutation'));
                    return;
                }
                view.explorer.prop('open', true);
                view.loadSnapshots(snapshotName, context.operation).then(() => {
                    if (!view.current()) return;
                    view.info.text(context.path
                        ? format('Operation {0} selected. Saved node path: {1}. Nothing was saved or activated.', context.operation, context.path)
                        : format('Operation {0} selected. Nothing was saved or activated.', context.operation));
                });
            }
            async function use(item) {
                box.find('.hero-mapping-saved-node-choices').remove();
                state.text(text('Checking the saved node against its snapshot…'));
                try {
                    const result = await store.resolve(item.name);
                    if (!view.current()) return;
                    const context = result.context, bookmark = result.bookmark;
                    if (bookmark.status === 'UNRESOLVABLE' || !context) { state.text(text(bookmark.reason)); return; }
                    const outdated = bookmark.status === 'OUTDATED' ? ' ' + text('Note: a newer validated snapshot exists; the bookmark still refers to its original snapshot.') : '';
                    state.text(text(context.reason) + outdated);
                    if (context.mappable) return preselect(bookmark.schema_snapshot, context);
                    const choices = $("<div class='hero-mapping-saved-node-choices'></div>");
                    (context.candidates || []).forEach((candidate) => choices.append(button(`${candidate.operation} · ${candidate.path}`, () => { choices.remove(); preselect(bookmark.schema_snapshot, candidate); })));
                    if (!context.candidates?.length && context.operation) choices.append(button(`${text('Select operation')} ${context.operation}`, () => { choices.remove(); preselect(bookmark.schema_snapshot, {...context, path: ''}); }));
                    choices.append(button(text('Open in Graph Viewer'), () => leaveWorkspace(() => window.hero_integration_ui.setRoute('api', {hero_tool: 'graph', snapshot: bookmark.schema_snapshot, node: bookmark.node_id}))));
                    state.after(choices);
                } catch (error) { state.text(errorText(error)); }
            }
            function row(item) {
                const entry = $("<div class='hero-mapping-saved-node' role='listitem'></div>");
                entry.append($('<strong></strong>').text(item.alias), $('<span></span>').text(` · ${item.node_name} · ${item.node_kind} · ${snapshotLabel(item.schema_snapshot)} · ${item.status}`));
                entry.attr('title', item.reason || '');
                entry.append(button(text('Use in Mapping'), () => use(item)).prop('disabled', item.status === 'UNRESOLVABLE'));
                entry.append(button(text('Rename'), () => frappe.prompt({fieldname: 'alias', fieldtype: 'Data', label: text('Alias'), reqd: 1, default: item.alias},
                    async (values) => { try { await store.rename(item.name, values.alias); await refresh(); } catch (error) { state.text(errorText(error)); } }, text('Rename saved HERO node'), text('Save'))));
                entry.append(button(text('Remove'), () => frappe.confirm(text('Remove this saved HERO node? Mappings are not affected.'),
                    async () => { try { await store.remove(item.name); await refresh(); } catch (error) { state.text(errorText(error)); } })));
                return entry;
            }
            let timer = null;
            search.on('input', () => { clearTimeout(timer); timer = setTimeout(refresh, 250); });
            box.on('toggle', () => { if (box.prop('open')) refresh(); });
            box.append(search, state, list);
            section.append(box);
        }
        function renderList() {
            toolbar.detach().empty(); body.empty(); renderWorkspaceContext(); renderContext({}); renderMappingsTable(body);
            body.append($("<p class='text-muted'></p>").text(text("Choose an active mapping row or definition to inspect it. Reading and selection do not create a draft.")));
            if (admin) renderPersonalDraftList();
            renderSchemaExplorer();
        }
        function renderPersonalDraftList() {
            const viewEpoch = epoch, viewTab = tab, section = $("<section data-personal-drafts></section>");
            const list = $("<div></div>"), message = $("<p role='status'></p>"), pager = $("<div></div>");
            section.append($("<h3></h3>").text(text("My new personal drafts")), message, list, pager); body.append(section);
            let serial = 0;
            async function load(offset) {
                const request = ++serial;
                message.text(text("Loading personal drafts…"));
                try {
                    const result = await rpc("mapping_draft_list", {direction: directionForTab(viewTab), start: offset, page_size: 50});
                    if (disposed || epoch !== viewEpoch || tab !== viewTab || request !== serial || !section[0].isConnected) return;
                    if (!Array.isArray(result.items)) throw new Error("Invalid personal draft list.");
                    list.empty(); message.text(result.items.length ? "" : text("No personal drafts on this page."));
                    result.items.forEach((item) => list.append($("<p></p>").append(button(
                        `${item.operation_name} · ${snapshotLabel(item.snapshot_name)} · ${item.definition_id}`,
                        () => leave(() => openParentlessDraft(item))))));
                    pager.empty().append(button("Previous drafts", () => load(Math.max(0, offset - 50))).prop("disabled", !offset),
                        button("Next drafts", () => load(offset + 50)).prop("disabled", !result.has_more));
                } catch (error) { if (!disposed && epoch === viewEpoch && request === serial) message.text(errorText(error)); }
            }
            load(0);
        }
        async function openParentlessDraft(item) {
            if (locked()) return;
            const request = ++epoch; setBusy(true); say("Loading your saved draft…");
            try {
                const result = await rpc("mapping_draft_read", {definition_id: item.definition_id});
                if (disposed || request !== epoch) return;
                const draft = result.draft, definition = draft?.new_definition;
                if (draft?.state !== "OPEN" || draft.definition_id !== item.definition_id || !definition ||
                    definition.definition_id !== item.definition_id || definition.direction !== directionForTab(tab) ||
                    definition.source_instance !== item.source_instance || definition.snapshot_name !== item.snapshot_name ||
                    definition.schema_fingerprint !== item.schema_fingerprint || draft.base_active_revision !== 0) {
                    throw new Error("Personal draft context changed. Reload the draft list.");
                }
                active = {name: "", definition_id: item.definition_id, source_instance: item.source_instance,
                    schema_snapshot: item.snapshot_name, schema_fingerprint: item.schema_fingerprint,
                    direction: item.direction, operation_kind: item.operation_kind, operation_name: item.operation_name,
                    revision: 0, status: "Draft", mapping_type: "graphql", parentless: true};
                model = new DraftState(draft); mode = "draft"; selected = 0; fieldFilter = "";
                renderDetail(); say("Personal draft opened. It has no active definition.");
            } catch (error) { if (!disposed && request === epoch) say(errorText(error)); }
            finally { if (!disposed && request === epoch) setBusy(false); }
        }
        async function open(item, kind, focus = true) {
            if (locked()) return;
            const request = ++epoch; pendingView = {kind: "detail", item, mappingType: kind, focus}; ruleCache.clear(); panel.prepend(toolbar.detach()); body.empty(); toolbar.empty(); say("Loading active definition…");
            toolbar.append(button("Back to mappings", () => leave(() => loadList(false))));
            try {
                const detail = await rpc("frontend_mapping_detail", {mapping_type: kind, name: item.name});
                if (disposed || request !== epoch) return;
                active = {...detail, mapping_type: kind}; mode = "active"; model = null; selected = 0; fieldFilter = ""; returnFocus = item.name;
                renderDetail(); say(""); if (focus) focusHeading();
            } catch (error) { if (request === epoch && !disposed) say(errorText(error)); }
            finally { if (request === epoch) pendingView = null; }
        }
        function content() { return mode === "draft" ? model.content : active.definition || active; }
        function rows(value) {
            let result = [];
            if (Array.isArray(value.mapping_definition_ids)) result = value.mapping_definition_ids.map((id, index) => ({id, index, reference: true}));
            else if (object(value.source) || object(value.target)) result = [{id: "single", index: 0, value}];
            return result.concat((value.additional_mappings || []).map((entry, index) => ({id: entry.id, index, additional: true, value: entry})));
        }
        let openedChoice = null;
        const closeChoice = (focus = false) => {
            if (!openedChoice) return;
            const {control, summary, invalidate} = openedChoice;
            openedChoice = null; invalidate(); control.prop("open", false);
            if (focus && summary[0].isConnected) summary.trigger("focus");
        };
        const closeChoiceOutside = (event) => {
            if (openedChoice && !openedChoice.control[0].contains(event.target)) closeChoice(false);
        };
        document.addEventListener("pointerdown", closeChoiceOutside, true);
        function choice(label, value, parent, loader, select) {
            const control = $("<details class='hero-mapping-select' data-field-picker></details>").attr("data-choice-label", label);
            const displayValue = value || text("Choose…");
            const summary = $("<summary></summary>").append($("<span></span>").text(text(label)), $("<strong></strong>").text(displayValue).attr("title", displayValue));
            const popup = $("<div class='hero-mapping-select-popup'></div>"), results = $("<div role='list'></div>");
            const query = $("<input class='form-control' type='search'>").attr("aria-label", format("Search {0}", text(label)));
            let offset = 0, seq = 0, choiceEpoch = epoch, pending = false;
            const current = (n) => !disposed && choiceEpoch === epoch && n === seq && control[0].isConnected;
            const load = async () => {
                const n = ++seq; pending = true; results.empty().text(text("Loading options…"));
                try {
                    const result = await loader(query.val().trim(), offset);
                    if (!current(n)) return;
                    results.empty();
                    result.items.forEach((item) => results.append($("<div role='listitem'></div>").append(button(item.label, () => {
                        closeChoice(true); select(item);
                    }).attr("title", item.label).prop("disabled", Boolean(item.disabled)), item.context ? $("<small></small>").text(item.context) : null)));
                    if (!result.items.length) results.append($("<p></p>").text(text(result.emptyMessage || "No options match this selection.")));
                    results.append($("<div class='hero-mapping-actions'></div>").append(
                        button("Previous", () => { offset = Math.max(0, offset - 20); load(); }).attr("aria-label", text("Previous options")).prop("disabled", !offset),
                        $("<span></span>").text(`${offset + (result.items.length ? 1 : 0)}–${offset + result.items.length} / ${result.total}`),
                        button("Next", () => { offset += 20; load(); }).attr("aria-label", text("Next options")).prop("disabled", offset + 20 >= result.total)));
                } catch (error) { if (current(n)) results.empty().append($("<p></p>").text(errorText(error)), button("Retry options", load)); }
                finally { if (current(n)) pending = false; }
            };
            const apply = () => { offset = 0; load(); };
            query.on("input", () => { seq++; results.empty(); }).on("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); apply(); } });
            popup.append(query, button("Search options", apply), results); control.append(summary, popup).appendTo(parent);
            summary.on("click", (event) => {
                event.preventDefault();
                if (openedChoice?.control[0] === control[0]) { closeChoice(true); return; }
                closeChoice(false); openedChoice = {control, summary, invalidate: () => { seq++; pending = false; }};
                control.prop("open", true); load(); query.trigger("focus");
            });
            control.on("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeChoice(true); } });
            control.on("hero-mapping-picker-resume", () => { choiceEpoch = epoch; seq++; if (pending) load(); });
            return control;
        }
        function definitionOptions(direction = directionForTab(tab), snapshot = snapshotFilter) {
            return async (search, offset) => {
                // The existing list pages both categories independently. Filter locally without hiding later pages.
                let all = [];
                for (let start = 0; start <= 10000; start += 100) {
                    const data = await rpc("frontend_mapping_list", {search, start, page_size: 100, direction});
                    all.push(...data.graphql_mappings.map((item) => ({...item, kind: "graphql"})),
                        ...data.frappe_mappings.map((item) => ({...item, kind: "frappe"})));
                    if (start + 100 >= Math.max(data.pagination.graphql_total, data.pagination.frappe_total)) break;
                    if (start === 10000) throw new Error("Readable definition range exceeded");
                }
                all = all.filter((item) => !snapshot || item.schema_snapshot === snapshot);
                return {items: all.slice(offset, offset + 20).map((item, index) => definitionChoice(item, offset + index)), total: all.length};
            };
        }
        function renderDefinitionChooser(parent = body, label = "Definition", binding = false) {
            return choice(label, active?.operation_name || active?.operation, parent,
                definitionOptions(binding ? "" : directionForTab(tab), binding ? "" : snapshotFilter),
                (item) => leave(() => {
                    if (binding) { active = {...item, mapping_type: item.kind}; showBindings(0); }
                    else open(item, item.kind);
                }));
        }
        function renderWorkspaceContext() {
            const header = $("<div class='hero-mapping-workspace-context'></div>").appendTo(body);
            choice("GraphQL schema snapshot", snapshotFilter ? snapshotLabel(snapshotFilter) : text("All readable snapshots"), header, async (search, offset) => {
                const currentEpoch = epoch;
                const snapshots = admin ? await readSnapshotPages((args) => rpc("schema_snapshot_list", args), () => !disposed && epoch === currentEpoch) : [];
                const validated = (snapshots || []).filter((item) => item.analysis_status === "Validated").sort((a, b) => snapshotTime(b).localeCompare(snapshotTime(a)));
                const options = [{name: "", label: text("All readable snapshots")}, ...validated.map((item) => ({name: item.name, label: snapshotLabel(item.name)}))]
                    .filter((item) => `${item.label} ${item.name}`.toLowerCase().includes(search.toLowerCase()));
                return {items: options.slice(offset, offset + 20), total: options.length};
            }, (item) => leave(() => { snapshotFilter = item.name; active = null; model = null; renderList(); })).prop("hidden", !admin);
            const parent = admin && snapshotFilter ? snapshotParents.get(snapshotFilter) : "";
            if (parent) header.append(button(format("Inherit mappings from parent {0}", snapshotLabel(parent)), () => inheritFromParent(snapshotFilter, parent))
                .attr("data-inherit-parent", "").attr("title", text("Creates your personal drafts on this snapshot from the parent's active mappings. Nothing is activated.")));
            if (active) header.append($("<span class='text-muted'></span>").text(`${snapshotLabel(active.schema_snapshot) || text("Snapshot not provided")} · ${text("Revision")}: ${active.revision} · ${mode === "draft" ? text("Personal draft") : active.status || text("Active")}`));
        }
        function inheritFromParent(snapshotName, parent) {
            if (locked()) return;
            modal("Inherit mappings from parent",
                format("Create your personal drafts on {0} from the active mappings of {1}? Each draft is checked against the new schema. Nothing is activated and the parent stays unchanged.", snapshotLabel(snapshotName), snapshotLabel(parent)),
                [["Inherit as drafts", async () => {
                    const request = ++epoch; setBusy(true); say("Inheriting parent mappings as personal drafts…");
                    try {
                        const result = await rpc("workspace_snapshot_inherit_mappings", {snapshot_name: snapshotName}, true);
                        if (disposed || request !== epoch) return;
                        const counts = result?.counts || {};
                        const problems = (result?.results || []).filter((item) => item.status === "Incompatible");
                        say(format("{0} draft(s) on {1}, {2} incompatible, {3} skipped. Nothing was activated.", counts["Draft Created"] || 0, snapshotLabel(snapshotName), problems.length, counts.Skipped || 0));
                        if (problems.length) modal("Not inherited", problems.map((item) => `${item.operation}: ${item.reason}`).join(" · "), [["Close", () => {}, true]]);
                    } catch (error) { if (!disposed && request === epoch) say(errorText(error)); }
                    finally { if (!disposed && request === epoch) { setBusy(false); await loadList(false); } }
                }, true], ["Cancel", () => {}]]);
        }
        function renderMappingsTable(parent) {
            const section = $("<section class='hero-mapping-active'></section>").attr("aria-label", text("Active Mappings")).appendTo(parent);
            section.append($("<h3></h3>").text(text("Active Mappings")));
            const filters = $("<div class='hero-mapping-table-filters'></div>").appendTo(section);
            const operation = $("<select class='form-control'></select>").attr("aria-label", text("Operation filter"))
                .append($("<option value=''></option>").text(text("All operations")));
            $("<label></label>").append($("<span></span>").text(text("Operation")), operation).appendTo(filters);
            const searchInput = field("Search active mappings", search, () => {}, filters).attr("maxlength", 120);
            const fieldSearch = field("Filter field mappings", fieldFilter, () => {}, filters);
            const tableWrap = $("<div class='hero-mapping-table-wrap'></div>").appendTo(section);
            const table = $("<table class='table table-sm hero-mapping-table'><thead><tr><th>Status</th><th>Operation</th><th></th><th>App</th><th>Target</th><th>Revision</th></tr></thead><tbody></tbody></table>").appendTo(tableWrap);
            table.find("th").each((index, th) => { $(th).text(text($(th).text())); });
            table.find("th").eq(2).text(text(tab === "Mutation" ? "Source Frappe" : "Source HERO"));
            const pager = $("<div class='hero-mapping-actions'></div>").appendTo(section);
            let offset = 0, sequence = 0, tableEpoch = epoch, data, apps = new Map(), pending = false;
            const valid = (n) => !disposed && epoch === tableEpoch && n === sequence && section[0].isConnected;
            const draw = () => {
                table.find("tbody").empty();
                // In draft mode the open definition shows the personal draft rows, so field mappings are chosen here, not in a separate draft table.
                const draftOpen = (item, kind) => mode === "draft" && Boolean(model) && kind === "graphql" && active?.mapping_type === "graphql" && active?.name === item.name;
                const entries = [...data.graphql_mappings.map((item) => ({item, kind: "graphql"})), ...data.frappe_mappings.map((item) => ({item, kind: "frappe"}))];
                if (mode === "draft" && model && active?.parentless && !entries.some(({item, kind}) => draftOpen(item, kind))) entries.unshift({item: active, kind: "graphql"});
                const needle = fieldSearch.val().trim().toLowerCase();
                entries.filter(({item, kind}) => draftOpen(item, kind) || ((!snapshotFilter || item.schema_snapshot === snapshotFilter) && (!operation.val() || (item.operation_name || item.operation) === operation.val()))).forEach(({item, kind}) => {
                    const isDraft = draftOpen(item, kind);
                    const definition = isDraft ? model.content : item.definition || item;
                    const linked = new Map([...ruleCache, ...data.frappe_mappings.map((rule) => [rule.definition_id, rule])]);
                    const projected = kind === "frappe" ? [{source: item.source_path, target: `${item.target_doctype}.${item.target_field}`, app: "Not provided", index: 0}]
                        : rows(definition).map((row, index) => {
                            const rule = row.reference ? linked.get(row.id) || ruleCache.get(row.id) : null, value = row.value;
                            return row.reference ? {source: rule?.source_path || "Referenced field (details required)", target: rule ? `${rule.target_doctype}.${rule.target_field}` : "Not provided", app: "Not provided", index}
                                : {source: `${value.source?.doctype || ""}.${value.source?.field || ""}`, target: item.direction === "FRAPPE_TO_MUTATION" ? `${value.target?.operation || item.operation_name} / ${value.target?.argument || ""}.${value.target?.field_path ?? value.target?.field ?? ""}` : `${value.target?.doctype || ""}.${value.target?.field_path ?? value.target?.field ?? ""}`, app: (item.direction === "FRAPPE_TO_MUTATION" ? value.source?.app : value.target?.app) || "Not provided", index};
                        });
                    if (!projected.length) projected.push({source: "No configured field", target: "Not provided", app: "Not provided", index: 0});
                    projected.forEach((row) => {
                        const targetDoctype = kind === "frappe" ? item.target_doctype : item.direction === "FRAPPE_TO_MUTATION" ? definition.source?.doctype : definition.target?.doctype;
                        if (row.app === "Not provided") row.app = apps.get(targetDoctype || row.target.split(".")[0]) || row.app;
                        if (needle && !`${row.source} ${row.target} ${row.app}`.toLowerCase().includes(needle)) return;
                        const isSelected = active?.name === item.name && active?.mapping_type === kind && selected === row.index;
                        const select = () => {
                            if (locked()) return;
                            if (active?.name === item.name && active?.mapping_type === kind) { selected = row.index; renderDetail(); }
                            else leave(async () => { await open(item, kind, false); if (active?.name === item.name) { selected = row.index; renderDetail(); } });
                        };
                        const tr = $("<tr tabindex='0'></tr>").toggleClass("is-selected", isSelected).toggleClass("is-draft", isDraft).attr({"aria-selected": String(isSelected), "aria-label": format("{0}: {1} to {2}", item.operation_name || item.operation, row.source, row.target), "data-mapping-name": item.name});
                        tr.on("click", select).on("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(); } });
                        for (const value of [isDraft ? text("Personal draft") : item.status || "Active", item.operation_name || item.operation, row.source, row.app, row.target, item.revision]) tr.append($("<td></td>").text(String(value ?? "Not provided")));
                        table.find("tbody").append(tr);
                    });
                });
                if (!table.find("tbody").children().length) table.find("tbody").append($("<tr><td colspan='6'></td></tr>").find("td").text(text("No active mappings match this context or filter. Try another snapshot, search or page.")).end());
                body.find(".hero-mapping-context").trigger("hero-mapping-summary-refresh");
            };
            // Draft edits redraw the rows in place; the handler is kept per tab like before.
            rowChangeHandler = () => { if (data && section[0].isConnected) draw(); };
            root.off("hero-mapping-content-change").on("hero-mapping-content-change", rowChangeHandler);
            const load = async () => {
                const n = ++sequence; pending = true;
                table.find("tbody").empty().append($("<tr><td colspan='6'></td></tr>").find("td").text(text("Loading active mappings…")).end());
                try {
                    const result = await rpc("frontend_mapping_list", {search: searchInput.val(), start: offset, page_size: 20, direction: directionForTab(tab)});
                    if (!valid(n)) return;
                    data = result;
                    const needed = new Set(data.graphql_mappings.flatMap((item) => item.definition?.mapping_definition_ids || []));
                    data.frappe_mappings.forEach((rule) => ruleCache.set(rule.definition_id, rule));
                    for (let start = 0; [...needed].some((id) => !ruleCache.has(id)) && start <= 10000; start += 100) {
                        const rules = await rpc("frontend_mapping_list", {search: "", start, page_size: 100, direction: "QUERY_TO_FRAPPE"});
                        if (!valid(n)) return;
                        rules.frappe_mappings.forEach((rule) => ruleCache.set(rule.definition_id, rule));
                        if (start + 100 >= rules.pagination.frappe_total) break;
                    }
                    if (admin) {
                        try { const catalog = await readableApps(); if (!valid(n)) return; apps = new Map(catalog.doctypes.filter((item) => item.selectable).map((item) => [item.doctype, item.app_name])); }
                        catch (_) { /* Missing app metadata is explicitly marked, never invented. */ }
                    }
                    const chosen = operation.val(); operation.find("option:not(:first)").remove();
                    [...new Set([...data.graphql_mappings.map((item) => item.operation_name), ...data.frappe_mappings.map((item) => item.operation)])].forEach((name) => operation.append($("<option></option>").val(name).text(name)));
                    operation.val(chosen || ""); if (operation.val() === null) operation.val(""); draw();
                    const total = Math.max(data.pagination.graphql_total, data.pagination.frappe_total);
                    pager.empty().append(button("Previous mappings", () => { offset = Math.max(0, offset - 20); load(); }).prop("disabled", !offset),
                        $("<span></span>").text(`${text("Definition page")} ${Math.floor(offset / 20) + 1} · ${text("Total definitions / field rules")}: ${data.pagination.graphql_total} / ${data.pagination.frappe_total}`),
                        button("Next mappings", () => { offset += 20; load(); }).prop("disabled", offset + 20 >= total || offset >= 10000));
                } catch (error) { if (valid(n)) { table.find("tbody").empty().append($("<tr><td colspan='6'></td></tr>").find("td").text(errorText(error)).append(button("Retry mappings", load)).end()); } }
                finally { if (valid(n)) pending = false; }
            };
            const apply = () => { search = searchInput.val(); fieldFilter = fieldSearch.val(); offset = 0; load(); };
            filters.append(button("Search mappings", apply)); operation.on("change", draw); fieldSearch.on("input", () => { fieldFilter = fieldSearch.val(); if (data) draw(); });
            searchInput.on("keydown", (event) => { if (event.key === "Enter") apply(); });
            section.attr("data-field-picker", "").on("hero-mapping-picker-resume", () => { tableEpoch = epoch; sequence++; if (pending) load(); });
            load();
            return section;
        }
        function renderDetail() {
            toolbar.detach().empty().append(button("Browse mappings", () => leave(() => loadList(false))));
            body.empty();
            const draftStatus = $("<div class='hero-mapping-draft-status'></div>");
            if (admin && active.mapping_type === "graphql") {
                if (!active.parentless) toolbar.append(button("Active version", () => leave(() => { epoch++; ruleCache.clear(); model = null; mode = "active"; renderDetail(); })),
                    button("Open my draft", openDraft));
                toolbar.append(button("My saved revisions", () => leave(() => showRevisions(0))));
                if (mode === "draft") {
                    if (!active.parentless) toolbar.append(button("Compare active changes", compareActiveChanges).prop("disabled", Boolean(model.detailStale)));
                    toolbar.append(button("Save draft", save, true).attr("data-save-draft", ""), button("Discard saved draft", discard));
                    toolbar.append(button("Preview saved operation plan", previewDraftPlan).prop("disabled", model.dirty || model.blocked));
                    const reconciliationState = model.reconciliation ? "Local reconciliation changes — not saved"
                        : model.saved.reconciliation_json ? "Saved reconciliation evidence is current only for its recorded draft content."
                            : "No saved reconciliation evidence.";
                    draftStatus.append($("<p data-draft-state></p>"), $("<p class='text-muted'></p>").text(`${text("Personal draft revision")}: ${model.saved.content_revision} · ${text("Generation")}: ${model.saved.generation}`),
                        $("<p class='text-muted' data-reconciliation-state></p>").text(text(reconciliationState)));
                    if (model.blocked) toolbar.append(button("Check saved draft", reconcile), button("Reload saved draft", reloadDraft));
                }
            }
            renderWorkspaceContext();
            const value = content();
            renderContext(value);
            const editable = mode === "draft";
            const list = rows(value);
            const contextSection = body.find(".hero-mapping-context").last();
            const removeRow = (row) => {
                if (!row) return;
                if (row.additional) model.removeAdditional(row.index); else model.remove(row.index);
                selected = 0; renderDetail();
                root.find("[data-delete-mapping]").trigger("focus");
                say("Mapping removed locally. Undo is available until a successful save.");
            };
            // Personal draft actions live directly in Map Fields; there is no separate lower draft area.
            if (editable && model) {
                const actions = $("<div class='hero-mapping-draft-actions' role='group'></div>").attr("aria-label", text("Personal draft actions"));
                if (!model.creation) {
                    actions.append(button("Create Mapping", () => { model.creation = {}; renderDetail(); }).attr("data-create-mapping", ""));
                } else {
                    actions.append($("<p class='hero-mapping-draft-hint'></p>").text(text("Choose source and target for the new mapping; existing mappings remain unchanged.")),
                        button("Add mapping to draft", () => {
                            try { model.createAdditional(); selected = rows(model.content).length - 1; renderDetail(); say("New mapping added locally. Save the draft to keep it."); }
                            catch (error) { contextSection.find("[role='status']").first().text(text(error.message)); }
                        }, true).attr("data-create-mapping", ""),
                        button("Cancel new mapping", () => { model.creation = null; renderDetail(); }));
                }
                actions.append(button("Delete Mapping", () => removeRow(list[selected])).attr("data-delete-mapping", "").prop("disabled", !list[selected] || Boolean(model.creation)));
                if (model.undo.length) actions.append(button(format("Undo removal ({0})", model.undo.length), () => { model.restore(); renderDetail(); }));
                if (Array.isArray(value.mapping_definition_ids)) actions.append(button("Add existing field rule", addReference));
                else if (!value.source && !value.target && active.direction === "FRAPPE_TO_MUTATION") actions.append(button("Add field mapping", () => { model.set(["source"], {}); model.set(["target"], {}); renderDetail(); }).prop("disabled", model.undo.length > 0));
                actions.append(button("Activate Mapping", activateDraft).attr("data-activate-draft", ""));
                actions.append($("<p class='text-muted hero-mapping-contract-note'></p>").text(text("Delete changes the personal draft only. Saving keeps a personal draft; Activate publishes a saved valid draft and does not start synchronization.")));
                contextSection.append(actions);
            }
            // Transformation, rule parameters and definition settings stay available as collapsible details under Map Fields.
            const parameters = $("<details class='hero-mapping-parameters hero-mapping-rule-details'></details>")
                .append($("<summary></summary>").text(text("Rule details (transformation and parameters)")));
            const detail = $("<section class='hero-mapping-row-detail'></section>").attr("aria-label", text("Selected field mapping")).attr("data-editable", String(editable)).appendTo(parameters);
            for (const key of ["records_path", "record_path", "identity_path", "target_operation"]) {
                if (!(key in value)) continue;
                if (editable && typeof value[key] === "string") field(key.replaceAll("_", " "), value[key], (v) => model.set([key], v), parameters);
                else parameters.append($("<p></p>").text(`${key}: ${JSON.stringify(value[key])}`));
            }
            parameters.append($("<details></details>").append($("<summary></summary>").text(text("Configured structure and additional rules")), safeJson(value)));
            contextSection.append(parameters);
            if (list[selected]) renderRow(list[selected], detail);
            else detail.append($("<p></p>").text(text(list.length ? "Choose a field mapping row." : "No field mappings in this definition.")));
            renderMappingsTable(body);
            body.append(toolbar, draftStatus);
            toolbar.addClass("hero-mapping-workbench-actions");
            const tableEpoch = epoch;
            list.filter((row) => row.reference && !ownRule(value, row.id) && !ruleCache.has(row.id)).forEach(async (row) => {
                try {
                    const rule = await readActiveRule(row.id);
                    if (rule && !disposed && tableEpoch === epoch && !ruleCache.has(row.id)) { ruleCache.set(row.id, rule); rowChangeHandler?.(); }
                } catch (_) { /* The reference remains visible; details offer the error context. */ }
            });
            if (!admin) body.append($("<p class='text-muted'></p>").text(text("Employee read-only view")));
            updateDirty(); if (busy) root.find("button,input,select,textarea").prop("disabled", true);
            if (model && !canSaveDraft(model, active)) root.find("[data-save-draft]").prop("disabled", true);
        }
        function renderContext(value) {
            const mutation = tab === "Mutation", frappeSide = mutation ? "source" : "target";
            const context = $("<section class='hero-mapping-context'></section>").attr("aria-label", text("Map Fields")).append($("<h3></h3>").text(text("Map Fields")));
            const fieldNotice = $("<p role='status'></p>");
            let openFieldDraft;
            if (admin && active && active.mapping_type === "graphql") {
                const guidance = $("<div class='hero-mapping-context-note' data-field-edit-mode></div>");
                if (mode === "draft" && model) guidance.append($("<p></p>").text(text("Personal draft: selecting a field changes this draft locally. Save draft to keep the changes.")));
                else {
                    openFieldDraft = button("Open my draft to edit fields", openDraft);
                    guidance.append($("<p></p>").text(text("Active mapping is read-only. Open your personal draft to change either field.")), openFieldDraft);
                }
                context.append(guidance.append(fieldNotice));
            }
            const sourcePane = $("<section class='hero-mapping-context-pane' data-mapping-side='source'></section>").append($("<h4></h4>").text(text(mutation ? "Source Frappe" : "Source HERO")));
            const targetPane = $("<section class='hero-mapping-context-pane' data-mapping-side='target'></section>").append($("<h4></h4>").text(text(mutation ? "Target HERO" : "Target Frappe")));
            const heroPane = mutation ? targetPane : sourcePane, frappePane = mutation ? sourcePane : targetPane;
            const heroControls = $("<div class='hero-mapping-selection-fields'></div>").appendTo(heroPane);
            const frappeControls = $("<div class='hero-mapping-selection-fields'></div>").appendTo(frappePane);
            const browseKey = `${tab}:${active?.definition_id || ""}:${selected}`;
            const chosenSnapshot = active?.schema_snapshot || snapshotFilter;
            const heroBrowseKey = `${tab}:${chosenSnapshot || ""}`;
            const selectedObject = () => browseObjects.get(heroBrowseKey) || "";
            const selectedAction = () => browseActions.get(heroBrowseKey) || active?.operation_name || "";
            const selectedArea = () => browseAreas.get(heroBrowseKey) || "All areas";
            const refreshContext = () => active ? renderDetail() : renderList();
            const row = rows(value)[selected] || {};
            const existingRule = !model?.creation && row.reference ? ownRule(value, row.id) || ruleCache.get(row.id) : null;
            const savedSource = model?.creation?.source || (row.additional ? row.value?.source : value.source) || {};
            const savedObject = row.reference ? value.field_contexts?.[row.id]?.object_type : savedSource.object_type;
            if (!mutation && savedObject && !browseObjects.has(heroBrowseKey)) browseObjects.set(heroBrowseKey, savedObject);
            const configured = model?.creation ? (model.creation[frappeSide] || {}) : row.additional ? row.value[frappeSide] || {} : value[frappeSide] || {};
            let browseApp = model?.creation?.app || selectedPickerApp(model, frappeSide, configured, admin) || "";
            let browseDoctype = existingRule?.target_doctype || configured.doctype || "", browseChanged = false;
            let fieldCatalog;
            const catalogEpoch = epoch, contractState = $("<p class='text-muted' role='status'></p>").appendTo(frappePane);
            const getFieldCatalog = async () => {
                if (!admin) return {apps: [], doctypes: [], fields: []};
                if (!fieldCatalog) {
                    const method = mutation && !row.reference && chosenSnapshot ? "writeback_mapper_catalog" : "mapping_target_catalog";
                    fieldCatalog = rpc(method, method === "writeback_mapper_catalog" ? {snapshot_name: chosenSnapshot} : {}).then((result) => {
                        if (method === "writeback_mapper_catalog" && !disposed && epoch === catalogEpoch && context[0].isConnected) contractState.text(text(writebackContractLabel(result, active?.operation_name)));
                        return result;
                    }).catch((error) => { fieldCatalog = null; throw error; });
                }
                return fieldCatalog;
            };
            const page = (items, search, offset) => {
                const needle = search.toLowerCase();
                const filtered = items.filter((item) => `${item.label} ${item.context || ""} ${item.searchText || ""}`.toLowerCase().includes(needle));
                return {items: filtered.slice(offset, offset + 20), total: filtered.length};
            };
            const setField = (f, isFrappe) => {
                if (mode !== "draft" || !model) {
                    const message = "Open my draft before changing a field. Browsing has not saved or created anything.";
                    fieldNotice.text(text(message)); say(message); openFieldDraft?.trigger("focus"); return;
                }
                if (model.creation) {
                    if (isFrappe) {
                        model.creation[frappeSide] = {doctype: f.target_doctype || browseDoctype, field: f.path};
                        model.creation.app = browseApp;
                    } else if (mutation) {
                        if (selectedAction() !== active.operation_name) { fieldNotice.text(text("Choose the matching saved definition before editing this action.")); return; }
                        model.creation.target = {operation: selectedAction(), argument: f.argument, input_type: f.argument_input_type, field: f.path, graphql_type: f.type};
                    } else model.creation.source = {field: f.path, object_type: f.object_type, schema_context: f.schema_context};
                    renderDetail(); return;
                }
                if (row.additional) { fieldNotice.text(text('Use New mapping to add another assignment.')); return; }
                if (row.reference && !ownRule(model.content, row.id)) { say("This referenced active rule is read-only. Its existing personal-rule contract is required before editing."); return; }
                if (row.reference) {
                    if (isFrappe) { const target = f.target_doctype || browseDoctype; model.referenceApps ||= Object.create(null); model.referenceApps[row.id] = {app: browseApp, doctype: target}; model.set(["field_rules", row.id, "target_doctype"], target); model.set(["field_rules", row.id, "target_field"], f.path); }
                    else { model.set(["field_rules", row.id, "source_path"], f.path); model.set(["field_rules", row.id, "source_type"], f.mapping_type || f.fieldtype); model.set(["field_contexts", row.id], {object_type: f.object_type, schema_context: f.schema_context}); }
                } else if (isFrappe && mutation) model.setFrappeSource(browseApp, f.target_doctype || browseDoctype, f.path);
                else if (isFrappe) model.setFrappeTarget(browseApp, f.target_doctype || browseDoctype, f.path);
                else if (!mutation) model.setHeroSource(f);
                else { if (selectedAction() !== active.operation_name) { fieldNotice.text(text("Choose the matching saved definition before editing this action.")); return; } model.set(["target", "argument"], f.argument); model.set(["target", "operation"], selectedAction()); model.set(["target", "field"], f.path); if (Object.hasOwn(model.content.target, "field_path")) model.set(["target", "field_path"], f.path); model.set(["target", "graphql_type"], f.type); }
                renderDetail();
                body.find("[data-choice-label]").filter((_, element) => element.dataset.choiceLabel === (isFrappe ? "Frappe Field" : mutation ? "Input field" : "HERO Field")).find("summary").trigger("focus");
            };
            let areaChoice;
            if (!mutation) areaChoice = choice("Area", text(selectedArea()), heroControls, async (search, offset) =>
                page(mappingAreas.map((label) => ({label})), search, offset), (item) => {
                    if (item.label !== selectedArea()) {
                        browseAreas.set(heroBrowseKey, item.label);
                        browseObjects.set(heroBrowseKey, "");
                        browsePaths.delete(browseKey);
                    }
                    refreshContext();
                    say("Area selected for browsing. Saved mappings and fetch configuration are unchanged.");
                });
            const objectOrAction = choice(mutation ? "Action" : "Object", "", heroControls, async (search, offset) => {
                if (!admin || !chosenSnapshot) return {items: [], total: 0, emptyMessage: "Choose a validated schema snapshot first."};
                if (mutation) {
                    const catalog = await rpc("schema_operation_catalog", {snapshot_name: chosenSnapshot, operation_kind: "MUTATION"});
                    return page(catalog.map((item) => ({label: item.operation_name, context: "Mutation action"})), search, offset);
                }
                const catalog = await rpc("mapping_object_catalog", {snapshot_name: chosenSnapshot, object_type: "", search: "", start: 0, page_size: 1});
                return page(objectsInArea(catalog.objects, selectedArea()).map((item) => ({...item, label: item.label || item.name})), search, offset);
            }, (item) => {
                if (mutation) browseActions.set(heroBrowseKey, item.label); else browseObjects.set(heroBrowseKey, item.name);
                browsePaths.delete(browseKey); refreshContext();
                root.find("[data-choice-label]").filter((_, element) => element.dataset.choiceLabel === (mutation ? "Action" : "Object")).find("summary").trigger("focus");
                say(mutation ? "Action selected for browsing. Choose its matching definition before saving." : "Object selected for browsing. Field selection does not grant access or change the fetch configuration.");
            });
            const path = choice(mutation ? "Argument / Input" : "Relationship / Path", "", heroControls, async (search, offset) => {
                if (!admin || !chosenSnapshot) return {items: [], total: 0, emptyMessage: "Choose a validated schema snapshot first."};
                let paths;
                if (mutation) {
                    if (!selectedAction()) return {items: [], total: 0, emptyMessage: "Choose an action first."};
                    const catalog = await rpc("schema_operation_catalog", {snapshot_name: chosenSnapshot, operation_kind: "MUTATION"});
                    const operation = catalog.find((item) => item.operation_name === selectedAction());
                    paths = Object.entries(operation?.input_fields || {}).map(([argument]) => ({label: `${argument} / ${actionInputType(operation, argument) || "Input"}`, prefix: argument + "."}));
                } else {
                    if (!selectedObject()) return {items: [], total: 0, emptyMessage: "Choose a HERO object first."};
                    const pathsByName = new Map([["All paths", ""]]);
                    for (let start = 0; start <= 10000; start += 50) {
                        const result = await rpc("mapping_object_catalog", {snapshot_name: chosenSnapshot, object_type: selectedObject(), search: "", start, page_size: 50});
                        result.items.forEach((field) => { const parent = field.path.split(".").slice(0, -1).join("."); if (parent) pathsByName.set(parent, parent + "."); });
                        if (start + 50 >= result.total) break;
                        if (start === 10000) throw new Error("Readable path range exceeded");
                    }
                    paths = [...pathsByName].map(([label, prefix]) => ({label, prefix}));
                }
                paths = paths.filter((item) => item.label.toLowerCase().includes(search.toLowerCase()));
                return {items: paths.slice(offset, offset + 20), total: paths.length};
            }, (item) => {
                browsePaths.set(browseKey, item); path.find("summary strong").text(item.label).attr("title", item.label);
                say("Path selected for browsing. Mapping content is unchanged; choose a field in your personal draft to edit it.");
            });
            const heroField = choice(mutation ? "Input field" : "HERO Field", "", heroControls, async (search, offset) => {
                if (!admin || !chosenSnapshot) return {items: [], total: 0, emptyMessage: "Choose a validated schema snapshot first."};
                if (!mutation) {
                    if (!selectedObject()) return {items: [], total: 0, emptyMessage: "Choose a HERO object first."};
                    const prefix = browsePaths.get(browseKey)?.prefix || "";
                    if (prefix) {
                        let fields = [];
                        for (let start = 0; start <= 10000; start += 50) {
                            const part = await rpc("mapping_object_catalog", {snapshot_name: chosenSnapshot, object_type: selectedObject(), search: "", start, page_size: 50});
                            fields.push(...part.items);
                            if (start + 50 >= part.total) break;
                            if (start === 10000) throw new Error("Readable field range exceeded");
                        }
                        return page(fields.filter((f) => f.path.startsWith(prefix)).map((f) => ({...f, label: f.path, context: objectFieldContext(f), disabled: f.selectable === false})), search, offset);
                    }
                    const result = await rpc("mapping_object_catalog", {snapshot_name: chosenSnapshot, object_type: selectedObject(), search, start: offset, page_size: 20});
                    return {items: result.items.map((f) => ({...f, label: f.path, context: objectFieldContext(f), disabled: f.selectable === false})), total: result.total};
                }
                if (!selectedAction()) return {items: [], total: 0, emptyMessage: "Choose an action first."};
                const catalog = await rpc("schema_operation_catalog", {snapshot_name: chosenSnapshot, operation_kind: "MUTATION"});
                const operation = catalog.find((v) => v.operation_name === selectedAction());
                const items = Object.entries(operation?.input_fields || {}).flatMap(([argument, list]) => list.map((f) => ({...f, argument, argument_input_type: actionInputType(operation, argument), fieldtype: f.type, label: `${argument}.${f.path}`, context: `${f.type || ""}${f.nullable === false ? " · required" : ""}${f.select_options?.length ? ` · enum: ${f.select_options.join(", ")}` : ""}`, disabled: f.selectable === false})));
                const prefix = browsePaths.get(browseKey)?.prefix || "";
                return page(items.filter((item) => !prefix || item.label.startsWith(prefix)), search, offset);
            }, (item) => setField(item, false));
            if (!mutation) heroPane.append($('<p class="text-muted"></p>').text(text("Field selection does not grant read access. Inclusion in the approved fetch configuration must be checked separately.")));
            const app = choice("App", "", frappeControls, async (search, offset) => {
                const catalog = await getFieldCatalog();
                return page(catalog.apps.map((item) => ({label: item.app_name || item})), search, offset);
            }, (item) => { if (browseApp !== item.label) { browseApp = item.label; browseDoctype = ""; browseChanged = true; } refresh(); });
            const doctype = choice("DocType", "", frappeControls, async (search, offset) => {
                const catalog = await getFieldCatalog();
                return page(catalog.doctypes.filter((item) => item.app_name === browseApp).map((item) => ({...item, label: item.doctype, context: item.selectable ? "" : item.reason || "Unavailable", disabled: !item.selectable})), search, offset);
            }, (item) => { if (browseDoctype !== item.doctype) { browseDoctype = item.doctype; browseChanged = true; } refresh(); });
            const frappeField = choice("Frappe Field", "", frappeControls, async (search, offset) => {
                const catalog = await getFieldCatalog();
                const selectedDoc = catalog.doctypes.find((item) => item.app_name === browseApp && item.doctype === browseDoctype && item.selectable);
                if (!selectedDoc) return {items: [], total: 0, emptyMessage: "Choose an available App and DocType before choosing a Frappe field."};
                if (mutation && !row.reference) return page((catalog.fields || []).filter((f) => f.app_name === browseApp && f.doctype === browseDoctype).map((f) => ({...f, label: f.path, searchText: f.description || "", context: `${f.label || ""} · ${f.fieldtype || ""}${reviewedTargetLabel(f) ? ` · ${text("Reviewed HERO target")}: ${reviewedTargetLabel(f)}` : ""}${f.selectable === false ? ` · ${f.reason || "not permitted"}` : ""}`, disabled: f.selectable === false})), search, offset);
                const result = await rpc("mapping_target_catalog_page", {app_name: browseApp, doctype: browseDoctype, search, start: offset, page_size: 20});
                return {items: result.items.map((f) => ({...f, label: f.path, context: `${f.fieldtype || f.type || ""}${f.selectable === false ? ` · ${f.reason || "not permitted"}` : ""}`, disabled: f.selectable === false})), total: result.total};
            }, (item) => setField(item, true));
            if (!admin) [areaChoice, objectOrAction, path, heroField, app, doctype, frappeField].forEach((control) => control?.prop("hidden", true));
            const metadata = (pane) => {
                const dl = $("<dl class='hero-mapping-metadata'></dl>").appendTo(pane), fields = {};
                for (const label of ["Name", "Type", "Node", "Revision", "Status"]) { fields[label] = $("<dd></dd>").text(text("Not provided")); dl.append($("<dt></dt>").text(text(label)), fields[label]); }
                fields.ID = $("<dd></dd>").text(text("Not provided"));
                pane.append($("<details></details>").append($("<summary></summary>").text(text("Technical details")), $("<dl></dl>").append($("<dt></dt>").text(text("ID")), fields.ID)));
                return fields;
            };
            const heroMeta = metadata(heroPane), frappeMeta = metadata(frappePane);
            const relationship = $('<section class="hero-mapping-context-note"></section>').attr("aria-label", text("Relationship details"));
            let appNames = new Map();
            const refresh = () => {
                const value = active ? content() : {}, row = rows(value)[selected], rule = !model?.creation && row?.reference ? ownRule(value, row.id) || ruleCache.get(row.id) : null;
                const assignment = model?.creation || (row?.additional ? row.value : value);
                const frappeValue = assignment[frappeSide] || {};
                if (!browseApp && browseDoctype) browseApp = appNames.get(browseDoctype) || (!admin && frappeValue.app) || "";
                const setChoiceValue = (element, label) => element.find("summary strong").text(label).attr("title", label);
                setChoiceValue(app, browseApp || text("Choose…"));
                setChoiceValue(doctype, browseDoctype || text("Choose…"));
                setChoiceValue(objectOrAction, (mutation ? selectedAction() : selectedObject()) || text("Choose…"));
                setChoiceValue(frappeField, browseChanged ? text("Choose…") : rule?.target_field || frappeValue.field_path || frappeValue.field || text("Choose…"));
                const currentObject = row?.reference ? value.field_contexts?.[row.id]?.object_type : assignment.source?.object_type;
                const objectMismatch = !mutation && currentObject && selectedObject() && currentObject !== selectedObject();
                setChoiceValue(heroField, objectMismatch ? text("Choose…") : rule?.source_path || (mutation ? assignment.target?.field_path || assignment.target?.field : assignment.source?.field) || text("Choose…"));
                const savedPath = rule?.source_path || assignment.source?.field || "";
                setChoiceValue(path, browsePaths.get(browseKey)?.label || (mutation ? [assignment.target?.argument, assignment.target?.input_type].filter(Boolean).join(" / ") : !objectMismatch && savedPath.includes(".") ? savedPath.split(".").slice(0, -1).join(".") : "All paths") || text("Not provided"));
                heroMeta.ID.text(rule?.definition_id || active?.definition_id || text("Not provided"));
                heroMeta.Name.text((mutation ? selectedAction() : selectedObject()) || text("Not provided"));
                heroMeta.Type.text(rule?.source_type || (mutation ? value.target?.graphql_type || value.target?.input_type : value.source?.type) || text("Not provided"));
                heroMeta.Node.text(rule?.source_path || (mutation ? value.target?.argument : value.records_path || value.record_path) || text("Not provided"));
                for (const fields of [heroMeta, frappeMeta]) { fields.Revision.text(active ? `${text("Mapping")} ${active.revision}` : text("Not provided")); fields.Status.text(active ? mode === "draft" ? text("Personal draft") : active.status || text("Active") : text("No selection")); }
                frappeMeta.ID.text(text("Not provided")); frappeMeta.Name.text(rule?.target_field || frappeValue.field_path || frappeValue.field || text("Not provided"));
                frappeMeta.Type.text(frappeValue.fieldtype || text("Not provided")); frappeMeta.Node.text(rule?.target_doctype || frappeValue.doctype || text("Not provided"));
                const fieldContext = row?.reference ? value.field_contexts?.[row.id] : assignment.source;
                const relation = !mutation && relationshipDescription(rule, value.target_operation, fieldContext);
                relationship.empty().prop("hidden", !relation);
                if (relation) {
                    relationship.append($('<h4></h4>').text(text("Relationship")));
                    for (const [label, detail] of [["Source", relation.source], ["Target", relation.target], ["Role", relation.role],
                        ["Identity", relation.identity], ["Cardinality", relation.cardinality], ["Status", relation.state]])
                        relationship.append($('<p></p>').append($('<strong></strong>').text(`${text(label)}: `), document.createTextNode(text(detail || "Not provided"))));
                }
            };
            context.on("hero-mapping-summary-refresh", refresh).on("hero-mapping-selection-change", () => renderDetail());
            context.append(sourcePane, $("<span class='hero-mapping-arrow' aria-hidden='true'>→</span>"), targetPane, relationship).appendTo(body); refresh();
            if (admin && active) { const requestEpoch = epoch; readableApps().then((catalog) => {
                if (disposed || requestEpoch !== epoch || !context[0].isConnected) return;
                appNames = new Map(catalog.doctypes.filter((item) => item.selectable).map((item) => [item.doctype, item.app_name])); refresh();
            }).catch(() => {}); }
            if (!active) context.append($("<p class='text-muted hero-mapping-context-note'></p>").text(text("Choose a definition or mapping row. Unavailable metadata is marked Not provided.")));
        }
        async function renderRow(row, panel) {
            const selectionRequest = ++detailRequest;
            panel.empty(); panel.attr("data-row-id", row.id);

            if (row.reference && mode === "draft" && ownRule(model.content, row.id)) {
                const rule = model.content.field_rules[row.id];
                panel.append($("<h4></h4>").text(text("Personal field rule")));
                for (const [label, key] of [["Source path", "source_path"], ["Source type", "source_type"], ["Target DocType", "target_doctype"],
                    ["Target field", "target_field"], ["Transformation", "transformation"], ["Purpose", "purpose"], ["Relation", "relation"]]) {
                    if (key.startsWith("source_") || key.startsWith("target_")) continue;
                    const destination = panel;
                    field(label, rule[key] ?? "", (next) => model.set(["field_rules", row.id, key], next), destination);
                }
                const priority = field("Priority", rule.priority ?? "", (next) => model.set(["field_rules", row.id, "priority"], next === "" ? null : Number(next)), panel);
                priority.attr({type: "number", step: "1", min: "-1000000", max: "1000000"});
                panel.append($("<p class='text-muted'></p>").text(text("Changes belong to your personal draft. The active field rule stays unchanged.")));
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
            if (row.additional) { panel.append(safeJson(value)); return; }
            if (mode !== "draft" || panel.attr("data-editable") !== "true") { panel.append(safeJson(value)); return; }
            const mutation = mutationMapping(value);
            const descriptors = mutation ? [
                ["Source Frappe DocType", ["source", "doctype"]], ["Source Frappe field", ["source", "field"]],
                ["Target HERO argument", ["target", "argument"]], ["Target HERO input field", ["target", Object.hasOwn(value.target || {}, "field_path") ? "field_path" : "field"]],
            ] : [
                ["Source HERO field", ["source", "field"]], ["Target Frappe DocType", ["target", "doctype"]],
                ["Target Frappe field path", ["target", Object.hasOwn(value.target || {}, "field_path") ? "field_path" : "field"]],
            ];
            if (!mutation && Object.hasOwn(value.source || {}, "doctype")) descriptors.push(["Stored HERO source context", ["source", "doctype"], true]);
            if (!mutation && Object.hasOwn(value.target || {}, "argument")) descriptors.push(["Stored target argument", ["target", "argument"], true]);
            descriptors.push(["Transformation", ["transformation"], true]);
            for (const [label, path, advanced] of descriptors) {
                const v = path.reduce((a, key) => a?.[key], value);
                if (v !== undefined && typeof v !== "string") { panel.append($("<p></p>").text(`${label}: ${text("Configured special rule; preserved unchanged")}`)); continue; }
                if (!advanced) continue;
                const destination = panel;
                field(label, v || "", (next) => { model.set(path, next); if (path[0] === "target" && path[1] === "field_path" && Object.hasOwn(value.target, "field")) model.set(["target", "field"], next); }, destination).prop("readOnly", !advanced && ["doctype", "argument"].includes(path[1]));
            }
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
        async function previewDraftPlan() {
            if (locked() || !model || model.dirty || !canSaveDraft(model, active)) return;
            const state = model, request = ++epoch, args = state.saveArgs();
            setBusy(true); say("Preparing a non-executable plan…");
            try {
                const result = await rpc("mapping_draft_materialize_plan", {
                    definition_id: args.definition_id, expected_generation: args.expected_generation,
                    expected_draft_revision: args.expected_draft_revision, expected_active_revision: args.expected_active_revision,
                    expected_reconciliation_revision: args.expected_reconciliation_revision}, true);
                if (disposed || request !== epoch || state !== model) return;
                if (result.execution_authority !== "NONE") throw new Error("Unexpected plan authority.");
                const dialog = $("<dialog class='hero-mapping-dialog'></dialog>");
                dialog.append($("<h3></h3>").text(text("Saved operation plan")),
                    $("<p></p>").text(text("Preview only. This plan grants no execution permission.")), safeJson(result),
                    button("Close", () => { dialog[0].close(); dialog.remove(); }));
                dialog.on("cancel", () => setTimeout(() => dialog.remove(), 0));
                root.append(dialog); dialog[0].showModal(); say("Operation plan ready; no operation was executed.");
            } catch (error) { if (!disposed && request === epoch) say(errorText(error)); }
            finally { if (!disposed && request === epoch) setBusy(false); }
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
        function activateDraft() {
            if (locked() || !model || model.dirty || model.reconciliation || !model.saved.content_revision || !canSaveDraft(model, active)) return;
            const state = model, selectedActive = active, d = state.saved;
            const warning = d.warning_fingerprint || '';
            if (warning && (!Array.isArray(d.warnings) || !d.warnings.length)) {
                say('The current warnings are unavailable. Reload the saved draft before activation.'); return;
            }
            modal('Activate personal draft', warning
                ? `Publish the saved draft and acknowledge these warnings: ${d.warnings.join('; ')}. No HERO synchronization will be started.`
                : 'Publish the saved personal draft? No HERO synchronization will be started.', [
                ['Activate saved draft', async () => {
                    if (locked() || model !== state || state.dirty || state.reconciliation || !canSaveDraft(state, active)) return;
                    const request = ++epoch; setBusy(true); let detail = null;
                    try {
                        const result = await rpc('mapping_draft_publish', {definition_id: d.definition_id,
                            expected_generation: d.generation, expected_draft_revision: d.content_revision,
                            expected_active_revision: state.verifiedActiveRevision ?? d.base_active_revision,
                            expected_reconciliation_revision: d.reconciliation_revision || 0,
                            warning_fingerprint: warning, confirmed: true}, true);
                        if (disposed || request !== epoch) return;
                        const name = publicationResultName(result, selectedActive);
                        const observed = await rpc('frontend_mapping_detail', {mapping_type: 'graphql', name});
                        if (disposed || request !== epoch) return;
                        if (!publicationReadbackMatches(observed, result, selectedActive, d.definition_id)) throw new Error('Active readback differs from publication.');
                        detail = observed; model = null; mode = 'active';
                    } catch (error) {
                        if (!disposed && request === epoch) { state.blocked = true; say('Publication failed or its outcome is unknown. Check the active version before trying again.'); }
                    } finally { if (!disposed && request === epoch) setBusy(false); }
                    if (!disposed && request === epoch) {
                        if (detail) { active = {...detail, mapping_type: 'graphql'}; ruleCache.clear(); selected = 0; renderDetail(); say('Saved personal draft published.'); }
                        else renderDetail();
                    }
                }, true], ['Cancel', () => {}],
            ]);
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
            panel.prepend(toolbar.detach());
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
                    const select = $("<select class='form-control'></select>").attr("aria-label", text("Conflict resolution"))
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
                        if (disposed || request !== epoch) return;
                        if (active.parentless) { model = null; setBusy(false); await loadList(false); say("Personal draft discarded."); return; }
                        model = null; mode = "active"; say("Personal draft discarded.");
                    } catch (error) { say(errorText(error)); }
                    finally { if (!disposed && request === epoch) { setBusy(false); renderDetail(); } }
                }], ["Continue editing", () => {}],
            ]);
        }
        async function showRevisions(offset) {
            const request = ++epoch; revisionsStart = offset; say("Loading your saved revisions…");
            try { const result = await rpc("mapping_draft_revisions", {definition_id: active.definition_id, start: revisionsStart, page_size: 20});
                if (disposed || request !== epoch) return;
                panel.prepend(toolbar.detach());
                toolbar.empty().append(button("Back to definition", () => { mode = model ? "draft" : "active"; renderDetail(); }));
                body.empty().append($("<h3></h3>").text(text("My saved revisions — read only")));
                if (!result.items.length) body.append($("<p></p>").text(text("No saved personal revisions.")));
                result.items.forEach((item) => body.append($("<details></details>").append($("<summary></summary>").text(`${text("Generation")} ${item.generation} · ${text("Revision")} ${item.revision}`), safeJson(typeof item.content_json === "string" ? parseContent(item.content_json) : item.content))));
                body.append(button("Previous revisions", () => showRevisions(Math.max(0, revisionsStart - 20))).prop("disabled", !revisionsStart),
                    button("Next revisions", () => showRevisions(revisionsStart + 20)).prop("disabled", revisionsStart + 20 >= result.total)); say("");
            } catch (error) { if (!disposed && request === epoch) say(errorText(error)); }
        }
        async function showBindings(offset) {
            if (!admin) return;
            const request = ++epoch; pendingView = {kind: "binding"};
            toolbar.detach().empty(); body.empty();
            const header = $("<div class='hero-mapping-workspace-context'></div>").appendTo(body);
            renderDefinitionChooser(header, "Binding definition", true);
            body.append($("<h3></h3>").text(text("Bindings — read only")), $("<p class='text-muted'></p>").text(text("Existing binding identities, mappings and status. No write actions are available.")));
            const linksHost = $("<section></section>").appendTo(body);
            let linkRequest = 0;
            async function loadLinks(start) {
                const linkSerial = ++linkRequest;
                linksHost.empty().append($("<h4></h4>").text(text("Existing integration links")),
                    $("<p class='text-muted'></p>").text(text("Object links are separate from field bindings and are not filtered by the selected definition.")));
                const status = $("<p></p>").text(text("Loading integration links…")).appendTo(linksHost);
                try {
                    const data = await rpc("frontend_integration_links", {start, page_size: 20});
                    if (disposed || request !== epoch || linkSerial !== linkRequest) return;
                    status.remove();
                    const table = $("<table class='table table-sm hero-mapping-table'><thead><tr><th>Status</th><th>Source instance</th><th>Source type</th><th>Source ID</th><th>Target</th><th>Last synchronized</th></tr></thead><tbody></tbody></table>").appendTo(linksHost);
                    table.find("th").each((index, th) => { $(th).text(text($(th).text())); });
                    if (!data.items.length) table.find("tbody").append($("<tr><td colspan='6'></td></tr>").find("td").text(text("No existing integration links.")).end());
                    data.items.forEach((item) => {
                        const row = $("<tr></tr>");
                        for (const value of [item.sync_status, item.source_instance || "default", item.source_type, item.source_id, `${item.target_doctype} · ${item.target_name}`, item.last_synced_on]) row.append($("<td></td>").text(String(value ?? text("Not provided"))));
                        table.find("tbody").append(row);
                    });
                    const p = data.pagination;
                    linksHost.append(button("Previous links", () => loadLinks(Math.max(0, p.start - p.page_size))).prop("disabled", !p.start),
                        $("<span></span>").text(`${text("Links")} ${p.total ? p.start + 1 : 0}–${Math.min(p.start + p.page_size, p.total)} / ${p.total}`),
                        button("Next links", () => loadLinks(p.start + p.page_size)).prop("disabled", p.start + p.page_size >= p.total));
                } catch (error) { if (!disposed && request === epoch && linkSerial === linkRequest) status.text(errorText(error)); }
            }
            loadLinks(0);
            body.append($("<h4></h4>").text(text("Field bindings for selected definition")));
            const wrap = $("<div class='hero-mapping-table-wrap'></div>").appendTo(body);
            const table = $("<table class='table table-sm hero-mapping-table'><thead><tr><th>Status</th><th>Direction</th><th>Source HERO</th><th>App</th><th>Target</th><th>Mapping revision</th><th>Binding revision</th><th>Identity</th></tr></thead><tbody></tbody></table>").appendTo(wrap);
            table.find("th").each((index, th) => { $(th).text(text($(th).text())); });
            const message = (value) => table.find("tbody").empty().append($("<tr><td colspan='8'></td></tr>").find("td").text(text(value)).end());
            if (!active?.definition_id) { message("Choose a binding definition above to load existing bindings."); say(""); pendingView = null; return; }
            message("Loading bindings…"); say("");
            try {
                const result = await rpc("frontend_mapping_bindings", {definition_id: active.definition_id, start: offset, page_size: 20});
                if (disposed || request !== epoch) return;
                table.find("tbody").empty();
                if (!result.items.length) message("No existing bindings use this mapping definition.");
                result.items.forEach((item) => {
                    const row = $("<tr></tr>");
                    for (const value of [item.sync_state || "UNACKNOWLEDGED", item.direction, `${item.source_instance}.${item.source_field}`, item.target_app, `${item.target_doctype}.${item.target_name}.${item.target_field}`, item.mapping_revision, item.binding_revision, item.identity_key]) row.append($("<td></td>").text(String(value ?? text("Not provided"))));
                    table.find("tbody").append(row);
                });
                const p = result.pagination;
                body.append($("<div class='hero-mapping-actions'></div>").append(button("Previous bindings", () => showBindings(Math.max(0, p.start - p.page_size))).prop("disabled", !p.start),
                    $("<span></span>").text(`${text("Bindings")} ${p.total ? p.start + 1 : 0}–${Math.min(p.start + p.page_size, p.total)} / ${p.total}`),
                    button("Next bindings", () => showBindings(p.start + p.page_size)).prop("disabled", p.start + p.page_size >= p.total)));
            } catch (error) { if (!disposed && request === epoch) { message(errorText(error)); body.append(button("Retry bindings", () => showBindings(offset))); } }
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
        const controller = {beforeLeave: leaveWorkspace, destroy() { shell.removeClass("hero-mapping-shell"); disposed = true; epoch++; window.removeEventListener("beforeunload", unload); if (router.route === guardedRoute) router.route = originalRoute; document.removeEventListener("click", links, true); document.removeEventListener("pointerdown", closeChoiceOutside, true); }};
        current = controller; loadList(); return controller;
    }
    window.hero_integration_mapping_editor = Object.freeze({mount, DraftState, stable, readActiveRule, readSnapshotPages, exchangeExpectedStates, exchangeCounts, exchangeCanAdopt, reviewedTargetLabel, writebackContractLabel, legacyWritebackRows, legacyWritebackDetailMatches, sharedWritebackDraftAccess, reviewedWritebackEditable, buildReviewedWriteback, sharedWritebackRequest, sharedWritebackSaveAccepted, finishSharedWritebackSave, legacyWritebackCurrent, legacyPackageSelection, legacyPackageAdoptionArgs, legacyCandidateArgs, legacyCandidateMatches, legacyCandidateRawMessage, legacyCandidateRpc, legacyCandidatePreviewText, isOwnedExportLink, mappingTabs, mappingAreas, objectsInArea, relationshipDescription, directionForTab, definitionChoice, objectFieldContext, actionInputType, bindingAccess, selectedPickerApp, hasCurrentDraftBase, canSaveDraft, publicationResultName, publicationReadbackMatches, tabKeyTarget, MappingTabStates, resumePendingView});
})();
