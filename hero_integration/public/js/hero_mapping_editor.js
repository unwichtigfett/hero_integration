(() => {
    "use strict";

    if (window.hero_integration_mapping_editor) return;

    const statusClass = (status) => {
        const normalized = String(status || "INVALID").toLowerCase();
        return ["active", "draft"].includes(normalized) ? normalized : "invalid";
    };

    function mount(page, routeContext = {}) {
        page.main.empty().addClass("hero-integration-ui");
        const snapshot = $("<input class='form-control' placeholder='HERO GraphQL Schema Snapshot'>"), operation = $("<select class='form-control'><option value='HeroCustomers'>HeroCustomers</option><option value='HeroProjectMatches'>HeroProjectMatches</option></select>");
        const sourceSearch = $("<input class='form-control' placeholder='Name, type, relation, or HERO field'>"), targetSearch = $("<input class='form-control' placeholder='Name, type, relation, or Frappe field'>"), activeSearch = $("<input class='form-control' placeholder='Filter existing mappings'>");
        const targetApp = $("<select class='form-control'><option value=''>All installed apps</option></select>"), targetDocType = $("<select class='form-control'><option value=''>Choose a DocType</option></select>");
        const sourceList = $("<div class='hero-mapping-field-list'></div>"), targetList = $("<div class='hero-mapping-field-list'></div>"), suggestions = $("<div class='hero-mapping-table-wrap'></div>"), activeMappings = $("<div class='hero-mapping-table-wrap'></div>"), statusSummary = $("<div class='hero-mapping-statuses'></div>"), preview = $("<pre class='hero-mapping-preview' aria-live='polite'></pre>");
        const importFile = $("<input type='file' accept='application/json,.json' class='form-control-file'>"), exchangeResult = $("<div class='mt-2'></div>");
        let selectedSource = null, selectedTarget = null, importPayload = null, sourceFields = [], activeSourceFields = [], pendingSourcePath = routeContext.source_path || "";
        if (routeContext.snapshot_name) snapshot.val(routeContext.snapshot_name);
        if (["HeroCustomers", "HeroProjectMatches"].includes(routeContext.operation)) operation.val(routeContext.operation);

        const controls = $("<div class='hero-mapping-controls'></div>").append($("<label></label>").text("GraphQL Schema Snapshot").append(snapshot), $("<label></label>").text("Read-only HERO operation").append(operation));
        const mappingGrid = $("<div class='hero-mapping-grid'></div>").append(
            $("<section class='hero-mapping-pane'></section>").append($("<h3>HERO source</h3><p class='text-muted'>Choose a field from the saved read-only operation.</p>"), sourceSearch, sourceList),
            $("<section class='hero-mapping-pane'></section>").append($("<h3>Frappe target</h3><p class='text-muted'>Installed apps stay dynamically discoverable. Foreign targets remain Draft proposals.</p>"), targetApp, targetDocType, targetSearch, targetList),
        );
        const advanced = $("<details class='hero-mapping-advanced'><summary>Advanced: JSON import and schema provisioning</summary></details>").append($("<p class='text-muted'></p>").text("An upload only fills a reviewed checklist. Select changes and confirm one adoption; it never migrates business data."), importFile, exchangeResult);
        page.main.append(
            $("<h2>HERO Mapping Editor</h2><p class='text-muted'>Mappings are reviewed proposals. Creating a Draft never activates it.</p>"), controls,
            $("<section class='hero-mapping-section'></section>").append($("<h3>Zuordnen</h3>"), mappingGrid, preview),
            $("<section class='hero-mapping-section'></section>").append($("<h3>Vorschläge prüfen</h3><p class='text-muted'>Reason and confidence are advisory; selecting a suggestion only prepares a Draft.</p>"), suggestions),
            $("<section class='hero-mapping-section'></section>").append($("<h3>Aktive Mappings</h3>"), statusSummary, activeSearch, activeMappings), advanced,
        );

        const renderFields = (container, fields, choose, side) => {
            container.empty();
            if (!fields.length) return container.append($("<p class='text-muted'></p>").text("No compatible fields found."));
            for (const field of fields) {
                const mappings = field.existing_mappings || [], relation = field.relation ? ` → ${field.relation}` : "";
                const row = $("<button type='button' class='btn btn-default hero-mapping-field'></button>").text(`${field.label} · ${field.graphql_path || field.path} · ${field.fieldtype}${relation}`).attr("title", field.reason || "");
                row.toggleClass("is-conflict", mappings.length > 1).append($("<span class='hero-mapping-field-meta'></span>").text(mappings.length ? ` ${mappings.length} existing` : side));
                if (!field.selectable) row.prop("disabled", true).addClass("text-muted"); else row.on("click", () => choose(field));
                container.append(row);
            }
        };
        const allMappings = () => activeSourceFields.flatMap((field) => (field.existing_mappings || []).map((mapping) => ({...mapping, source_label: field.label})));
        const renderStatusSummary = (mappings) => {
            const counts = {Draft: 0, Active: 0, Invalid: 0};
            mappings.forEach((mapping) => { counts[statusClass(mapping.status) === "invalid" ? "Invalid" : mapping.status] += 1; });
            statusSummary.empty(); Object.entries(counts).forEach(([label, count]) => statusSummary.append($("<span class='hero-mapping-status'></span>").addClass(`status-${label.toLowerCase()}`).text(`${label}: ${count}`)));
        };
        const renderActiveMappings = () => {
            const rows = allMappings(), needle = activeSearch.val().trim().toLowerCase();
            const filtered = rows.filter((mapping) => !needle || [mapping.operation, mapping.source_path, mapping.source_label, mapping.target_doctype, mapping.target_field, mapping.status].join(" ").toLowerCase().includes(needle));
            renderStatusSummary(rows); activeMappings.empty();
            if (!filtered.length) return activeMappings.append($("<p class='text-muted'></p>").text("No mappings match this filter."));
            const table = $("<table class='table table-sm hero-mapping-table'><thead><tr><th>Status</th><th>HERO source</th><th>Frappe target</th><th>Revision</th></tr></thead><tbody></tbody></table>");
            const conflicts = new Set(rows.filter((row) => rows.filter((other) => other.source_path === row.source_path && other.status === "Active").length > 1).map((row) => row.source_path));
            for (const mapping of filtered) {
                const row = $("<tr></tr>").toggleClass("is-conflict", conflicts.has(mapping.source_path));
                row.append($("<td></td>").append($("<span class='hero-mapping-status'></span>").addClass(`status-${statusClass(mapping.status)}`).text(mapping.status)), $("<td></td>").text(`${mapping.source_label || mapping.source_path} · ${mapping.source_path}`), $("<td></td>").text(`${mapping.target_doctype}.${mapping.target_field}`), $("<td></td>").text(mapping.mapping_revision || "—"));
                if (conflicts.has(mapping.source_path)) row.attr("title", "Conflict: more than one active target uses this HERO source."); table.find("tbody").append(row);
            }
            activeMappings.append(table);
        };
        const showPreview = () => {
            if (!snapshot.val() || !selectedSource || !selectedTarget) return;
            const sourceType = selectedSource.mapping_type || "string|null", record = {[selectedSource.path]: sourceType.startsWith("int") ? 1 : sourceType.startsWith("boolean") ? true : "Synthetic preview value"};
            frappe.call({method: "hero_integration.api.preview_mapping_candidate", args: {snapshot_name: snapshot.val(), operation: operation.val(), source_path: selectedSource.path, target_field: selectedTarget.path, target_doctype: selectedTarget.target_doctype || targetDocType.val(), source_record_json: JSON.stringify(record)}}).then((response) => {
                preview.text(JSON.stringify(response.message, null, 2));
                page.set_secondary_action(__("Create Draft Mapping"), () => frappe.new_doc("HERO Frappe Mapping", {operation: operation.val(), source_path: selectedSource.path, source_type: selectedSource.mapping_type, target_doctype: selectedTarget.target_doctype || targetDocType.val(), target_field: selectedTarget.path, schema_snapshot: snapshot.val(), status: "Draft"}));
            });
        };
        const renderSuggestions = (matches) => {
            suggestions.empty(); if (!matches.length) return suggestions.append($("<p class='text-muted'></p>").text("No compatible target exists for this selection."));
            const table = $("<table class='table table-sm hero-mapping-table'><thead><tr><th>HERO source</th><th>Frappe target</th><th>Reason</th><th>Confidence</th></tr></thead><tbody></tbody></table>");
            for (const match of matches) {
                const select = $("<button type='button' class='btn btn-sm btn-outline-primary'>Review</button>").on("click", () => { selectedSource = selectedSource?.path === match.source_path ? selectedSource : {path: match.source_path, mapping_type: match.source_type}; selectedTarget = {path: match.target_field, target_doctype: match.target_doctype}; showPreview(); });
                table.find("tbody").append($("<tr></tr>").append($("<td></td>").text(match.source_path), $("<td></td>").text(`${match.target_app || "Custom"}/${match.target_doctype}.${match.target_field}`), $("<td></td>").text(match.reason), $("<td></td>").append(select, document.createTextNode(` ${match.confidence} (${match.score})`))));
            }
            suggestions.append(table);
        };
        const showSuggestions = (args) => { if (snapshot.val()) frappe.call({method: "hero_integration.api.mapping_suggestions", args: Object.assign({snapshot_name: snapshot.val(), operation: operation.val(), target_doctype: targetDocType.val()}, args)}).then((response) => renderSuggestions(response.message || [])); };
        const selectSource = (field) => { selectedSource = field; showSuggestions({source_path: field.path}); showPreview(); }, selectTarget = (field) => { selectedTarget = field; showSuggestions({target_field: field.path, target_doctype: field.target_doctype}); showPreview(); };
        const loadSources = () => { if (!snapshot.val()) return; const args = {snapshot_name: snapshot.val(), operation: operation.val()}; Promise.all([frappe.call({method: "hero_integration.api.mapping_source_picker", args: {...args, search: sourceSearch.val()}}), frappe.call({method: "hero_integration.api.mapping_source_picker", args: {...args, search: ""}})]).then(([response, complete]) => { sourceFields = response.message || []; activeSourceFields = complete.message || []; renderFields(sourceList, sourceFields, selectSource, "HERO"); renderActiveMappings(); const requested = sourceFields.find((field) => field.path === pendingSourcePath && field.selectable); if (requested) { pendingSourcePath = ""; selectSource(requested); } }); };
        const loadTargets = () => frappe.call({method: "hero_integration.api.mapping_target_picker", args: {operation: operation.val(), search: targetSearch.val(), app_name: targetApp.val(), target_doctype: targetDocType.val()}}).then((response) => renderFields(targetList, response.message || [], selectTarget, "Frappe"));
        const loadTargetCatalog = () => frappe.call({method: "hero_integration.api.mapping_target_catalog", args: {app_name: targetApp.val()}}).then((response) => { const catalog = response.message || {apps: [], doctypes: []}, selectedApp = targetApp.val(), selectedDocType = targetDocType.val(); targetApp.empty().append($("<option value=''>All installed apps</option>")); (catalog.apps || []).forEach((app) => targetApp.append($("<option></option>").val(app).text(app))); targetApp.val(selectedApp); targetDocType.empty().append($("<option value=''>Choose a DocType</option>")); (catalog.doctypes || []).forEach((item) => targetDocType.append($("<option></option>").val(item.doctype).text(`${item.app_name || "Custom"} / ${item.doctype}`).prop("disabled", !item.selectable).attr("title", item.reason))); targetDocType.val(selectedDocType); });
        const renderChecklist = (checklist) => {
            exchangeResult.empty(); const selected = new Set(checklist.items.filter((item) => item.selectable).map((item) => item.id)), summary = $("<p class='text-muted'></p>"), update = () => summary.text(`${selected.size} selected. ${checklist.counts.Conflict || 0} conflicts and ${checklist.counts.Invalid || 0} invalid entries remain excluded.`);
            const controls = $("<div class='mb-2'></div>"), all = $("<button class='btn btn-sm btn-default mr-2'>Select all valid</button>"), none = $("<button class='btn btn-sm btn-default mr-2'>Clear selection</button>"); all.on("click", () => { checklist.items.filter((item) => item.selectable).forEach((item) => selected.add(item.id)); exchangeResult.find("input[data-item-id]").prop("checked", true); update(); }); none.on("click", () => { selected.clear(); exchangeResult.find("input[data-item-id]").prop("checked", false); update(); }); exchangeResult.append(controls.append(all, none, summary));
            const groups = {}; checklist.items.forEach((item) => (groups[item.group] ||= []).push(item)); Object.entries(groups).forEach(([group, items]) => { const section = $("<div class='mb-3'></div>").append($("<h6></h6>").text(group)); items.forEach((item) => { const checkbox = $("<input type='checkbox' class='mr-2' data-item-id=''>").attr("data-item-id", item.id).prop("checked", selected.has(item.id)).prop("disabled", !item.selectable); checkbox.on("change", () => { if (checkbox.prop("checked")) { item.dependencies.filter((dependency) => !selected.has(dependency)).forEach((dependency) => { selected.add(dependency); exchangeResult.find(`input[data-item-id='${dependency}']`).prop("checked", true); }); selected.add(item.id); } else if (checklist.items.some((candidate) => candidate.dependencies.includes(item.id) && selected.has(candidate.id))) { checkbox.prop("checked", true); return frappe.msgprint(__("Deselect dependent entries before removing this requirement.")); } else selected.delete(item.id); update(); }); section.append($("<label class='d-block'></label>").append(checkbox, document.createTextNode(`${item.state}: ${item.label}. ${item.detail}`))); }); exchangeResult.append(section); });
            exchangeResult.append($("<button class='btn btn-primary'>Apply selected adoption</button>").on("click", () => { if (!selected.size) return frappe.msgprint(__("Select at least one valid change.")); frappe.confirm(__("Provision the selected schema and activate selected mappings? This cannot migrate HERO data."), () => frappe.call({method: "hero_integration.api.adopt_mapping_package", args: {snapshot_name: snapshot.val(), payload_json: importPayload, selected_ids_json: JSON.stringify([...selected]), confirmed: true}}).then((result) => exchangeResult.append($("<pre class='mt-2'></pre>").text(JSON.stringify(result.message, null, 2))))); })); update();
        };
        sourceSearch.on("input", loadSources); targetSearch.on("input", loadTargets); activeSearch.on("input", renderActiveMappings);
        targetApp.on("change", () => { selectedTarget = null; targetDocType.val(""); loadTargetCatalog().then(loadTargets); }); targetDocType.on("change", () => { selectedTarget = null; loadTargets(); }); operation.on("change", () => { selectedSource = null; selectedTarget = null; suggestions.empty(); preview.empty(); loadSources(); loadTargets(); });
        importFile.on("change", (event) => { const file = event.target.files[0]; if (!file || !snapshot.val()) return; const reader = new FileReader(); reader.onload = () => { importPayload = reader.result; frappe.call({method: "hero_integration.api.preview_mapping_package", args: {snapshot_name: snapshot.val(), payload_json: importPayload}}).then((response) => renderChecklist(response.message)); }; reader.readAsText(file); });
        page.set_primary_action(__("Open Graph Viewer"), () => { if (!snapshot.val()) return frappe.msgprint(__("Choose a GraphQL Schema Snapshot first.")); window.hero_integration_ui.setRoute("hero-graph-viewer", {snapshot_name: snapshot.val(), operation: operation.val()}); });
        page.add_menu_item(__("Export Mapping JSON"), () => { if (!snapshot.val()) return frappe.msgprint(__("Choose a GraphQL Schema Snapshot first.")); frappe.call({method: "hero_integration.api.export_mapping_json", args: {snapshot_name: snapshot.val()}}).then((response) => { const exported = response.message, url = URL.createObjectURL(new Blob([exported.content], {type: "application/json"})), link = document.createElement("a"); link.href = url; link.download = exported.filename; link.click(); URL.revokeObjectURL(url); }); });
        page.add_menu_item(__("GraphQL Services"), () => window.hero_integration_ui.setRoute("hero-graphql-services")); page.add_menu_item(__("Review Draft Mappings"), () => frappe.set_route("List", "HERO Frappe Mapping"));
        if (snapshot.val()) loadSources(); loadTargetCatalog().then(loadTargets);
    }

    window.hero_integration_mapping_editor = Object.freeze({mount});
})();
