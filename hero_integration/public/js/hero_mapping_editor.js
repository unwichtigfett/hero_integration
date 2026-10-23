(() => {
    "use strict";

    if (window.hero_integration_mapping_editor) return;

    const PAGE_SIZE = 50;
    const statusClass = (status) => ["active", "draft"].includes(String(status || "").toLowerCase()) ? String(status).toLowerCase() : "invalid";

    function mount(page, routeContext = {}) {
        page.main.empty().addClass("hero-integration-ui");
        const snapshot = $("<select class='form-control' disabled><option value=''>Choose a saved GraphQL Schema Snapshot</option></select>");
        const snapshotMetadata = $("<div class='text-muted hero-mapping-snapshot-metadata' aria-live='polite'></div>");
        const operation = $("<select class='form-control' disabled><option value=''>Choose a snapshot query</option></select>");
        const operationSearch = $("<input class='form-control' placeholder='Filter snapshot queries' disabled>");
        const sourceSearch = $("<input class='form-control' placeholder='Name, path, type, or relation'>");
        const reverseSearch = $("<input class='form-control' placeholder='Filter suggested HERO fields' disabled>");
        const targetSearch = $("<input class='form-control' placeholder='Name, fieldname, type, or description' disabled>");
        const readOperationFilter = $("<select class='form-control' disabled><option value=''>All read operations</option></select>");
        const activeSearch = $("<input class='form-control' placeholder='Filter saved read mappings by field'>");
        const resetReadFilters = $("<button type='button' class='btn btn-default btn-sm' disabled>Reset filters</button>");
        const targetApp = $("<select class='form-control'><option value=''>Choose an app</option></select>");
        const targetDocType = $("<select class='form-control' disabled><option value=''>Choose a DocType</option></select>");
        const targetCatalogStatus = $("<div class='text-muted hero-mapping-catalog-status' aria-live='polite'></div>");
        const retryTargetCatalog = $("<button type='button' class='btn btn-default btn-sm' hidden>Retry app catalog</button>");
        const retrySnapshots = $("<button type='button' class='btn btn-default btn-sm' hidden>Retry saved snapshots</button>");
        const sourceList = $("<div class='hero-mapping-picker-host'></div>"), targetList = $("<div class='hero-mapping-picker-host'></div>");
        const reverseSuggestions = $("<div class='hero-mapping-reverse-suggestions'></div>");
        const sourceDetails = $("<details class='hero-mapping-field-details'><summary>HERO field details</summary></details>");
        const targetDetails = $("<details class='hero-mapping-field-details'><summary>Frappe field details</summary></details>");
        const sourcePager = $("<div class='hero-mapping-pager'></div>"), targetPager = $("<div class='hero-mapping-pager'></div>");
        const suggestions = $("<div class='hero-mapping-table-wrap'></div>"), activeMappings = $("<div class='hero-mapping-table-wrap'></div>");
        const statusSummary = $("<div class='hero-mapping-statuses'></div>"), preview = $("<pre class='hero-mapping-preview' aria-live='polite'></pre>");
        const dynamicKind = $("<input class='form-control' value='MUTATION' readonly>");
        const writebackApp = $("<select class='form-control' disabled><option value=''>Choose an app</option></select>");
        const writebackDocType = $("<select class='form-control' disabled><option value=''>Choose a DocType</option></select>");
        const writebackSearch = $("<input class='form-control' placeholder='Name, fieldname, or type' disabled>");
        const dynamicOperation = $("<select class='form-control' disabled><option value=''>Choose a snapshot mutation</option></select>");
        const dynamicOperationSearch = $("<input class='form-control' placeholder='Filter snapshot mutations' disabled>");
        const writebackTargetSearch = $("<input class='form-control' placeholder='Filter writable HERO input fields' disabled>");
        const dynamicArguments = $("<div class='text-muted hero-mapping-operation-arguments'></div>");
        const dynamicFieldPath = $("<select class='form-control' disabled><option value=''>Choose the schema-proven HERO input field</option></select>"), dynamicTransformation = $("<select class='form-control'><option value='Identity'>Identity</option><option value='Trim'>Trim text</option></select>"), dynamicPreview = $("<pre class='hero-mapping-preview' aria-live='polite'></pre>");
        const dynamicReason = $("<input class='form-control' placeholder='Why this mapping is compatible'>"), dynamicConfidence = $("<input class='form-control' type='number' min='0' max='1' step='0.01' value='0.5'>");
        const dynamicContract = $("<div class='text-muted hero-mapping-operation-arguments' aria-live='polite'></div>"), dynamicStatus = $("<div class='hero-mapping-statuses' aria-live='polite'></div>"), dynamicRows = $("<div class='hero-mapping-table-wrap'></div>"), dynamicSearch = $("<input class='form-control' placeholder='Filter saved writeback mappings'>");
        const writebackSourceList = $("<div class='hero-mapping-picker-host'></div>"), writebackTargetList = $("<div class='hero-mapping-picker-host'></div>");
        const writebackSourceDetails = $("<details class='hero-mapping-field-details'><summary>Frappe source field details</summary></details>"), writebackTargetDetails = $("<details class='hero-mapping-field-details'><summary>HERO target field details</summary></details>");
        const writebackSourcePager = $("<div class='hero-mapping-pager'></div>"), writebackTargetPager = $("<div class='hero-mapping-pager'></div>"), writebackComparison = $("<div class='hero-mapping-operation-arguments' aria-live='polite'></div>");
        const saveDynamicDraft = $("<button type='button' class='btn btn-default'>Save Draft</button>"), activateDynamic = $("<button type='button' class='btn btn-warning'>Activate reviewed mapping</button>");
        const importFile = $("<input type='file' accept='application/json,.json' class='form-control-file'>"), exchangeResult = $("<div class='mt-2'></div>");
        const advanced = $("<details class='hero-mapping-advanced'><summary>Advanced: JSON import and schema provisioning</summary></details>");
        const provisionCustomField = $("<button type='button' class='btn btn-default btn-sm'>Prepare Custom Field provisioning</button>");
        let selectedSource = null, selectedTarget = null, importPayload = null, activeSourceFields = [], pendingSourcePath = routeContext.source_path || "";
        let sourceStart = 0, targetStart = 0, currentSuggestions = [], suggestionsExpanded = false, reverseExpanded = false, suggestionDirection = "", suggestionSequence = 0, sourceLoadSequence = 0, targetCatalogSequence = 0;
        let savedReadMappings = [], readMappingLoadError = "", readMappingsLoading = false, readMappingLoadSequence = 0, writebackCatalog = null, writebackFields = [], selectedWritebackField = null, writebackCatalogSequence = 0, writebackSourceStart = 0, writebackTargetStart = 0, dynamicMappings = [];
        const requestedSnapshotName = routeContext.snapshot_name || routeContext.snapshot || "";
        const snapshotStorageKey = () => {
            const site = String(frappe.boot?.sitename || window.location.host || "site");
            const user = String(frappe.session?.user || frappe.boot?.user?.name || "guest");
            return `hero-integration.mapping.snapshot:${site}:${user}`;
        };
        const storedSnapshotName = () => {
            try { return window.localStorage.getItem(snapshotStorageKey()) || ""; } catch (_) { return ""; }
        };
        const persistSnapshotName = (name) => {
            try {
                if (name) window.localStorage.setItem(snapshotStorageKey(), name);
                else window.localStorage.removeItem(snapshotStorageKey());
            } catch (_) { /* Browser storage is optional; route state remains authoritative. */ }
        };
        const snapshots = new Map();
        const requestedOperation = routeContext.operation || "";

        const controls = $("<div class='hero-mapping-controls'></div>").append($("<label></label>").text("GraphQL Schema Snapshot").append(snapshot, snapshotMetadata, retrySnapshots), $("<label></label>").text("HERO source operation").append(operationSearch, operation));
        const mappingGrid = $("<div class='hero-mapping-grid'></div>").append(
            $("<section class='hero-mapping-pane'></section>").append($("<h3>HERO source</h3><p class='text-muted'>Search the saved, read-only operation and select one source field.</p>"), sourceSearch, sourceList, sourcePager, sourceDetails, $("<h4 class='hero-mapping-subheading'>Suggested HERO source fields</h4>"), reverseSearch, reverseSuggestions),
            $("<section class='hero-mapping-pane'></section>").append($("<h3>Frappe target</h3><p class='text-muted'>Choose an installed app, then a DocType, before searching its fields. This catalog is available even when no HERO schema snapshot has been saved yet.</p>"), targetApp, targetCatalogStatus, retryTargetCatalog, targetDocType, targetSearch, targetList, targetPager, targetDetails, $("<p class='text-muted hero-mapping-provision-note'></p>").append("Missing a Custom Field? ", provisionCustomField)),
        );
        advanced.append($("<p class='text-muted'></p>").text("An upload only fills a reviewed checklist. Select changes and confirm one adoption; it never migrates business data."), importFile, exchangeResult);
        page.main.append(
            $("<header class='hero-mapping-header'><h2>HERO Mapping Editor</h2><p class='text-muted'>Mappings are reviewed proposals. Creating a Draft never activates it.</p></header>"), controls,
            $("<section class='hero-mapping-section'></section>").append($("<h3>Map fields</h3>"), mappingGrid, preview),
            $("<section class='hero-mapping-section'></section>").append($("<h3>Review Suggestions</h3><p class='text-muted'>The strongest compatible proposals are shown first; reviewing one only prepares a Draft.</p>"), suggestions),
            $("<section class='hero-mapping-section'></section>").append($("<h3>Active Read Mappings</h3><p class='text-muted'>All saved active mappings for the selected snapshot are shown independently from the source-field picker.</p>"), statusSummary, $("<div class='hero-mapping-controls'></div>").append($("<label></label>").text("Read operation").append(readOperationFilter), $("<label></label>").text("Field filter").append(activeSearch), resetReadFilters), activeMappings), advanced,
            $("<section class='hero-mapping-section hero-mapping-dynamic'></section>").append(
                $("<h3>Frappe to HERO writeback</h3><p class='text-muted'>Inspect every saved mutation and eligible installed-app source field. Only one reviewed, schema-bound contract can ever be activated; all other compatible proposals remain non-executable Drafts.</p>"),
                $("<div class='hero-mapping-controls'></div>").append($("<label></label>").text("Contract type").append(dynamicKind), $("<label></label>").text("Technical contract").append(dynamicContract), dynamicArguments),
                $("<div class='hero-mapping-grid'></div>").append(
                    $("<section class='hero-mapping-pane'></section>").append($("<h3>Frappe source</h3><p class='text-muted'>Choose an eligible local field from any installed app. The source picker is independent from the HERO-to-Frappe mapper.</p>"), $("<label></label>").text("Frappe app").append(writebackApp), $("<label></label>").text("Frappe source DocType").append(writebackDocType), writebackSearch, writebackSourceList, writebackSourcePager, writebackSourceDetails),
                    $("<section class='hero-mapping-pane'></section>").append($("<h3>HERO mutation target</h3><p class='text-muted'>Choose the validated snapshot mutation and inspect its exact typed input fields. Only reviewed contracts can be activated.</p>"), $("<label></label>").text("Snapshot mutation").append(dynamicOperationSearch, dynamicOperation), writebackTargetSearch, writebackTargetList, writebackTargetPager, writebackTargetDetails),
                ),
                writebackComparison, $("<div class='hero-mapping-controls'></div>").append($("<label></label>").text("Transformation").append(dynamicTransformation), $("<label></label>").text("Suggestion rationale").append(dynamicReason), $("<label></label>").text("Confidence 0–1").append(dynamicConfidence)), dynamicPreview,
                $("<div class='mt-2'></div>").append(saveDynamicDraft, document.createTextNode(" "), activateDynamic), dynamicStatus, $("<h4>Saved writeback mappings</h4>"), dynamicSearch, dynamicRows,
            ),
        );

        const showDetails = (details, field) => {
            details.empty().append($("<summary></summary>").text(`${field.label || field.path} details`));
            const rows = [["Fieldname", field.path], ["Type", field.fieldtype], ["Relation", field.relation || "—"], ["Description", field.description || field.reason || "—"]];
            details.append($("<dl class='hero-mapping-detail-list'></dl>").append(rows.flatMap(([label, value]) => [$("<dt></dt>").text(label), $("<dd></dd>").text(value)]))).prop("open", true);
        };
        const renderPager = (container, result, load) => {
            container.empty(); if (!result.total) return;
            const first = result.start + 1, last = result.start + result.items.length;
            container.append($("<span class='text-muted'></span>").text(`${first}–${last} of ${result.total}`));
            if (result.start) container.append($("<button type='button' class='btn btn-default btn-sm'>Previous</button>").on("click", () => load(Math.max(0, result.start - PAGE_SIZE))));
            if (result.has_more) container.append($("<button type='button' class='btn btn-default btn-sm'>Next</button>").on("click", () => load(result.start + PAGE_SIZE)));
        };
        const renderPicker = (container, result, choose, details, label, groupBy) => {
            container.empty();
            if (!result.items.length) return container.append($("<p class='text-muted'></p>").text(result.total ? "Refine the search to find a field." : "No compatible fields found."));
            const picker = $("<select class='form-control hero-mapping-picker' size='8'></select>").attr("aria-label", label).append($("<option value=''></option>").text(`Choose a ${label}`));
            const groups = new Map();
            result.items.forEach((field, index) => {
                const key = groupBy(field), group = groups.get(key) || $("<optgroup></optgroup>").attr("label", key);
                group.append($("<option></option>").val(String(index)).text(`${field.label} · ${field.path} · ${field.fieldtype}${field.relation ? ` → ${field.relation}` : ""}`).prop("disabled", !field.selectable));
                groups.set(key, group);
            });
            groups.forEach((group) => picker.append(group));
            picker.on("change", () => { const field = result.items[Number(picker.val())]; if (field) { showDetails(details, field); if (field.selectable) choose(field); } });
            container.append(picker);
        };
        const renderReadOperationFilter = () => {
            const selected = readOperationFilter.val(), operations = [...new Set(savedReadMappings.map((mapping) => mapping.operation).filter(Boolean))].sort();
            readOperationFilter.empty().append($("<option value=''></option>").text("All read operations"));
            operations.forEach((value) => readOperationFilter.append($("<option></option>").val(value).text(value)));
            readOperationFilter.val(operations.includes(selected) ? selected : "").prop("disabled", !operations.length);
            resetReadFilters.prop("disabled", !savedReadMappings.length);
        };
        const renderActiveMappings = () => {
            if (readMappingLoadError) {
                statusSummary.empty(); activeMappings.empty().append($("<p class='text-danger'></p>").text(`Read mappings could not be loaded: ${readMappingLoadError}`));
                return;
            }
            if (!hasValidSnapshot()) {
                statusSummary.empty(); activeMappings.empty().append($("<p class='text-muted'></p>").text("Choose a validated snapshot to load saved read mappings."));
                return;
            }
            if (readMappingsLoading) {
                statusSummary.empty(); activeMappings.empty().append($("<p class='text-muted'></p>").text("Loading saved active read mappings…"));
                return;
            }
            const rows = savedReadMappings, operationFilter = readOperationFilter.val(), needle = activeSearch.val().trim().toLowerCase();
            const filtered = rows.filter((mapping) => (!operationFilter || mapping.operation === operationFilter) && (!needle || [mapping.operation, mapping.definition_id, mapping.source_path, mapping.source_type, mapping.target_doctype, mapping.target_field].join(" ").toLowerCase().includes(needle)));
            const counts = rows.reduce((result, mapping) => { result[mapping.operation] = (result[mapping.operation] || 0) + 1; return result; }, {});
            statusSummary.empty().append($("<span class='hero-mapping-status status-active'></span>").text(`Active: ${rows.length}`), $("<span class='hero-mapping-status'></span>").text(`Visible: ${filtered.length}`));
            Object.entries(counts).sort(([left], [right]) => left.localeCompare(right)).forEach(([label, count]) => statusSummary.append($("<span class='hero-mapping-status'></span>").text(`${label}: ${count}`)));
            activeMappings.empty(); if (!rows.length) return activeMappings.append($("<p class='text-muted'></p>").text("No active read mappings are saved for this snapshot."));
            if (!filtered.length) return activeMappings.append($("<p class='text-muted'></p>").text("No saved read mappings match these filters."));
            const table = $("<table class='table table-sm hero-mapping-table'><thead><tr><th>Status</th><th>Operation</th><th>HERO source</th><th>Frappe target</th><th>Revision</th></tr></thead><tbody></tbody></table>");
            const conflicts = new Set(rows.filter((row) => rows.filter((other) => other.operation === row.operation && other.source_path === row.source_path).length > 1).map((row) => `${row.operation}:${row.source_path}`));
            filtered.forEach((mapping) => table.find("tbody").append($("<tr></tr>").toggleClass("is-conflict", conflicts.has(`${mapping.operation}:${mapping.source_path}`)).append($("<td></td>").append($("<span class='hero-mapping-status'></span>").addClass(`status-${statusClass(mapping.status)}`).text(mapping.status)), $("<td></td>").text(mapping.operation), $("<td></td>").text(mapping.source_path), $("<td></td>").text(`${mapping.target_doctype}.${mapping.target_field}`), $("<td></td>").text(mapping.mapping_revision || "—"))));
            activeMappings.append(table);
        };
        const selectedSnapshot = () => snapshots.get(snapshot.val()) || null;
        const hasValidSnapshot = () => selectedSnapshot()?.analysis_status === "Validated";
        let queryOperations = [], dynamicOperations = [];
        const dynamicDirection = () => "FRAPPE_TO_MUTATION";
        const writebackContract = () => (writebackCatalog?.contracts || []).find((item) => item.operation === dynamicOperation.val()) || null;
        const mutationTargetFields = () => {
            const selected = dynamicOperations.find((item) => item.operation_name === dynamicOperation.val());
            if (!selected) return [];
            return (selected.arguments || []).flatMap((argument) => (selected.input_fields?.[argument.name] || []).map((field) => ({
                key: `${argument.name}:${field.path}`, operation: selected.operation_name, argument: argument.name,
                input_type: argument.input_type || argument.type, field: field.path, field_path: field.path,
                graphql_type: field.type, required: field.required, kind: field.kind,
                path: `${argument.name}${field.path ? `.${field.path}` : ""}`,
                label: `${argument.name}${field.path ? `.${field.path}` : ""}`,
                fieldtype: field.type, relation: argument.type, selectable: Boolean(field.selectable),
                reason: field.reason || "Direct scalar or enum input field.",
                description: `Argument: ${argument.name} (${argument.type}). Path: ${field.path || "direct argument"}. ${field.required ? "Required." : "Nullable."}`,
            })));
        };
        const selectedWritebackTarget = () => mutationTargetFields().find((item) => item.key === dynamicFieldPath.val()) || null;
        const visualDynamicMapping = () => {
            const target = selectedWritebackTarget();
            if (!writebackDocType.val() || !selectedWritebackField?.path || !target) return null;
            return {version: 1, source: {doctype: writebackDocType.val(), field: selectedWritebackField.path}, target, transformation: dynamicTransformation.val()};
        };
        const renderDynamicPreview = () => {
            const mapping = visualDynamicMapping();
            writebackComparison.text(mapping ? `${mapping.source.doctype}.${mapping.source.field} → ${mapping.target.operation} / ${mapping.target.argument}.${mapping.target.field || "direct"} · ${mapping.target.graphql_type || "schema type pending"} · ${mapping.transformation}` : "Choose one Frappe source field and its schema-proven HERO mutation input field.");
            dynamicPreview.text(mapping ? JSON.stringify({writes: false, status: "Draft preview", mapping}, null, 2) : "Choose a Frappe app, DocType, source field, snapshot mutation, and schema-proven HERO input field to preview a Draft.");
        };
        const renderDynamicMappings = () => {
            const needle = dynamicSearch.val().trim().toLowerCase();
            const rows = dynamicMappings.filter((row) => !needle || [row.name, row.operation_name, row.direction, row.status, row.suggestion_reason].join(" ").toLowerCase().includes(needle));
            dynamicRows.empty();
            if (!rows.length) return dynamicRows.append($("<p class='text-muted'></p>").text("No dynamic mapping contracts are stored for this snapshot and type."));
            ["Draft", "Active", "Invalid"].forEach((status) => {
                const group = rows.filter((row) => statusClass(row.status) === status.toLowerCase());
                if (!group.length) return;
                const table = $("<table class='table table-sm hero-mapping-table'><thead><tr><th>Operation</th><th>Direction</th><th>Status</th><th>Revision</th></tr></thead><tbody></tbody></table>");
                group.forEach((row) => table.find("tbody").append($("<tr></tr>").append($("<td></td>").text(row.operation_name), $("<td></td>").text(row.direction), $("<td></td>").append($("<span class='hero-mapping-status'></span>").addClass(`status-${statusClass(row.status)}`).text(row.status)), $("<td></td>").text(row.mapping_revision || "—"))));
                dynamicRows.append($("<h5></h5>").text(`${status} writeback mappings (${group.length})`), table);
            });
        };
        const loadDynamicMappings = () => {
            if (!hasValidSnapshot()) { dynamicMappings = []; return renderDynamicMappings(); }
            return frappe.call({method: "hero_integration.api.graphql_mapping_list", args: {snapshot_name: snapshot.val(), operation_kind: "MUTATION"}}).then((response) => { dynamicMappings = response.message || []; renderDynamicMappings(); });
        };
        const renderWritebackContract = () => {
            const contract = writebackContract();
            dynamicContract.empty();
            if (!dynamicOperation.val()) return activateDynamic.prop("disabled", true);
            if (!contract) {
                dynamicContract.text("No schema-bound technical mutation contract exists for this operation; it can only remain a non-executable Draft.");
                return activateDynamic.prop("disabled", true);
            }
            const names = (contract.contracts || []).map((item) => `${item.name} (${item.status}, rev ${item.contract_revision || 1})`).join(", ");
            dynamicContract.text(contract.activation_ready ? `Reusable technical contract: ${names}.` : contract.blocker);
            activateDynamic.prop("disabled", !contract.activation_ready || !visualDynamicMapping());
        };
        const renderWritebackInputTarget = () => {
            dynamicFieldPath.empty().append($("<option value=''></option>").text("Choose the schema-proven HERO input field"));
            mutationTargetFields().forEach((target) => dynamicFieldPath.append($("<option></option>").val(target.key).text(`${target.path} · ${target.graphql_type}${target.required ? " · required" : " · nullable"}`).prop("disabled", !target.selectable).attr("title", target.reason)));
            dynamicFieldPath.prop("disabled", !dynamicOperation.val());
            renderWritebackTargets();
            renderWritebackContract();
            renderDynamicPreview();
        };
        const renderQueryOperations = () => {
            const current = operation.val() || requestedOperation || queryOperations.find((item) => item.operation_name === "HeroCustomers")?.operation_name || queryOperations[0]?.operation_name || "";
            const needle = operationSearch.val().trim().toLowerCase();
            const visible = queryOperations.filter((item) => !needle || [item.operation_name, item.return_type, ...(item.arguments || []).map((argument) => `${argument.name} ${argument.type}`)].join(" ").toLowerCase().includes(needle));
            operation.empty().append($("<option value=''></option>").text("Choose a snapshot query"));
            visible.forEach((item) => operation.append($("<option></option>").val(item.operation_name).text(`${item.operation_name} → ${item.return_type || "unknown"}`)));
            operation.val(visible.some((item) => item.operation_name === current) ? current : "").prop("disabled", !visible.length);
            operationSearch.prop("disabled", !queryOperations.length);
        };
        const loadQueryOperations = () => {
            queryOperations = []; operation.empty().append($("<option value=''></option>").text("Choose a snapshot query")).prop("disabled", true); operationSearch.prop("disabled", true);
            if (!hasValidSnapshot()) return;
            return frappe.call({method: "hero_integration.api.schema_operation_catalog", args: {snapshot_name: snapshot.val(), operation_kind: "QUERY"}}).then((response) => {
                queryOperations = response.message || [];
                renderQueryOperations();
                if (operation.val()) loadSources(0);
            });
        };
        const renderDynamicOperations = () => {
            const current = dynamicOperation.val();
            const needle = dynamicOperationSearch.val().trim().toLowerCase();
            const visible = dynamicOperations.filter((item) => !needle || [item.operation_name, item.return_type, ...(item.arguments || []).map((argument) => `${argument.name} ${argument.type}`)].join(" ").toLowerCase().includes(needle));
            dynamicOperation.empty().append($("<option value=''></option>").text("Choose a snapshot mutation"));
            visible.forEach((item) => dynamicOperation.append($("<option></option>").val(item.operation_name).text(`${item.operation_name} → ${item.return_type || "unknown"}`)));
            dynamicOperation.val(visible.some((item) => item.operation_name === current) ? current : "").prop("disabled", !visible.length);
            dynamicOperationSearch.prop("disabled", !dynamicOperations.length);
            showDynamicOperation();
        };
        const renderWritebackTargets = () => {
            const operation = dynamicOperations.find((item) => item.operation_name === dynamicOperation.val());
            writebackTargetList.empty(); writebackTargetPager.empty();
            if (!operation) {
                writebackTargetSearch.val("").prop("disabled", true);
                writebackTargetDetails.empty().append($("<summary>HERO target field details</summary>"));
                return writebackTargetList.append($("<p class='text-muted'></p>").text("Choose a snapshot mutation to inspect its typed input arguments and fields."));
            }
            writebackTargetSearch.prop("disabled", false);
            const needle = writebackTargetSearch.val().trim().toLowerCase();
            const fields = mutationTargetFields().filter((field) => !needle || [field.label, field.path, field.fieldtype, field.relation, field.description, field.reason].join(" ").toLowerCase().includes(needle));
            const page = fields.slice(writebackTargetStart, writebackTargetStart + PAGE_SIZE);
            renderPicker(writebackTargetList, {items: page, total: fields.length, start: writebackTargetStart, has_more: writebackTargetStart + PAGE_SIZE < fields.length}, (field) => {
                dynamicFieldPath.val(field.key); showDetails(writebackTargetDetails, field); renderWritebackContract(); renderDynamicPreview();
            }, writebackTargetDetails, "HERO mutation input field", (field) => field.relation || "mutation input");
            renderPager(writebackTargetPager, {items: page, total: fields.length, start: writebackTargetStart, has_more: writebackTargetStart + PAGE_SIZE < fields.length}, (start) => { writebackTargetStart = start; renderWritebackTargets(); });
        };
        const selectWritebackField = (field) => {
            selectedWritebackField = field?.selectable ? field : null;
            writebackTargetStart = 0;
            if (selectedWritebackField) showDetails(writebackSourceDetails, selectedWritebackField);
            showDynamicOperation();
        };
        const renderWritebackFields = () => {
            const doctype = writebackDocType.val();
            writebackFields = (writebackCatalog?.fields || []).filter((field) => field.doctype === doctype);
            selectedWritebackField = null;
            writebackSourceStart = 0;
            const needle = writebackSearch.val().trim().toLowerCase();
            const fields = writebackFields.filter((field) => !needle || [field.label, field.path, field.fieldtype, field.reason].join(" ").toLowerCase().includes(needle));
            const page = fields.slice(writebackSourceStart, writebackSourceStart + PAGE_SIZE);
            writebackSearch.prop("disabled", !doctype);
            renderPicker(writebackSourceList, {items: page, total: fields.length, start: writebackSourceStart, has_more: writebackSourceStart + PAGE_SIZE < fields.length}, selectWritebackField, writebackSourceDetails, "Frappe source field", () => doctype || "Frappe source");
            renderPager(writebackSourcePager, {items: page, total: fields.length, start: writebackSourceStart, has_more: writebackSourceStart + PAGE_SIZE < fields.length}, (start) => { writebackSourceStart = start; renderWritebackFields(); });
            renderWritebackInputTarget();
        };
        const renderWritebackDocTypes = () => {
            const current = writebackDocType.val();
            writebackDocType.empty().append($("<option value=''></option>").text("Choose a DocType"));
            (writebackCatalog?.doctypes || []).forEach((item) => writebackDocType.append($("<option></option>").val(item.doctype).text(`${item.app_name || "Custom"} / ${item.doctype}`).prop("disabled", !item.selectable).attr("title", item.reason || "")));
            writebackDocType.val(current).prop("disabled", !writebackApp.val() || !(writebackCatalog?.doctypes || []).length);
            renderWritebackFields();
        };
        const loadWritebackCatalog = () => {
            if (!hasValidSnapshot()) {
                writebackCatalog = null; selectedWritebackField = null;
                writebackApp.prop("disabled", true); writebackDocType.prop("disabled", true); writebackSearch.prop("disabled", true);
                writebackSourceList.empty(); writebackSourcePager.empty();
                return renderWritebackInputTarget();
            }
            const request = ++writebackCatalogSequence, selectedApp = writebackApp.val(), selectedDocType = writebackDocType.val();
            writebackApp.prop("disabled", true);
            return frappe.call({method: "hero_integration.api.writeback_mapper_catalog", args: {snapshot_name: snapshot.val(), app_name: selectedApp, source_doctype: selectedDocType}}).then((response) => {
                if (request !== writebackCatalogSequence) return;
                writebackCatalog = response.message || {apps: [], doctypes: [], fields: [], contracts: []};
                writebackApp.empty().append($("<option value=''></option>").text("Choose an app"));
                (writebackCatalog.apps || []).forEach((app) => writebackApp.append($("<option></option>").val(app).text(app)));
                writebackApp.val(selectedApp).prop("disabled", !(writebackCatalog.apps || []).length);
                renderWritebackDocTypes();
            }).catch((error) => {
                if (request !== writebackCatalogSequence) return;
                writebackCatalog = null; selectedWritebackField = null;
                writebackApp.prop("disabled", false); writebackDocType.prop("disabled", true); writebackSearch.prop("disabled", true);
                writebackSourceList.empty().append($("<p class='text-danger'></p>").text("Writeback source catalog could not be loaded. Retry after resolving the backend error.")); writebackSourcePager.empty();
                dynamicStatus.text(`Writeback source fields could not be loaded: ${error?.message || "request failed"}.`);
                renderWritebackInputTarget();
            });
        };
        const showDynamicOperation = () => {
            const selected = dynamicOperations.find((item) => item.operation_name === dynamicOperation.val());
            dynamicArguments.empty();
            if (!selected) return renderWritebackInputTarget();
            const args = (selected.arguments || []).map((argument) => { const fields = selected.input_fields?.[argument.name] || []; return `${argument.name}: ${argument.type}${argument.required ? " (required)" : ""}${fields.length ? `; ${fields.length} typed input field(s)` : ""}`; }).join(", ");
            dynamicArguments.text(`Return: ${selected.return_type || "unknown"}. Arguments: ${args || "none"}. Nested input paths, lists, enums, nullability, and scalars are read directly from the validated snapshot; collections remain disabled without a resolver.`);
            renderWritebackInputTarget();
        };
        const loadDynamicOperations = () => {
            dynamicOperations = []; dynamicOperation.empty().append($("<option value=''></option>").text("Choose a snapshot operation")).prop("disabled", !hasValidSnapshot()); dynamicArguments.empty();
            if (!hasValidSnapshot()) { dynamicMappings = []; renderDynamicMappings(); return; }
            return frappe.call({method: "hero_integration.api.schema_operation_catalog", args: {snapshot_name: snapshot.val(), operation_kind: "MUTATION"}}).then((response) => {
                dynamicOperations = response.message || [];
                renderDynamicOperations();
                dynamicStatus.text(dynamicOperations.length ? `${dynamicOperations.length} snapshot mutations available. Choose a Frappe source field and typed input field; activation stays contract-gated.` : "The selected validated snapshot declares no mutations.");
                loadDynamicMappings();
            });
        };
        const saveDynamic = (activate) => {
            if (!hasValidSnapshot() || !dynamicOperation.val()) return frappe.msgprint("Choose a validated snapshot operation first.");
            const mapping = visualDynamicMapping();
            if (!mapping || Array.isArray(mapping) || typeof mapping !== "object") return frappe.msgprint("Choose one Frappe field and its reviewed HERO input field.");
            const contract = writebackContract();
            if (activate && (!contract || !contract.activation_ready)) return frappe.msgprint(contract?.blocker || "The reusable technical mutation contract is not ready for activation.");
            const submit = () => frappe.call({method: "hero_integration.api.graphql_mapping_save", args: {snapshot_name: snapshot.val(), operation_kind: "MUTATION", operation_name: dynamicOperation.val(), direction: dynamicDirection(), mapping_json: JSON.stringify(mapping), activate, suggestion_reason: dynamicReason.val().trim(), confidence: Number(dynamicConfidence.val())}}).then((response) => {
                dynamicStatus.text(`${response.message.status} mapping ${response.message.name}, revision ${response.message.revision}.`); loadDynamicMappings(); loadWritebackCatalog();
            });
            if (activate) frappe.confirm("Activate this reviewed contract? Future execution still requires separate snapshot, permission, and confirmation gates.", submit); else submit();
        };
        const showSnapshotMetadata = () => {
            const selected = selectedSnapshot();
            snapshotMetadata.empty();
            if (!selected) return snapshotMetadata.text("Choose a saved, validated snapshot before loading HERO source fields or suggestions.");
            const rows = [
                ["Name", selected.name], ["Fingerprint", selected.schema_fingerprint || "—"],
                ["Validation", selected.analysis_status || "Unavailable"], ["Modified", selected.modified || "—"],
            ];
            snapshotMetadata.append($("<dl class='hero-mapping-detail-list'></dl>").append(rows.flatMap(([label, value]) => [$("<dt></dt>").text(label), $("<dd></dd>").text(value)])));
            if (selected.analysis_detail) snapshotMetadata.append($("<p></p>").text(selected.analysis_detail));
            if (!hasValidSnapshot()) snapshotMetadata.append($("<p class='text-danger'></p>").text("This snapshot is not validated and cannot be used for mappings."));
        };
        const showPreview = () => {
            if (!hasValidSnapshot() || !selectedSource || !selectedTarget) return;
            const sourceType = selectedSource.mapping_type || "string|null", value = sourceType.startsWith("int") ? 1 : sourceType.startsWith("boolean") ? true : "Synthetic preview value", segments = selectedSource.path.split(".").filter(Boolean), record = {};
            let nestedRecord = record; segments.forEach((segment, index) => { if (index === segments.length - 1) nestedRecord[segment] = value; else nestedRecord = nestedRecord[segment] = {}; });
            frappe.call({method: "hero_integration.api.preview_mapping_candidate", args: {snapshot_name: snapshot.val(), operation: operation.val(), source_path: selectedSource.path, target_field: selectedTarget.path, target_doctype: selectedTarget.target_doctype || targetDocType.val(), source_record_json: JSON.stringify(record)}}).then((response) => {
                preview.text(JSON.stringify(response.message, null, 2));
                page.set_secondary_action("Create Draft Mapping", () => frappe.new_doc("HERO Frappe Mapping", {operation: operation.val(), source_path: selectedSource.path, source_type: selectedSource.mapping_type, target_doctype: selectedTarget.target_doctype || targetDocType.val(), target_field: selectedTarget.path, schema_snapshot: snapshot.val(), status: "Draft"}));
            });
        };
        const useReverseSuggestion = (match) => {
            selectedSource = {
                path: match.source_path, label: match.source_path, fieldtype: match.source_graphql_type,
                mapping_type: match.source_type, graphql_path: match.source_graphql_path,
                relation: match.source_relation, return_type: match.source_return_type,
            };
            showDetails(sourceDetails, selectedSource); showPreview();
        };
        const renderReverseSuggestions = () => {
            reverseSuggestions.empty();
            if (!selectedTarget) {
                reverseSearch.prop("disabled", true);
                return reverseSuggestions.append($("<p class='text-muted'></p>").text("Choose a Frappe target field to see compatible HERO source fields."));
            }
            reverseSearch.prop("disabled", false);
            if (suggestionDirection !== "target") return reverseSuggestions.append($("<p class='text-muted'></p>").text("Choose the target field again to refresh HERO suggestions."));
            const needle = reverseSearch.val().trim().toLowerCase(), matches = currentSuggestions.filter((match) => !needle || [match.source_path, match.source_graphql_path, match.source_graphql_type, match.source_type, match.source_relation, match.reason, match.confidence].join(" ").toLowerCase().includes(needle));
            if (!matches.length) return reverseSuggestions.append($("<p class='text-muted'></p>").text("No compatible HERO source field exists for this target."));
            const shown = reverseExpanded ? matches : matches.slice(0, 5), list = $("<div class='hero-mapping-suggestion-list'></div>");
            shown.forEach((match) => {
                const type = match.source_graphql_type || match.source_type, path = match.source_graphql_path || match.source_path;
                const relation = match.source_relation ? ` Relation: ${match.source_relation}.` : "";
                list.append($("<div class='hero-mapping-suggestion'></div>").append($("<strong></strong>").text(`${path} · ${type}`), $("<p class='text-muted mb-1'></p>").text(`Contract path: ${match.source_path}. ${match.reason} Confidence: ${match.confidence}.${relation}`), $("<button type='button' class='btn btn-default btn-sm'>Use HERO field</button>").on("click", () => useReverseSuggestion(match))));
            });
            reverseSuggestions.append(list);
            if (!reverseExpanded && matches.length > shown.length) reverseSuggestions.append($("<button type='button' class='btn btn-default btn-sm'>Show more HERO suggestions</button>").on("click", () => { reverseExpanded = true; renderReverseSuggestions(); }));
        };
        const renderSuggestions = () => {
            suggestions.empty(); if (!currentSuggestions.length) return suggestions.append($("<p class='text-muted'></p>").text(suggestionDirection === "target" ? "No compatible HERO source field exists for this target." : "No compatible target exists for this selection."));
            const shown = suggestionsExpanded ? currentSuggestions : currentSuggestions.slice(0, 5), table = $("<table class='table table-sm hero-mapping-table'><thead><tr><th>HERO source</th><th>Frappe target</th><th>Reason</th><th>Confidence</th></tr></thead><tbody></tbody></table>");
            shown.forEach((match) => {
                const review = $("<button type='button' class='btn btn-sm btn-outline-primary'>Review</button>").on("click", () => { selectedSource = selectedSource?.path === match.source_path ? selectedSource : {path: match.source_path, mapping_type: match.source_type}; selectedTarget = {path: match.target_field, target_doctype: match.target_doctype}; showPreview(); });
                table.find("tbody").append($("<tr></tr>").append($("<td></td>").text(match.source_path), $("<td></td>").text(`${match.target_app || "Custom"}/${match.target_doctype}.${match.target_field}`), $("<td></td>").text(match.reason), $("<td></td>").append(review, document.createTextNode(` ${match.confidence} (${match.score})`))));
            });
            suggestions.append(table); if (!suggestionsExpanded && currentSuggestions.length > shown.length) suggestions.append($("<button type='button' class='btn btn-default btn-sm'>Show more suggestions</button>").on("click", () => { suggestionsExpanded = true; renderSuggestions(); }));
        };
        const resetSuggestions = () => { suggestionSequence += 1; currentSuggestions = []; suggestionDirection = ""; suggestionsExpanded = false; reverseExpanded = false; reverseSearch.val(""); renderSuggestions(); renderReverseSuggestions(); };
        const showSuggestions = (args, direction) => {
            if (!hasValidSnapshot()) return resetSuggestions();
            const request = ++suggestionSequence;
            frappe.call({method: "hero_integration.api.mapping_suggestions", args: Object.assign({snapshot_name: snapshot.val(), operation: operation.val(), target_doctype: targetDocType.val()}, args)}).then((response) => {
                if (request !== suggestionSequence) return;
                currentSuggestions = response.message || []; suggestionDirection = direction; suggestionsExpanded = false; reverseExpanded = false; reverseSearch.val(""); renderSuggestions(); renderReverseSuggestions();
            });
        };
        const selectSource = (field) => { selectedSource = field; showSuggestions({source_path: field.path}, "source"); showPreview(); };
        const selectTarget = (field) => { selectedTarget = field; showSuggestions({target_field: field.path, target_doctype: field.target_doctype}, "target"); showPreview(); renderDynamicPreview(); };
        const loadSources = (start = sourceStart) => {
            if (!hasValidSnapshot()) return;
            const snapshotName = snapshot.val(), request = ++sourceLoadSequence;
            sourceStart = start; const args = {snapshot_name: snapshotName, operation: operation.val()};
            Promise.all([frappe.call({method: "hero_integration.api.mapping_source_picker_page", args: {...args, search: sourceSearch.val(), start, page_size: PAGE_SIZE}}), frappe.call({method: "hero_integration.api.mapping_source_picker", args: {...args, search: ""}})]).then(([response, complete]) => {
                if (request !== sourceLoadSequence || snapshot.val() !== snapshotName) return;
                const result = response.message || {items: [], total: 0}; activeSourceFields = complete.message || [];
                renderPicker(sourceList, result, selectSource, sourceDetails, "HERO field", (field) => field.selectable ? "Selected operation fields" : "Schema-only context"); renderPager(sourcePager, result, loadSources);
                const requested = result.items.find((field) => field.path === pendingSourcePath && field.selectable); if (requested) { pendingSourcePath = ""; selectSource(requested); }
            });
        };
        const clearTargets = (message) => { targetList.empty().append($("<p class='text-muted'></p>").text(message)); targetPager.empty(); targetDetails.empty().append($("<summary>Frappe field details</summary>")); };
        const loadTargets = (start = targetStart) => {
            if (!targetDocType.val()) return clearTargets("Choose an app and DocType before fields are loaded.");
            targetStart = start;
            frappe.call({method: "hero_integration.api.mapping_target_picker_page", args: {operation: operation.val(), search: targetSearch.val(), app_name: targetApp.val(), target_doctype: targetDocType.val(), start, page_size: PAGE_SIZE}}).then((response) => {
                const result = response.message || {items: [], total: 0}; renderPicker(targetList, result, selectTarget, targetDetails, "Frappe field", () => targetDocType.val()); renderPager(targetPager, result, loadTargets);
            });
        };
        const loadTargetCatalog = () => {
            const request = ++targetCatalogSequence, selectedApp = targetApp.val(), selectedDocType = targetDocType.val();
            targetCatalogStatus.text("Loading installed Frappe apps …"); retryTargetCatalog.prop("hidden", true); targetApp.prop("disabled", true);
            return frappe.call({method: "hero_integration.api.mapping_target_catalog", args: {app_name: selectedApp}}).then((response) => {
                if (request !== targetCatalogSequence) return;
                const catalog = response.message || {apps: [], doctypes: []};
                targetApp.empty().append($("<option value=''></option>").text("Choose an app")); (catalog.apps || []).forEach((app) => targetApp.append($("<option></option>").val(app).text(app))); targetApp.val(selectedApp).prop("disabled", false);
                targetDocType.empty().append($("<option value=''></option>").text("Choose a DocType")).prop("disabled", !selectedApp); targetSearch.prop("disabled", !selectedDocType);
                (catalog.doctypes || []).forEach((item) => targetDocType.append($("<option></option>").val(item.doctype).text(`${item.app_name || "Custom"} / ${item.doctype}`).prop("disabled", !item.selectable).attr("title", item.reason))); targetDocType.val(selectedDocType);
                targetCatalogStatus.text(catalog.apps?.length ? `${catalog.apps.length} installed apps available.` : "No installed Frappe apps were returned.");
            }).catch((error) => {
                if (request !== targetCatalogSequence) return;
                targetApp.prop("disabled", false); targetCatalogStatus.text(`Installed Frappe apps could not be loaded: ${error?.message || "request failed"}.`); retryTargetCatalog.prop("hidden", false);
            });
        };
        const renderChecklist = (checklist) => {
            exchangeResult.empty(); const selected = new Set(checklist.items.filter((item) => item.selectable).map((item) => item.id)), summary = $("<p class='text-muted'></p>"), update = () => summary.text(`${selected.size} selected. ${checklist.counts.Conflict || 0} conflicts and ${checklist.counts.Invalid || 0} invalid entries remain excluded.`);
            const controls = $("<div class='mb-2'></div>"), all = $("<button class='btn btn-sm btn-default mr-2'>Select all valid</button>"), none = $("<button class='btn btn-sm btn-default mr-2'>Clear selection</button>"); all.on("click", () => { checklist.items.filter((item) => item.selectable).forEach((item) => selected.add(item.id)); exchangeResult.find("input[data-item-id]").prop("checked", true); update(); }); none.on("click", () => { selected.clear(); exchangeResult.find("input[data-item-id]").prop("checked", false); update(); }); exchangeResult.append(controls.append(all, none, summary));
            const groups = {}; checklist.items.forEach((item) => (groups[item.group] ||= []).push(item)); Object.entries(groups).forEach(([group, items]) => { const section = $("<div class='mb-3'></div>").append($("<h6></h6>").text(group)); items.forEach((item) => { const checkbox = $("<input type='checkbox' class='mr-2' data-item-id=''> ").attr("data-item-id", item.id).prop("checked", selected.has(item.id)).prop("disabled", !item.selectable); checkbox.on("change", () => { if (checkbox.prop("checked")) { item.dependencies.filter((dependency) => !selected.has(dependency)).forEach((dependency) => { selected.add(dependency); exchangeResult.find(`input[data-item-id='${dependency}']`).prop("checked", true); }); selected.add(item.id); } else if (checklist.items.some((candidate) => candidate.dependencies.includes(item.id) && selected.has(candidate.id))) { checkbox.prop("checked", true); return frappe.msgprint("Deselect dependent entries before removing this requirement."); } else selected.delete(item.id); update(); }); section.append($("<label class='d-block'></label>").append(checkbox, document.createTextNode(`${item.state}: ${item.label}. ${item.detail}`))); }); exchangeResult.append(section); });
            exchangeResult.append($("<button class='btn btn-primary'>Apply selected adoption</button>").on("click", () => { if (!selected.size) return frappe.msgprint("Select at least one valid change."); frappe.confirm("Provision the selected schema and activate selected mappings? This cannot migrate HERO data.", () => frappe.call({method: "hero_integration.api.adopt_mapping_package", args: {snapshot_name: snapshot.val(), payload_json: importPayload, selected_ids_json: JSON.stringify([...selected]), confirmed: true}}).then((result) => exchangeResult.append($("<pre class='mt-2'></pre>").text(JSON.stringify(result.message, null, 2))))); })); update();
        };
        sourceSearch.on("input", () => loadSources(0)); reverseSearch.on("input", renderReverseSuggestions); targetSearch.on("input", () => loadTargets(0)); activeSearch.on("input", renderActiveMappings); readOperationFilter.on("change", renderActiveMappings);
        resetReadFilters.on("click", () => { readOperationFilter.val(""); activeSearch.val(""); renderActiveMappings(); });
        operationSearch.on("input", renderQueryOperations); writebackSearch.on("input", renderWritebackFields); writebackTargetSearch.on("input", () => { writebackTargetStart = 0; renderWritebackTargets(); }); dynamicOperationSearch.on("input", () => { writebackTargetStart = 0; renderDynamicOperations(); }); dynamicSearch.on("input", renderDynamicMappings);
        const invalidateSnapshotState = () => {
            sourceLoadSequence += 1; selectedSource = null; selectedTarget = null; importPayload = null; activeSourceFields = []; sourceStart = 0; pendingSourcePath = "";
            readMappingLoadSequence += 1; savedReadMappings = []; readMappingLoadError = ""; readMappingsLoading = false; readOperationFilter.empty().append($("<option value=''></option>").text("All read operations")).prop("disabled", true); resetReadFilters.prop("disabled", true); activeSearch.val(""); renderActiveMappings();
            resetSuggestions(); preview.empty(); sourceSearch.prop("disabled", !hasValidSnapshot()); importFile.prop("disabled", !hasValidSnapshot()); sourceList.empty().append($("<p class='text-muted'></p>").text(hasValidSnapshot() ? "Loading HERO fields…" : "Choose a validated snapshot before loading HERO fields.")); sourcePager.empty();
            sourceDetails.empty().append($("<summary>HERO field details</summary>")); targetDetails.empty().append($("<summary>Frappe field details</summary>")); exchangeResult.empty(); importFile.val("");
            writebackCatalog = null; writebackFields = []; selectedWritebackField = null; writebackCatalogSequence += 1; writebackSourceStart = 0; writebackTargetStart = 0; dynamicMappings = [];
            writebackApp.val(""); writebackDocType.val(""); writebackSearch.val(""); writebackTargetSearch.val(""); dynamicFieldPath.val(""); dynamicReason.val(""); writebackSourceList.empty(); writebackTargetList.empty(); writebackSourcePager.empty(); writebackTargetPager.empty(); renderDynamicMappings(); renderDynamicPreview(); loadDynamicOperations(); loadWritebackCatalog();
            loadQueryOperations();
        };
        const loadActiveReadMappings = () => {
            if (!hasValidSnapshot()) return;
            const snapshotName = snapshot.val(), schemaFingerprint = selectedSnapshot()?.schema_fingerprint || "", request = ++readMappingLoadSequence;
            readMappingLoadError = ""; readMappingsLoading = true; savedReadMappings = []; renderReadOperationFilter(); renderActiveMappings();
            return frappe.call({method: "frappe.client.get_list", args: {
                doctype: "HERO Frappe Mapping",
                filters: {schema_snapshot: snapshotName, schema_fingerprint: schemaFingerprint, status: "Active"},
                fields: ["name", "definition_id", "operation", "source_path", "source_type", "target_doctype", "target_field", "status", "priority", "mapping_revision"],
                order_by: "operation asc, priority asc, definition_id asc", limit_page_length: 500,
            }}).then((response) => {
                if (request !== readMappingLoadSequence || snapshot.val() !== snapshotName) return;
                savedReadMappings = response.message || []; readMappingLoadError = ""; readMappingsLoading = false; renderReadOperationFilter(); renderActiveMappings();
            }).catch((error) => {
                if (request !== readMappingLoadSequence || snapshot.val() !== snapshotName) return;
                savedReadMappings = []; readMappingLoadError = error?.message || error?.httpStatusText || "request failed"; readMappingsLoading = false; renderReadOperationFilter(); renderActiveMappings();
            });
        };
        const loadSnapshotList = () => {
            retrySnapshots.prop("hidden", true);
            return frappe.call({method: "hero_integration.api.schema_snapshot_list"}).then((response) => {
            const rows = response.message || [];
            snapshots.clear(); snapshot.empty().append($("<option value=''></option>").text("Choose a saved GraphQL Schema Snapshot"));
            rows.forEach((row) => { snapshots.set(row.name, row); snapshot.append($("<option></option>").val(row.name).text(`${row.name} · ${row.analysis_status || "Unavailable"}`)); });
            const storedName = storedSnapshotName();
            const preferredName = requestedSnapshotName || storedName;
            const preferredSnapshot = snapshots.get(preferredName);
            if (preferredSnapshot?.analysis_status === "Validated") {
                snapshot.val(preferredName);
                persistSnapshotName(preferredName);
            } else if (!requestedSnapshotName && storedName) {
                // Do not retain a deleted, invalid, or other-site snapshot selection.
                persistSnapshotName("");
            }
            snapshot.prop("disabled", !rows.length); showSnapshotMetadata(); invalidateSnapshotState();
            if (hasValidSnapshot()) { loadSources(0); loadActiveReadMappings(); } else if (!rows.length) snapshotMetadata.text("No saved schema snapshot yet. Create an explicit, validated snapshot before selecting HERO operations; Frappe targets remain available.");
            }).catch((error) => { snapshot.prop("disabled", true); snapshotMetadata.text(`Saved snapshots could not be loaded: ${error?.message || "request failed"}. HERO source fields remain unavailable.`); retrySnapshots.prop("hidden", false); });
        };
        targetApp.on("change", () => { selectedTarget = null; targetStart = 0; targetDocType.val(""); targetSearch.val("").prop("disabled", true); resetSuggestions(); clearTargets("Choose a DocType before fields are loaded."); loadTargetCatalog(); });
        targetDocType.on("change", () => { selectedTarget = null; targetStart = 0; resetSuggestions(); targetSearch.val("").prop("disabled", !targetDocType.val()); loadTargets(0); });
        operation.on("change", () => { selectedSource = null; selectedTarget = null; sourceStart = 0; targetStart = 0; resetSuggestions(); preview.empty(); if (operation.val()) { loadSources(0); loadTargets(0); } });
        writebackApp.on("change", () => { selectedWritebackField = null; writebackDocType.val(""); writebackSearch.val(""); loadWritebackCatalog(); });
        writebackDocType.on("change", renderWritebackFields);
        dynamicOperation.on("change", () => { writebackTargetStart = 0; showDynamicOperation(); });
        dynamicFieldPath.on("change", () => { renderWritebackContract(); renderDynamicPreview(); }); dynamicTransformation.on("change", () => { renderWritebackContract(); renderDynamicPreview(); });
        saveDynamicDraft.on("click", () => saveDynamic(false)); activateDynamic.on("click", () => saveDynamic(true));
        snapshot.on("change", () => {
            if (hasValidSnapshot()) persistSnapshotName(snapshot.val());
            else persistSnapshotName("");
            showSnapshotMetadata(); invalidateSnapshotState(); if (hasValidSnapshot()) { loadSources(0); loadActiveReadMappings(); }
        });
        retryTargetCatalog.on("click", loadTargetCatalog); retrySnapshots.on("click", loadSnapshotList);
        provisionCustomField.on("click", () => advanced.prop("open", true).find("summary").trigger("focus"));
        importFile.on("change", (event) => { const file = event.target.files[0]; if (!file || !hasValidSnapshot()) return; const reader = new FileReader(); reader.onload = () => { importPayload = reader.result; frappe.call({method: "hero_integration.api.preview_mapping_package", args: {snapshot_name: snapshot.val(), payload_json: importPayload}}).then((response) => renderChecklist(response.message)); }; reader.readAsText(file); });
        page.set_primary_action("Open Graph Viewer", () => { if (!hasValidSnapshot()) return frappe.msgprint("Choose a validated GraphQL Schema Snapshot first."); window.hero_integration_ui.setRoute("api", {hero_tool: "graph", snapshot: snapshot.val()}); });
        page.add_menu_item("Export Mapping JSON", () => { if (!hasValidSnapshot()) return frappe.msgprint("Choose a validated GraphQL Schema Snapshot first."); frappe.call({method: "hero_integration.api.export_mapping_json", args: {snapshot_name: snapshot.val()}}).then((response) => { const exported = response.message, url = URL.createObjectURL(new Blob([exported.content], {type: "application/json"})), link = document.createElement("a"); link.href = url; link.download = exported.filename; link.click(); URL.revokeObjectURL(url); }); });
        page.add_menu_item("GraphQL Services", () => window.hero_integration_ui.setRoute("api", {hero_tool: "services", snapshot: snapshot.val()})); page.add_menu_item("Review Draft Mappings", () => frappe.set_route("List", "HERO Frappe Mapping"));
        clearTargets("Choose an app and DocType before fields are loaded."); renderReverseSuggestions(); showSnapshotMetadata(); renderDynamicPreview(); loadSnapshotList(); loadTargetCatalog();
    }

    window.hero_integration_mapping_editor = Object.freeze({mount});
})();
