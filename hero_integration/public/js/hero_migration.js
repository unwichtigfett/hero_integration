(() => {
    "use strict";

    if (window.hero_integration_migration) return;

    const ACTIONS = [
        ["Customer Migration", "Creates or updates only approved customer mappings. Preview is side-effect free."],
        ["Project Migration", "Requires confirmed ERPNext Company and approved project mappings. Preview is side-effect free."],
        ["Cheap Data Migration", "Runs the bounded lightweight contract. Execute stays blocked unless active mappings permit it."],
        ["Full Data Migration", "Runs all approved migration phases. It never implicitly enables polling or GraphQL mutations."],
        ["Sync", "Uses approved idempotent mappings only. Polling never invokes a HERO GraphQL mutation."],
    ];

    function mount(page) {
        page.main.empty().addClass("hero-integration-ui");
        const root = $("<section class='hero-migration-center'></section>"), state = $("<div class='hero-migration-state' aria-live='polite'></div>"), runs = $("<div class='hero-migration-runs'></div>"), events = $("<div class='hero-migration-events'></div>"), refresh = $("<button type='button' class='btn btn-default'>Refresh stored status</button>");
        const intro = $("<section class='hero-migration-panel hero-migration-intro'></section>").append($("<h2>HERO Migration / Synchronization</h2><p class='text-muted'>Preview never writes business or link status. Execute is separately confirmed and enforced server-side for System Managers only.</p>"), refresh);
        const actions = $("<section class='hero-migration-panel'></section>").append($("<h3>Manual actions</h3>"), $("<div class='hero-migration-actions'></div>"));
        const actionGrid = actions.find(".hero-migration-actions");
        root.append(intro, actions, $("<section class='hero-migration-panel hero-migration-status'></section>").append($("<h3>Current run status</h3>"), state), $("<section class='hero-migration-panel'></section>").append($("<h3>Runs, progress, conflicts, cancellation and resume</h3>"), runs), $("<section class='hero-migration-panel'></section>").append($("<h3>Polling and error history</h3>"), events));
        ACTIONS.forEach(([action, description]) => {
            const preview = $("<button type='button' class='btn btn-default'>Preview</button>").on("click", () => start(action, 1));
            const execute = $("<button type='button' class='btn btn-primary'>Execute</button>").on("click", () => confirmStart(action));
            actionGrid.append($("<article class='hero-migration-action'></article>").append($("<h4></h4>").text(action), $("<p class='text-muted'></p>").text(description), $("<div class='hero-migration-action-buttons'></div>").append(preview, execute)));
        });
        page.main.append(root);
        function confirmStart(action) { frappe.confirm(`Execute ${action}? This queues exactly one background run after server-side System Manager and active-mapping gates succeed.`, () => start(action, 0)); }
        function start(action, dryRun) { frappe.call({method: "hero_integration.api.start_migration", args: {action, dry_run: dryRun}}).then((response) => { if (response.message.run) { state.text(`${action} ${dryRun ? "preview" : "execution"} queued as ${response.message.run}.`); load(); } else state.text(response.message.detail || response.message.status || "No run was queued."); }).catch((error) => state.text(error.message || "The server blocked this action before it could run.")); }
        function renderRuns(rows) {
            runs.empty(); if (!rows.length) return runs.append($("<p class='text-muted'></p>").text("No stored runs yet."));
            const table = $("<table class='table table-sm'><thead><tr><th>Run</th><th>Action</th><th>Mode</th><th>State</th><th>Progress</th><th>Actions</th></tr></thead><tbody></tbody></table>");
            rows.forEach((row) => {
                const controls = $("<span></span>");
                if (["Queued", "Running"].includes(row.state)) controls.append($("<button type='button' class='btn btn-xs btn-default'>Cancel</button>").on("click", () => frappe.call({method: "hero_integration.api.cancel_run", args: {run_name: row.name}}).then(load)));
                if (["Failed", "Cancelled", "Blocked"].includes(row.state)) controls.append($("<button type='button' class='btn btn-xs btn-default'>Resume</button>").on("click", () => frappe.call({method: "hero_integration.api.resume_run", args: {run_name: row.name}}).then(load)));
                table.find("tbody").append($("<tr></tr>").append($("<td></td>").text(row.name), $("<td></td>").text(row.action), $("<td></td>").text(row.mode), $("<td></td>").text(row.state), $("<td></td>").text(`${row.processed || 0} processed; ${row.conflict_records || 0} conflicts`), $("<td></td>").append(controls)));
            }); runs.append(table);
        }
        function renderEvents(data) {
            events.empty(); const plan = data.polling || {};
            events.append($("<p class='text-muted'></p>").text(`Mode: ${plan.mode || "OFF"}. ${plan.reconciliation || ""} ${plan.watermarks || ""}`));
            if (plan.unsupported_sources?.length) events.append($("<p class='text-warning'></p>").text(`Unavailable: ${plan.unsupported_sources.join(", ")} require a supported runtime adapter; no request is made.`));
            if (data.sync_error) events.append($("<p class='text-danger'></p>").text(data.sync_error));
            const rows = data.events || []; if (!rows.length) return events.append($("<p class='text-muted'></p>").text("No polling events yet. OFF remains inactive."));
            const list = $("<ul></ul>"); rows.forEach((row) => list.append($("<li></li>").text(`${row.modified}: ${row.mode} ${row.status} — ${row.detail || ""}`))); events.append(list);
        }
        function load() { state.text("Loading stored status …"); return frappe.call({method: "hero_integration.api.workspace_sync_status"}).then((response) => { const data = response.message || {}; state.text(`Status: ${data.sync_status || "Not started"}. Last success: ${data.last_successful_sync || "never"}. Watermarks: ${data.watermarks?.status || "Not established"}.`); renderRuns(data.runs || []); renderEvents(data); }).catch((error) => state.text(error.message || "Stored status could not be loaded.")); }
        refresh.on("click", load); ACTIONS.forEach(([action]) => page.add_menu_item(action + " Preview", () => start(action, 1))); load();
    }

    window.hero_integration_migration = Object.freeze({mount});
})();
