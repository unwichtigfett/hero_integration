(() => {
    "use strict";

    if (window.hero_integration_mapping_editor) return;

    function mount(page, routeContext = {}) {
    page.main.empty().addClass("hero-integration-ui");
    const snapshot = $("<input class='form-control' placeholder='HERO GraphQL Schema Snapshot'>");
    const operation = $("<select class='form-control'><option value='HeroCustomers'>HeroCustomers</option><option value='HeroProjectMatches'>HeroProjectMatches</option></select>");
    const sourceSearch = $("<input class='form-control' placeholder='Search readable HERO fields'>");
    const targetSearch = $("<input class='form-control' placeholder='Search writable ERPNext fields'>");
    const targetApp = $("<select class='form-control'><option value=''>All installed apps</option></select>");
    const targetDocType = $("<select class='form-control'><option value=''>Choose a DocType</option></select>");
    const sourceList = $("<div class='mt-2'></div>");
    const targetList = $("<div class='mt-2'></div>");
    const suggestions = $("<div class='mt-3'></div>");
    const preview = $("<pre class='mt-3'></pre>");
    const importFile = $("<input type='file' accept='application/json,.json' class='form-control-file'>");
    const exchangeResult = $("<div class='mt-2'></div>");
    let selectedSource = null;
    let selectedTarget = null;
    let importPayload = null;
    let pendingSourcePath = routeContext.source_path || "";
    if (routeContext.snapshot_name) snapshot.val(routeContext.snapshot_name);
    if (routeContext.operation === "HeroCustomers" || routeContext.operation === "HeroProjectMatches") operation.val(routeContext.operation);
    page.main.append(
        $("<div class='form-group'><label>GraphQL Schema Snapshot</label></div>").append(snapshot),
        $("<div class='form-group'><label>Read-only HERO operation</label></div>").append(operation),
        $("<hr><h5>HERO source field</h5><p class='text-muted'>Only fields selected by the saved read-only operation can be used.</p>").append(sourceSearch, sourceList),
        $("<hr><h5>Frappe target field</h5><p class='text-muted'>Installed apps are discovered from this site. Foreign-app fields remain Draft proposals and cannot bypass an app-owned integration.</p>").append(targetApp, targetDocType, targetSearch, targetList),
        $("<hr><h5>Non-binding suggestions</h5>").append(suggestions, preview),
        $("<hr><h5>Schema and mapping adoption</h5><p class='text-muted'>An upload only fills this checklist. Select changes, then confirm one shared adoption action. It never migrates business data.</p>").append(importFile, exchangeResult),
    );

    const renderFields = (container, fields, choose) => {
        container.empty();
        if (!fields.length) {
            container.append($("<p class='text-muted'>No compatible fields found.</p>"));
            return;
        }
        fields.forEach((field) => {
            const mappingText = (field.existing_mappings || []).map((mapping) => ` • ${mapping.status}: ${mapping.target_doctype}.${mapping.target_field}`).join("");
            const target = field.target_doctype ? `${field.app_name || "Custom"} / ${field.target_doctype}.` : "";
            const text = `${target}${field.label} — ${field.graphql_path || field.path} (${field.fieldtype})${field.nullable ? ", nullable" : ""}${field.relation ? ` → ${field.relation}` : ""}${mappingText}`;
            const button = $("<button class='btn btn-sm btn-default mr-2 mb-2'></button>").text(text).attr("title", field.reason);
            if (!field.selectable) {
                button.prop("disabled", true).addClass("text-muted");
            } else {
                button.on("click", () => choose(field));
            }
            container.append(button);
        });
    };
    const showPreview = () => {
        if (!snapshot.val() || !selectedSource || !selectedTarget) return;
        const sourceType = selectedSource.mapping_type || "string|null";
        const record = {[selectedSource.path]: sourceType.startsWith("int") ? 1 : sourceType.startsWith("boolean") ? true : "Synthetic preview value"};
        frappe.call({method: "hero_integration.api.preview_mapping_candidate", args: {
            snapshot_name: snapshot.val(), operation: operation.val(), source_path: selectedSource.path,
            target_field: selectedTarget.path, target_doctype: selectedTarget.target_doctype || targetDocType.val(), source_record_json: JSON.stringify(record),
        }}).then((response) => {
            preview.text(JSON.stringify(response.message, null, 2));
            page.set_secondary_action(__("Create Draft Mapping"), () => frappe.new_doc("HERO Frappe Mapping", {
                operation: operation.val(), source_path: selectedSource.path, source_type: selectedSource.mapping_type,
                target_doctype: selectedTarget.target_doctype || targetDocType.val(),
                target_field: selectedTarget.path, schema_snapshot: snapshot.val(), status: "Draft",
            }));
        });
    };
    const showSuggestions = (args) => {
        if (!snapshot.val()) return;
        frappe.call({method: "hero_integration.api.mapping_suggestions", args: Object.assign({snapshot_name: snapshot.val(), operation: operation.val(), target_doctype: targetDocType.val()}, args)})
            .then((response) => {
                suggestions.empty();
                const matches = response.message || [];
                if (!matches.length) return suggestions.text(__("No compatible target exists for this selection."));
                matches.forEach((match) => {
                    const button = $("<button class='btn btn-sm btn-outline-primary mr-2 mb-2'></button>")
                        .text(`${match.source_path} → ${match.target_app || "Custom"}/${match.target_doctype}.${match.target_field}: ${match.reason} (${match.confidence}, ${match.score})`)
                        .attr("title", __("This suggestion still requires review and explicit activation."));
                    button.on("click", () => {
                        selectedSource = selectedSource?.path === match.source_path ? selectedSource : {path: match.source_path, mapping_type: match.source_type};
                        selectedTarget = {path: match.target_field, target_doctype: match.target_doctype};
                        showPreview();
                    });
                    suggestions.append(button);
                });
            });
    };
    const selectSource = (field) => { selectedSource = field; showSuggestions({source_path: field.path}); showPreview(); };
    const selectTarget = (field) => { selectedTarget = field; showSuggestions({target_field: field.path, target_doctype: field.target_doctype}); showPreview(); };
    const loadSources = () => {
        if (!snapshot.val()) return;
        frappe.call({method: "hero_integration.api.mapping_source_picker", args: {snapshot_name: snapshot.val(), operation: operation.val(), search: sourceSearch.val()}})
            .then((response) => {
                const fields = response.message || [];
                renderFields(sourceList, fields, selectSource);
                const requested = fields.find((field) => field.path === pendingSourcePath && field.selectable);
                if (requested) {
                    pendingSourcePath = "";
                    selectSource(requested);
                }
            });
    };
    const loadTargets = () => frappe.call({method: "hero_integration.api.mapping_target_picker", args: {
        operation: operation.val(), search: targetSearch.val(), app_name: targetApp.val(), target_doctype: targetDocType.val(),
    }})
        .then((response) => renderFields(targetList, response.message || [], selectTarget));
    const loadTargetCatalog = () => frappe.call({method: "hero_integration.api.mapping_target_catalog", args: {app_name: targetApp.val()}})
        .then((response) => {
            const catalog = response.message || {apps: [], doctypes: []};
            const selectedApp = targetApp.val();
            targetApp.empty().append($("<option value=''>All installed apps</option>"));
            (catalog.apps || []).forEach((app) => targetApp.append($("<option></option>").val(app).text(app)));
            targetApp.val(selectedApp);
            const selectedDocType = targetDocType.val();
            targetDocType.empty().append($("<option value=''>Choose a DocType</option>"));
            (catalog.doctypes || []).forEach((item) => {
                const option = $("<option></option>").val(item.doctype).text(`${item.app_name || "Custom"} / ${item.doctype}`);
                option.prop("disabled", !item.selectable).attr("title", item.reason);
                targetDocType.append(option);
            });
            targetDocType.val(selectedDocType);
        });
    sourceSearch.on("input", loadSources);
    targetSearch.on("input", loadTargets);
    targetApp.on("change", () => { selectedTarget = null; targetDocType.val(""); loadTargetCatalog().then(loadTargets); });
    targetDocType.on("change", () => { selectedTarget = null; loadTargets(); });
    operation.on("change", () => { selectedSource = null; selectedTarget = null; suggestions.empty(); preview.empty(); loadSources(); loadTargets(); });
    const renderChecklist = (checklist) => {
        exchangeResult.empty();
        const selected = new Set(checklist.items.filter((item) => item.selectable).map((item) => item.id));
        const summary = $("<p class='text-muted'></p>");
        const groups = {};
        checklist.items.forEach((item) => (groups[item.group] ||= []).push(item));
        const updateSummary = () => {
            const selectedItems = checklist.items.filter((item) => selected.has(item.id));
            summary.text(`${selectedItems.length} selected. ${checklist.counts.Conflict || 0} conflicts and ${checklist.counts.Invalid || 0} invalid entries remain excluded.`);
        };
        const controls = $("<div class='mb-2'></div>");
        const selectAll = $("<button class='btn btn-sm btn-default mr-2'>Select all valid</button>");
        const deselectAll = $("<button class='btn btn-sm btn-default mr-2'>Clear selection</button>");
        selectAll.on("click", () => { checklist.items.filter((item) => item.selectable).forEach((item) => selected.add(item.id)); exchangeResult.find("input[data-item-id]").prop("checked", true); updateSummary(); });
        deselectAll.on("click", () => { selected.clear(); exchangeResult.find("input[data-item-id]").prop("checked", false); updateSummary(); });
        controls.append(selectAll, deselectAll, summary);
        exchangeResult.append(controls);
        Object.entries(groups).forEach(([group, items]) => {
            const section = $("<div class='mb-3'></div>").append($("<h6></h6>").text(group));
            items.forEach((item) => {
                const checkbox = $("<input type='checkbox' class='mr-2' data-item-id=''>").attr("data-item-id", item.id).prop("checked", selected.has(item.id)).prop("disabled", !item.selectable);
                checkbox.on("change", () => {
                    if (checkbox.prop("checked")) {
                        const missing = item.dependencies.filter((dependency) => !selected.has(dependency));
                        if (missing.length) {
                            missing.forEach((dependency) => selected.add(dependency));
                            missing.forEach((dependency) => exchangeResult.find(`input[data-item-id='${dependency}']`).prop("checked", true));
                            frappe.show_alert({message: __("Required dependencies were selected."), indicator: "blue"});
                        }
                        selected.add(item.id);
                    } else {
                        const dependents = checklist.items.filter((candidate) => candidate.dependencies.includes(item.id) && selected.has(candidate.id));
                        if (dependents.length) {
                            checkbox.prop("checked", true);
                            frappe.msgprint(__("Deselect dependent entries before removing this requirement."));
                            return;
                        }
                        selected.delete(item.id);
                    }
                    updateSummary();
                });
                const dependencies = item.dependencies.length ? ` Dependencies: ${item.dependencies.join(", ")}.` : "";
                section.append($("<label class='d-block'></label>").append(checkbox, document.createTextNode(`${item.state}: ${item.label}. ${item.detail}${dependencies}`)));
            });
            exchangeResult.append(section);
        });
        const apply = $("<button class='btn btn-primary'>Apply selection</button>");
        apply.on("click", () => {
            if (!selected.size) return frappe.msgprint(__("Select at least one valid change."));
            frappe.confirm(__("Provision the selected schema and activate selected mappings? This cannot migrate HERO data."), () => {
                frappe.call({method: "hero_integration.api.adopt_mapping_package", args: {
                    snapshot_name: snapshot.val(), payload_json: importPayload,
                    selected_ids_json: JSON.stringify([...selected]), confirmed: true,
                }}).then((result) => {
                    exchangeResult.append($("<pre class='mt-2'></pre>").text(JSON.stringify(result.message, null, 2)));
                    frappe.show_alert({message: __("Selection adopted."), indicator: "green"});
                });
            });
        });
        exchangeResult.append(apply);
        updateSummary();
    };
    importFile.on("change", (event) => {
        const file = event.target.files[0];
        if (!file || !snapshot.val()) return;
        const reader = new FileReader();
        reader.onload = () => {
            importPayload = reader.result;
            frappe.call({method: "hero_integration.api.preview_mapping_package", args: {snapshot_name: snapshot.val(), payload_json: importPayload}})
                .then((response) => {
                    renderChecklist(response.message);
                });
        };
        reader.readAsText(file);
    });
    page.set_primary_action(__("Open Graph Viewer"), () => {
        if (!snapshot.val()) return frappe.msgprint(__("Choose a GraphQL Schema Snapshot first."));
        window.hero_integration_ui.setRoute("hero-graph-viewer", {snapshot_name: snapshot.val(), operation: operation.val()});
    });
    page.add_menu_item(__("Export Mapping JSON"), () => {
        if (!snapshot.val()) return frappe.msgprint(__("Choose a GraphQL Schema Snapshot first."));
        frappe.call({method: "hero_integration.api.export_mapping_json", args: {snapshot_name: snapshot.val()}}).then((response) => {
            const exported = response.message;
            const url = URL.createObjectURL(new Blob([exported.content], {type: "application/json"}));
            const link = document.createElement("a");
            link.href = url;
            link.download = exported.filename;
            link.click();
            URL.revokeObjectURL(url);
        });
    });
    page.add_menu_item(__("GraphQL Services"), () => window.hero_integration_ui.setRoute("hero-graphql-services"));
    page.add_menu_item(__("Review Draft Mappings"), () => frappe.set_route("List", "HERO Frappe Mapping"));
    if (snapshot.val()) {
        loadSources();
    }
    loadTargetCatalog().then(loadTargets);
    }

    window.hero_integration_mapping_editor = Object.freeze({mount});
})();
