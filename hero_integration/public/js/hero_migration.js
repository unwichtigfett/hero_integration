/* HERO Migration / Synchronization page: four manual area actions, the sync service and the runtime views. */
(() => {
    "use strict";

    if (window.hero_integration_migration) return;

    const API = "hero_integration.api.";
    const PAGE_SIZE = 8;
    const REFRESH_MS = 5000;
    // Simple stroke icons (24×24 path data), drawn with the current text colour.
    const ICONS = {
        contacts: ["M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M4 20c0-3.3 3.6-6 8-6s8 2.7 8 6"],
        projects: ["M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"],
        events: ["M4 6h16v14H4Z", "M4 10h16", "M8 3v5", "M16 3v5"],
        tasks: ["M4 4h16v16H4Z", "M8 12l3 3 5-6"],
    };
    const MODE_LABELS = {OFF: "Off", MONITOR: "Monitor only", AUTO_SYNC: "Automatic sync"};
    const STATE_TONE = {Completed: "ok", "Completed With Conflicts": "warn", Running: "busy", Queued: "busy", Failed: "error", Blocked: "error", Cancelled: "muted"};
    const FUNCTION_TEXT = "Start / Update migrates the object fields included in the active mapping. Existing records are matched by source identity, bindings remain stable, and newly mapped fields are backfilled subject to validation and conflict rules.";

    function el(tag, attrs = {}, ...children) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (value === undefined || value === null || value === false) continue;
            if (key === "class") node.className = value;
            else if (key === "text") node.textContent = value;
            else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
            else node.setAttribute(key, value === true ? "" : value);
        }
        node.append(...clean(children));
        return node;
    }
    const clean = (children) => children.flat(Infinity).filter((child) => child !== null && child !== undefined && child !== false);
    const put = (node, ...children) => node.replaceChildren(...clean(children));
    const add = (node, ...children) => node.append(...clean(children));

    function errorMessage(error) {
        if (typeof error?.message === "string" && error.message) return error.message;
        try {
            const messages = JSON.parse(error?._server_messages || error?.responseJSON?._server_messages || "[]");
            const first = messages.map((item) => typeof item === "string" ? JSON.parse(item) : item).find(Boolean);
            if (first?.message) return first.message;
        } catch (_) { /* not JSON */ }
        return "The server rejected the request. Check the server log for details.";
    }

    function call(method, args = {}, type = "GET") {
        return new Promise((resolve, reject) => {
            frappe.call({method: API + method, args, type, callback: (response) => resolve(response?.message), error: (error) => reject(new Error(errorMessage(error)))})
                .catch?.((error) => reject(new Error(errorMessage(error))));
        });
    }

    const toDate = (value) => value ? new Date(String(value).replace(" ", "T")) : null;
    function formatDateTime(value) {
        const date = toDate(value);
        if (!date || Number.isNaN(date.getTime())) return "—";
        const pad = (number) => String(number).padStart(2, "0");
        return `${pad(date.getDate())}.${pad(date.getMonth() + 1)}.${date.getFullYear()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
    }
    function formatDuration(start, end) {
        const from = toDate(start), to = end ? toDate(end) : new Date();
        if (!from || !to || Number.isNaN(from.getTime()) || Number.isNaN(to.getTime())) return "—";
        const seconds = Math.max(0, Math.round((to - from) / 1000));
        const pad = (number) => String(number).padStart(2, "0");
        return `${pad(Math.floor(seconds / 3600))}:${pad(Math.floor(seconds / 60) % 60)}:${pad(seconds % 60)}`;
    }
    const number = (value) => Number(value || 0).toLocaleString("de-DE");
    const plural = (count, singular, pluralText) => `${number(count)} ${count === 1 ? singular : pluralText}`;

    function icon(name) {
        const ns = "http://www.w3.org/2000/svg";
        const svg = document.createElementNS(ns, "svg");
        svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("class", "hero-sync-icon"); svg.setAttribute("aria-hidden", "true");
        (ICONS[name] || []).forEach((data) => { const path = document.createElementNS(ns, "path"); path.setAttribute("d", data); svg.append(path); });
        return svg;
    }

    function toggle(label, checked, disabled, onChange, extraClass = "") {
        const input = el("button", {type: "button", role: "switch", class: `hero-sync-switch ${extraClass}`.trim(), "aria-checked": checked ? "true" : "false", disabled: disabled || undefined, "aria-label": label});
        input.append(el("span", {class: "hero-sync-switch-track", "aria-hidden": "true"}, el("span", {class: "hero-sync-switch-thumb"})));
        input.addEventListener("click", () => onChange(!checked));
        return input;
    }

    function mount(page) {
        page.main.empty().addClass("hero-integration-ui");
        const host = page.main.get(0);
        const state = {data: null, view: "migration", tab: "history", runsStart: 0, conflictsStart: 0, selectedRun: "", selectedArea: "contacts", selectedConflict: null, selectedConflicts: new Set(), busy: false, timer: null, destroyed: false, runs: null, conflicts: null, detail: null};

        const notice = el("div", {class: "hero-sync-notice", role: "status", "aria-live": "polite"});
        const head = el("header", {class: "hero-sync-panel hero-sync-head", "data-views": "migration"},
            el("h2", {text: "Hero Migration / Synchronization"}),
            el("p", {text: "Import and synchronize data from HERO to Frappe."}), notice);
        const subscription = el("section", {class: "hero-sync-panel hero-sync-subscription", "data-views": "sync", "aria-label": "Subscription Service"});
        const viewSwitch = el("nav", {class: "hero-sync-views", "aria-label": "Synchronization views"},
            ...[["migration", "Migration"], ["sync", "Sync"], ["runtime", "Runtime"]].map(([key, label]) =>
                el("button", {type: "button", class: "hero-sync-view-button", "data-view": key, text: label, onclick: () => { state.view = key; renderView(); }})));
        const manual = el("section", {class: "hero-sync-panel hero-sync-manual", "data-views": "migration", "aria-labelledby": "hero-sync-manual-title"});
        const service = el("section", {class: "hero-sync-panel hero-sync-service", "data-views": "sync", "aria-label": "Sync Services"});
        const states = el("section", {class: "hero-sync-panel hero-sync-states", "data-views": "migration sync", "aria-label": "States"});
        const work = el("section", {class: "hero-sync-panel hero-sync-work", "data-views": "runtime"});
        const root = el("section", {class: "hero-sync", "data-view": state.view}, viewSwitch, head, subscription, manual, service, states, work);
        // The frame is the size container: the layout follows the space the page really has.
        host.append(el("div", {class: "hero-sync-frame"}, root));

        function renderView() {
            root.dataset.view = state.view;
            viewSwitch.querySelectorAll("button").forEach((button) => button.setAttribute("aria-pressed", button.dataset.view === state.view ? "true" : "false"));
        }

        function say(text, tone = "") { notice.textContent = text || ""; notice.dataset.tone = tone; }

        // --- Subscription service (global switch) --------------------------------
        function renderSubscription() {
            const svc = state.data.service;
            const on = svc.allowed && !svc.paused;
            const label = !svc.allowed ? "Polling is off in Settings" : svc.mode === "MONITOR" ? "Monitor only: reads and reports, no data changes" : "Polling / Update";
            const button = el("button", {type: "button", role: "switch", class: "hero-sync-global", "aria-checked": on ? "true" : "false", disabled: (!svc.allowed || state.busy) || undefined,
                title: svc.allowed ? "Switches automatic processing on or off. Polling settings stay unchanged." : "Choose a polling mode under Settings first.",
                onclick: () => setService({enabled: on ? 0 : 1})}, el("span", {class: "hero-sync-dot", "aria-hidden": "true"}), on ? "Sync on" : "Sync off");
            put(subscription, 
                el("span", {class: `hero-sync-lamp ${on ? "is-on" : ""}`, "aria-hidden": "true"}),
                el("div", {class: "hero-sync-subscription-text"}, el("h3", {text: "Subscription Service"}), el("p", {text: label})),
                button);
        }

        // --- Manual actions ------------------------------------------------------
        function renderManual() {
            const activeRun = state.data.active_run;
            const cards = state.data.areas.map((area) => {
                const running = Boolean(area.running_run);
                const blockedByOther = Boolean(activeRun) && !running;
                const button = el("button", {type: "button", class: "hero-sync-action", "data-area": area.key,
                    disabled: (running || blockedByOther || !area.ready || state.busy) || undefined,
                    title: running ? "This area is running." : blockedByOther ? `Run ${activeRun.name} is still in progress.` : area.ready ? "" : area.blocker,
                    onclick: () => startArea(area)}, running ? "Running …" : area.button);
                const scope = area.scope || {};
                return el("article", {class: `hero-sync-card ${state.selectedArea === area.key ? "is-selected" : ""}`, "data-area": area.key},
                    el("button", {type: "button", class: "hero-sync-card-title", title: "Show function details", onclick: () => { state.selectedArea = area.key; state.tab = "details"; renderManual(); renderWork(); }},
                        icon(area.key), el("h4", {text: area.action_label})),
                    el("p", {class: "hero-sync-card-text", text: area.description}),
                    el("div", {class: "hero-sync-card-block"}, el("span", {class: "hero-sync-caption", text: "Mapped objects"}),
                        scope.object_count ? el("span", {text: plural(scope.object_count, "object", "objects")}) : el("span", {text: "No active mapping"}),
                        scope.object_count ? el("span", {text: plural(scope.field_count, "field", "fields")}) : null),
                    el("div", {class: "hero-sync-card-block"}, el("span", {class: "hero-sync-caption", text: "Last successful"}), el("span", {text: formatDateTime(area.last_successful)})),
                    area.ready ? null : el("p", {class: "hero-sync-card-blocker", text: "Not ready: approved read service missing."}),
                    button);
            });
            put(manual, 
                el("div", {class: "hero-sync-section-head"}, el("h3", {id: "hero-sync-manual-title", text: "Manual Actions"}), el("span", {text: "Start or update migrations for individual data areas"})),
                el("div", {class: "hero-sync-cards"}, cards));
        }

        function startArea(area) {
            const verb = area.button === "Start" ? "Start" : "Update";
            frappe.confirm(`${verb} ${area.action_label}? This migrates the object fields of the active mapping for ${area.label} from HERO into Frappe.`, async () => {
                state.busy = true; renderAll();
                say(`${area.action_label} is being queued …`);
                try {
                    const result = await call("sync_area_start", {area: area.key}, "POST");
                    say(`${area.action_label} queued as ${result.run}.`, "ok");
                    state.selectedRun = result.run; state.tab = "history"; state.runsStart = 0;
                } catch (error) {
                    say(error.message, "error");
                } finally {
                    state.busy = false;
                    await refresh();
                }
            });
        }

        // --- Sync service ----------------------------------------------------------
        function readonly(label, value, unit = "") {
            return el("div", {class: "hero-sync-row"}, el("span", {class: "hero-sync-row-label", text: label}),
                el("span", {class: "hero-sync-value"}, el("span", {text: value}), unit ? el("span", {class: "hero-sync-unit", text: unit}) : null));
        }
        function renderService() {
            const svc = state.data.service;
            const switches = state.data.areas.map((area) => el("div", {class: "hero-sync-row"},
                el("span", {class: "hero-sync-row-label", text: area.label}),
                el("span", {class: "hero-sync-switch-wrap"},
                    toggle(`Sync ${area.label}`, area.sync_enabled, state.busy, (value) => setService({area: area.key, area_enabled: value ? 1 : 0})),
                    el("span", {class: "hero-sync-switch-state", text: area.sync_enabled ? "On" : "Off"}))));
            const monitorNote = svc.mode === "MONITOR" ? el("p", {class: "hero-sync-hint", text: "Monitor only reads Contacts and Projects; it never changes data."}) : null;
            put(service, 
                el("div", {class: "hero-sync-service-grid"},
                    el("div", {class: "hero-sync-sub hero-sync-sub-areas"}, el("h3", {text: "Sync Services"}), el("p", {class: "hero-sync-hint", text: "Enable or disable synchronization per area."}), switches, monitorNote),
                    el("div", {class: "hero-sync-sub hero-sync-sub-mode"},
                        el("div", {class: "hero-sync-row"}, el("h3", {text: "Mode"}), el("span", {class: `hero-sync-mode mode-${String(svc.mode).toLowerCase()}`, text: MODE_LABELS[svc.mode] || svc.mode})),
                        readonly("Exec. time", svc.execution_window || "Any time"),
                        readonly("Timezone", svc.timezone),
                        readonly("Pagination", svc.pagination_strategy === "OFFSET" ? "Offset" : svc.pagination_strategy),
                        readonly("Timeout", String(svc.timeout_seconds), "sec"),
                        readonly("Notification", svc.notifications ? "On" : "Off")),
                    el("div", {class: "hero-sync-sub hero-sync-sub-info"}, el("h3", {text: "Service Information"}),
                        readonly("Polling interval", String(svc.interval_minutes), "min"),
                        readonly("Page size", String(svc.page_size)),
                        readonly("Retry limit", String(svc.retry_limit)),
                        readonly("Retry backoff", String(svc.retry_backoff_seconds), "sec"),
                        readonly("Requests per minute", String(svc.rate_limit)),
                        el("p", {class: "hero-sync-hint", text: "Edit these values under Settings."}))),
                svc.sync_error ? el("p", {class: "hero-sync-error", text: svc.sync_error}) : null);
        }

        async function setService(args) {
            state.busy = true; renderAll();
            try {
                state.data = await call("sync_service_set", args, "POST");
                say("Sync service updated.", "ok");
            } catch (error) {
                say(error.message, "error");
            } finally {
                state.busy = false; renderAll();
            }
        }

        // --- States ------------------------------------------------------------------
        function metric(label, value, tone = "") {
            return el("div", {class: "hero-sync-metric"}, el("span", {class: "hero-sync-metric-label", text: label}), el("span", {class: `hero-sync-metric-value ${tone}`, text: number(value)}));
        }
        function renderStates() {
            const metrics = state.data.metrics, records = metrics.records, polling = metrics.polling;
            put(states, 
                el("div", {class: "hero-sync-states-group"},
                    el("div", {class: "hero-sync-section-head"}, el("h3", {text: "States"}), el("span", {text: "Linked records per area"})),
                    el("div", {class: "hero-sync-metrics"}, state.data.areas.map((area) => metric(area.label, records[area.key])))),
                el("div", {class: "hero-sync-states-group"},
                    el("div", {class: "hero-sync-section-head"}, el("h3", {text: "Polling"}), el("span", {text: `Last ${polling.period_days} days`})),
                    el("div", {class: "hero-sync-metrics"}, metric("Runs", polling.runs), metric("Pass", polling.pass, "ok"), metric("Fail", polling.fail, polling.fail ? "error" : ""),
                        metric("Incomplete", polling.incomplete), metric("Skipped", polling.skipped))));
        }

        // --- Lower work area -----------------------------------------------------------
        const TABS = [["history", "Runtime History"], ["details", "Function Details"], ["conflicts", "Conflicts"]];
        function renderWork() {
            const tabs = el("div", {class: "hero-sync-tabs", role: "tablist"}, TABS.map(([key, label]) =>
                el("button", {type: "button", role: "tab", class: "hero-sync-tab", "aria-selected": state.tab === key ? "true" : "false",
                    onclick: () => { state.tab = key; renderWork(); loadTab(); }, text: label})));
            const main = el("div", {class: "hero-sync-work-main"}), side = el("aside", {class: "hero-sync-work-side", "aria-live": "polite"});
            if (state.tab === "history") renderHistory(main, side);
            else if (state.tab === "details") renderDetails(main, side);
            else renderConflicts(main, side);
            put(work, tabs, el("div", {class: "hero-sync-work-body"}, main, side));
        }

        function pager(start, total, shown, onPage) {
            const pages = Math.max(1, Math.ceil(total / PAGE_SIZE)), current = Math.floor(start / PAGE_SIZE);
            const first = Math.max(0, Math.min(current - 2, pages - 5));
            const buttons = [];
            for (let index = first; index < Math.min(pages, first + 5); index += 1) {
                buttons.push(el("button", {type: "button", class: "hero-sync-page", "aria-current": index === current ? "page" : undefined, text: String(index + 1), onclick: () => onPage(index * PAGE_SIZE)}));
            }
            return el("div", {class: "hero-sync-pager"},
                el("div", {class: "hero-sync-pages"},
                    el("button", {type: "button", class: "hero-sync-page", "aria-label": "Previous page", disabled: current === 0 || undefined, text: "‹", onclick: () => onPage((current - 1) * PAGE_SIZE)}),
                    buttons,
                    el("button", {type: "button", class: "hero-sync-page", "aria-label": "Next page", disabled: current >= pages - 1 || undefined, text: "›", onclick: () => onPage((current + 1) * PAGE_SIZE)})),
                el("span", {class: "hero-sync-hint", text: total ? `${start + 1}–${start + shown} of ${number(total)}` : "0 of 0"}));
        }

        function statusBadge(value) {
            return el("span", {class: `hero-sync-status tone-${STATE_TONE[value] || "muted"}`}, el("span", {class: "hero-sync-dot", "aria-hidden": "true"}), value || "—");
        }
        function progress(row) {
            const tone = STATE_TONE[row.state] || "muted";
            // HERO reports no total count, so a bar is shown only for finished or running work.
            const finished = ["Completed", "Completed With Conflicts"].includes(row.state);
            return el("div", {class: "hero-sync-progress"},
                finished || tone === "busy" ? el("span", {class: `hero-sync-bar tone-${tone} ${finished ? "is-full" : "is-running"}`, "aria-hidden": "true"}, el("span")) : null,
                el("span", {text: `${number(row.processed)} processed${row.conflict_records ? ` · ${number(row.conflict_records)} conflicts` : ""}`}));
        }

        function renderHistory(main, side) {
            const data = state.runs;
            if (!data) { add(main, el("p", {class: "hero-sync-hint", text: "Loading runtime history …"})); }
            else if (!data.rows.length) { add(main, el("p", {class: "hero-sync-hint", text: "No runs yet. Start a manual action to create the first run."})); }
            else {
                const body = el("tbody", {}, data.rows.map((row) => el("tr", {class: row.name === state.selectedRun ? "is-selected" : ""},
                    el("td", {class: "hero-sync-mono", text: row.name}),
                    el("td", {text: row.area}),
                    el("td", {}, statusBadge(row.state)),
                    el("td", {}, progress(row)),
                    el("td", {class: "is-optional", text: `${number(row.created_records)} / ${number(row.processed)}`}),
                    el("td", {text: formatDateTime(row.started_on || row.creation)}),
                    el("td", {class: "is-optional", text: row.started_on ? formatDuration(row.started_on, row.finished_on) : "—"}),
                    el("td", {}, el("button", {type: "button", class: "hero-sync-small", text: "View", onclick: () => selectRun(row.name)})))));
                add(main, el("div", {class: "hero-sync-table-wrap"}, el("table", {class: "hero-sync-table"},
                    el("thead", {}, el("tr", {}, [["ID"], ["Action"], ["Status"], ["Progress"], ["Created / processed", "is-optional"], ["Started"], ["Duration", "is-optional"], ["Action"]].map(([label, cls]) => el("th", {scope: "col", class: cls, text: label})))),
                    body)), pager(data.start, data.total, data.rows.length, (start) => { state.runsStart = Math.max(0, start); loadRuns(); }));
            }
            renderRunDetail(side);
        }

        function renderRunDetail(side) {
            const run = state.detail;
            if (!state.selectedRun) { add(side, el("p", {class: "hero-sync-empty", text: "Select a run to view details."})); return; }
            if (!run || run.name !== state.selectedRun) { add(side, el("p", {class: "hero-sync-hint", text: "Loading run details …"})); return; }
            const facts = [
                ["Action", run.area], ["Status", run.state], ["Mode", run.mode], ["Source instance", run.source_instance || "—"],
                ["Started", formatDateTime(run.started_on)], ["Finished", formatDateTime(run.finished_on)],
                ["Duration", run.started_on ? formatDuration(run.started_on, run.finished_on) : "—"],
                ["Processed", number(run.processed)], ["Created", number(run.created_records)], ["Updated or unchanged", number(run.skipped_records)], ["Conflicts", number(run.conflict_records)],
                ["Read service", run.graphql_service || "—"], ["Mapping version", run.graphql_mapping ? `${run.graphql_mapping} · rev. ${run.mapping_revision || 0}` : "—"],
            ];
            if (run.customer_phase_state) facts.push(["Contacts phase", `${run.customer_phase_state} · ${number(run.customer_phase_processed)} processed`]);
            if (run.project_phase_state && run.action === "Project Migration") facts.push(["Projects phase", `${run.project_phase_state} · ${number(run.project_phase_processed)} processed`]);
            if (Number(run.testing_enabled)) {
                facts.push(["Testing", `${run.testing_status || "PENDING"} · ${run.testing_executed_actions || 0}/${run.testing_max_actions || 0} HERO requests · ${number(run.testing_records_received)} records`]);
                try { Object.entries(JSON.parse(run.testing_results_json || "{}")).forEach(([name, result]) => facts.push([name, result.status])); } catch (_) { facts.push(["Testing results", "Results unavailable"]); }
            }
            const actions = el("div", {class: "hero-sync-detail-actions"},
                run.can_cancel ? el("button", {type: "button", class: "hero-sync-small", text: "Cancel", onclick: () => runAction("cancel_run", run, "Cancel this run at the next safe point? Records already written stay in place.")}) : null,
                run.can_continue ? el("button", {type: "button", class: "hero-sync-small is-primary", text: "Continue", onclick: () => runAction("resume_run", run, "Continue this run from its saved position? It keeps its original mapping version; current approvals are checked again.")}) : null);
            const outcomes = Object.entries(run.outcomes || {});
            add(side, 
                el("h4", {class: "hero-sync-mono", text: run.name}),
                el("dl", {class: "hero-sync-facts"}, facts.map(([label, value]) => [el("dt", {text: label}), el("dd", {text: value})])),
                run.error_message ? el("p", {class: "hero-sync-error", text: run.error_message}) : null,
                outcomes.length ? el("div", {class: "hero-sync-chips"}, outcomes.map(([code, count]) => el("span", {class: "hero-sync-chip", text: `${code}: ${number(count)}`}))) : null,
                run.conflicts?.length ? el("div", {class: "hero-sync-detail-list"}, el("h5", {text: "Conflicts in this run"}),
                    el("ul", {}, run.conflicts.map((item) => el("li", {text: `${item.source_type} ${item.source_id}: ${item.detail || item.result}`})))) : null,
                actions);
        }

        function runAction(method, run, question) {
            frappe.confirm(question, async () => {
                try {
                    await call(method, {run_name: run.name}, "POST");
                    say(method === "cancel_run" ? `Run ${run.name} is being cancelled.` : `Run ${run.name} is queued to continue.`, "ok");
                } catch (error) {
                    say(error.message, "error");
                }
                await refresh();
            });
        }

        function renderDetails(main, side) {
            const area = state.data.areas.find((item) => item.key === state.selectedArea) || state.data.areas[0];
            add(main, 
                el("div", {class: "hero-sync-area-picker", role: "group", "aria-label": "Action"}, state.data.areas.map((item) =>
                    el("button", {type: "button", class: "hero-sync-small", "aria-pressed": item.key === area.key ? "true" : "false", text: item.action_label, onclick: () => { state.selectedArea = item.key; renderManual(); renderWork(); }}))),
                el("h4", {text: area.action_label}),
                el("p", {text: area.scope_note}),
                el("p", {class: "hero-sync-quote", text: FUNCTION_TEXT}),
                el("p", {class: "hero-sync-hint", text: `${area.button === "Start" ? "Start runs the first import" : "Update processes new records, changed records and newly mapped fields"} for the HERO source instance "${state.data.source_instance}". You do not choose GraphQL services; the server resolves them from the active mapping.`}),
                el("h5", {text: "Mapped objects"}),
                area.scope.objects.length
                    ? el("div", {class: "hero-sync-scope"}, area.scope.objects.map((object) => el("div", {class: "hero-sync-scope-object"},
                        el("strong", {text: `${object.doctype} · ${plural(object.fields.length, "field", "fields")}`}),
                        el("div", {class: "hero-sync-chips"}, object.fields.map((field) => el("span", {class: "hero-sync-chip", text: field}))))))
                    : el("p", {class: "hero-sync-hint", text: "No active mapping rules exist for this area. Create and activate them under Mapping."}));
            add(side, 
                el("h4", {text: "Prerequisites"}),
                el("dl", {class: "hero-sync-facts"},
                    el("dt", {text: "Status"}), el("dd", {text: area.ready ? "Ready" : "Not ready"}),
                    el("dt", {text: "Read service"}), el("dd", {text: area.service?.service || "—"}),
                    el("dt", {text: "Snapshot"}), el("dd", {text: area.service?.snapshot || "—"}),
                    el("dt", {text: "Last successful"}), el("dd", {text: formatDateTime(area.last_successful)}),
                    el("dt", {text: "Automatic sync"}), el("dd", {text: area.sync_enabled ? (area.polled ? "On · read by polling" : "On") : "Off"})),
                area.blocker ? el("p", {class: "hero-sync-error", text: area.blocker}) : null);
        }

        async function decideConflicts(rows, decision) {
            if (!rows.length || state.busy) return;
            state.busy = true; renderWork();
            let completed = 0;
            for (const row of rows) {
                try { await call("decide_hero_review", {link: row.name, decision}, "POST"); completed++; state.selectedConflicts.delete(row.name); }
                catch (error) { say(`${completed} processed. ${row.name}: ${error.message}`, "error"); break; }
            }
            state.busy = false;
            if (completed === rows.length) say(`${completed} decisions saved. Conflicts marked for recheck are checked on the next synchronization.`, "ok");
            await loadConflicts();
        }

        function renderConflicts(main, side) {
            const data = state.conflicts;
            add(main, el("h3", {text: "Importing Conflicts"}));
            if (!data) { add(main, el("p", {class: "hero-sync-hint", text: "Loading conflicts …"})); return; }
            if (!data.rows.length) { add(main, el("p", {class: "hero-sync-hint", text: "No open conflicts."})); return; }
            const decidable = (row) => row.kind !== "record";
            const chosen = () => data.rows.filter((row) => decidable(row) && state.selectedConflicts.has(row.name));
            add(main, el("div", {class: "hero-sync-actions"},
                el("button", {type: "button", text: "Approve selected", disabled: state.busy || undefined, onclick: () => decideConflicts(chosen(), "approve")}),
                el("button", {type: "button", text: "Decline selected", disabled: state.busy || undefined, onclick: () => decideConflicts(chosen(), "reject")})),
                el("div", {class: "hero-sync-table-wrap"}, el("table", {class: "hero-sync-table"},
                    el("thead", {}, el("tr", {}, ["Select", "ID", "Conflict", "Entity Type", "Sync State", "Approve / Decline"].map((label) => el("th", {scope: "col", text: label})))),
                    el("tbody", {}, data.rows.map((row) => el("tr", {},
                        el("td", {}, decidable(row) ? el("input", {type: "checkbox", "aria-label": `Select ${row.name}`, checked: state.selectedConflicts.has(row.name) || undefined,
                            onchange: (event) => event.target.checked ? state.selectedConflicts.add(row.name) : state.selectedConflicts.delete(row.name)}) : null),
                        el("td", {class: "hero-sync-mono", text: decidable(row) ? row.name : `HERO ${row.source_id}`}),
                        el("td", {text: row.conflict_detail || "Conflict requires review."}),
                        el("td", {text: row.area}),
                        el("td", {text: !decidable(row) ? `Not imported (${row.run})` : row.sync_status === "Pending Review" ? "Confirmation pending" : (row.conflict_detail || "").startsWith("Recheck requested: ") ? "Recheck requested" : "Conflict"}),
                        el("td", {}, decidable(row) ? [el("button", {type: "button", class: "hero-sync-small", text: "Approve", disabled: state.busy || undefined, onclick: () => decideConflicts([row], "approve")}),
                            el("button", {type: "button", class: "hero-sync-small", text: "Decline", disabled: state.busy || undefined, onclick: () => decideConflicts([row], "reject")})]
                            : el("span", {class: "hero-sync-hint", text: "Fix in HERO; the next run imports it."}))))))),
                pager(data.start, data.total, data.rows.length, (start) => { state.conflictsStart = Math.max(0, start); state.selectedConflicts.clear(); loadConflicts(); }));
            add(side, el("p", {class: "hero-sync-hint", text: "Approving a field conflict requests a recheck on the next synchronization. Declining keeps the existing record and stops further updates. Records that were not imported at all come from the latest run of each area and have nothing to approve."}));
        }

        // --- Loading -------------------------------------------------------------------------
        async function loadRuns() {
            try { state.runs = await call("sync_page_runs", {start: state.runsStart, page_size: PAGE_SIZE}); }
            catch (error) { state.runs = {start: 0, total: 0, rows: []}; say(error.message, "error"); }
            if (state.selectedRun) await loadDetail();
            if (state.tab === "history") renderWork();
        }
        async function loadDetail() {
            try { state.detail = await call("sync_page_run_detail", {run_name: state.selectedRun}); }
            catch (error) { state.detail = null; state.selectedRun = ""; say(error.message, "error"); }
        }
        async function selectRun(name) {
            state.selectedRun = name; state.detail = null; renderWork();
            await loadDetail(); renderWork();
        }
        async function loadConflicts() {
            try { state.conflicts = await call("sync_page_conflicts", {start: state.conflictsStart, page_size: PAGE_SIZE}); }
            catch (error) { state.conflicts = {start: 0, total: 0, rows: []}; say(error.message, "error"); }
            if (state.tab === "conflicts") renderWork();
        }
        function loadTab() {
            if (state.tab === "history") return loadRuns();
            if (state.tab === "conflicts") return loadConflicts();
            return Promise.resolve();
        }

        function renderAll() {
            if (!state.data) return;
            renderView(); renderSubscription(); renderManual(); renderService(); renderStates(); renderWork();
        }

        async function refresh() {
            if (state.destroyed) return;
            try {
                state.data = await call("sync_page_state");
            } catch (error) {
                if (!state.data) { root.replaceChildren(el("p", {class: "hero-sync-error", text: error.message})); return; }
                say(error.message, "error");
            }
            renderAll();
            await loadTab();
            schedule();
        }
        function schedule() {
            clearTimeout(state.timer);
            // Refresh only while a run is queued or running, and only while the page is shown.
            if (!state.destroyed && state.data?.active_run && root.isConnected) state.timer = setTimeout(refresh, REFRESH_MS);
        }

        renderView();
        manual.append(el("p", {class: "hero-sync-hint", text: "Loading stored synchronization state …"}));
        refresh();
        return {destroy() { state.destroyed = true; clearTimeout(state.timer); }};
    }

    window.hero_integration_migration = Object.freeze({mount});
})();
