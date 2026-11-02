(() => {
    "use strict";

    const administratorI18n =
        window.hero_integration_i18n;

    const uiText =
        administratorI18n
            ? administratorI18n.text
            : (
                value
            ) => value;
    const format = (message, ...values) => uiText(message).replace(/\{(\d+)\}/g, (_, index) => String(values[index] ?? ""));

    if (window.hero_integration_settings_ui) return;

    const platform = window.hero_integration_platform;
    const GET = "workspace_settings_get";
    const SAVE = "workspace_settings_save";
    const SAVE_TESTING = "workspace_testing_settings_save";
    const REFRESH_SCHEMA = "workspace_schema_refresh";
    const SET_MUTATION_EXECUTION = "workspace_mutation_execution_set";

    function node(tag, cls, text) {
        const el = document.createElement(tag);
        if (cls) el.className = cls;
        if (text !== undefined) el.textContent = uiText(text);
        return el;
    }

    function field(label, input, help = "") {
        const wrap = node("label", "hero-settings-field");
        wrap.append(node("span", "hero-settings-label", label), input);
        if (help) wrap.append(node("span", "hero-settings-help", help));
        return wrap;
    }

    function errorMessage(error) {
        return typeof error?.message === "string" && error.message
            ? error.message
            : "The server did not provide additional details.";
    }

    // One settings group: a panel with a title, an optional description and a field grid.
    function section(title, layout, description, ...children) {
        const panel = node("section", `hero-page-panel hero-settings-section hero-settings-${layout}`);
        const heading = node("h3", "hero-settings-section-title", title);
        heading.id = `hero-settings-${layout}-title`;
        panel.setAttribute("aria-labelledby", heading.id);
        const grid = node("div", "hero-settings-grid");
        grid.append(...children);
        panel.append(heading);
        if (description) panel.append(node("p", "hero-settings-section-help", description));
        panel.append(grid);
        return panel;
    }

    function column(cls, ...children) {
        const wrap = node("div", cls);
        wrap.append(...children);
        return wrap;
    }

    function pollingGroup(title, ...fields) {
        const group = node("fieldset", "hero-settings-polling-group");
        group.append(node("legend", "", title), ...fields);
        return group;
    }

    async function mount(workspace) {
        const root = node("section", "hero-page hero-settings");
        const header = node("header", "hero-page-header hero-settings-header");
        const heading = node("div");
        heading.append(
            node("h2", "", "HERO Settings"),
            node("p", "", "Configure the connection stored by HERO Integration.")
        );
        const refresh = node("button", "hero-page-button is-quiet hero-settings-button", "Refresh");
        refresh.type = "button";
        const save = node("button", "hero-page-button hero-settings-button primary", "Save Settings");
        save.type = "button";
        save.title = uiText("Saves connection, sources, safety and polling settings. A new or unverified API token loads the HERO schema right away.");
        const reset = node("button", "hero-page-button is-quiet hero-settings-button", "Reset");
        reset.type = "button";
        reset.title = uiText("Discards unsaved changes and shows the saved settings again.");
        const headerActions = node("div", "hero-page-header-actions");
        headerActions.append(refresh);
        header.append(heading, headerActions);

        const enabled = document.createElement("input");
        enabled.type = "checkbox";
        enabled.className = "hero-settings-check";

        const url = document.createElement("input");
        url.type = "url";
        url.className = "hero-settings-input";
        url.placeholder = "https://…/graphql";
        url.autocomplete = "off";

        const allowedHosts = document.createElement("textarea");
        allowedHosts.className = "hero-settings-input";
        allowedHosts.rows = 2;
        const sourceInstance = document.createElement("input");
        sourceInstance.className = "hero-settings-input";
        const bindingConfirm = document.createElement("input");
        bindingConfirm.type = "checkbox";
        bindingConfirm.className = "hero-settings-check";
        const bindingStatus = node("div", "hero-settings-status", "Source binding is not loaded yet.");
        bindingStatus.setAttribute("role", "status");
        const allowDeletion = document.createElement("input");
        allowDeletion.type = "checkbox";
        allowDeletion.className = "hero-settings-check";
        const pollingMode = document.createElement("select");
        pollingMode.className = "hero-settings-input";
        ["OFF", "MONITOR", "AUTO_SYNC"].forEach(value => {
            const option = document.createElement("option"); option.value = value; option.textContent = value; pollingMode.append(option);
        });
        const pollingInterval = document.createElement("input");
        pollingInterval.type = "number"; pollingInterval.min = "1"; pollingInterval.max = "1440"; pollingInterval.className = "hero-settings-input";
        const pollingTimezone = document.createElement("input"); pollingTimezone.className = "hero-settings-input";
        const pollingWindow = document.createElement("input"); pollingWindow.className = "hero-settings-input"; pollingWindow.placeholder = "HH:MM-HH:MM";
        const pollingSnapshot = node("span", "", "Loading …");
        const pollingBootstrap = document.createElement("input"); pollingBootstrap.type = "checkbox"; pollingBootstrap.className = "hero-settings-check";
        const pollingPageSize = document.createElement("input"); pollingPageSize.type = "number"; pollingPageSize.min = "1"; pollingPageSize.max = "1000"; pollingPageSize.className = "hero-settings-input";
        const pollingPagination = document.createElement("select"); pollingPagination.className = "hero-settings-input"; ["OFFSET", "CURSOR"].forEach(value => { const option = document.createElement("option"); option.value = value; option.textContent = value; pollingPagination.append(option); });
        const pollingBackfill = document.createElement("input"); pollingBackfill.type = "checkbox"; pollingBackfill.className = "hero-settings-check";
        const pollingInventory = document.createElement("input"); pollingInventory.type = "checkbox"; pollingInventory.className = "hero-settings-check";
        const pollingReconciliation = document.createElement("input"); pollingReconciliation.type = "checkbox"; pollingReconciliation.className = "hero-settings-check";
        const pollingRetryLimit = document.createElement("input"); pollingRetryLimit.type = "number"; pollingRetryLimit.min = "0"; pollingRetryLimit.max = "3"; pollingRetryLimit.className = "hero-settings-input";
        const pollingBackoff = document.createElement("input"); pollingBackoff.type = "number"; pollingBackoff.min = "0"; pollingBackoff.max = "3600"; pollingBackoff.className = "hero-settings-input";
        const pollingTimeout = document.createElement("input"); pollingTimeout.type = "number"; pollingTimeout.min = "1"; pollingTimeout.max = "120"; pollingTimeout.className = "hero-settings-input";
        const pollingRate = document.createElement("input"); pollingRate.type = "number"; pollingRate.min = "1"; pollingRate.className = "hero-settings-input";
        const pollingConcurrency = document.createElement("input"); pollingConcurrency.type = "number"; pollingConcurrency.min = "1"; pollingConcurrency.max = "1"; pollingConcurrency.value = "1"; pollingConcurrency.className = "hero-settings-input";
        const pollingNotifications = document.createElement("input"); pollingNotifications.type = "checkbox"; pollingNotifications.className = "hero-settings-check";
        const testingEnabled = document.createElement("input"); testingEnabled.type = "checkbox"; testingEnabled.className = "hero-settings-check";
        const testingMaxActions = document.createElement("input"); testingMaxActions.type = "number"; testingMaxActions.min = "1"; testingMaxActions.max = "100"; testingMaxActions.step = "1"; testingMaxActions.className = "hero-settings-input";

        const token = document.createElement("input");
        token.type = "password";
        token.className = "hero-settings-input";
        token.placeholder = uiText("Leave blank to keep the existing token");
        token.autocomplete = "new-password";
        token.spellcheck = false;

        const schemaInterval = document.createElement("input");
        schemaInterval.type = "number";
        schemaInterval.className = "hero-settings-input";
        schemaInterval.min = "10";
        schemaInterval.max = "240";
        schemaInterval.step = "1";

        const timeout = document.createElement("input");
        timeout.type = "number";
        timeout.className = "hero-settings-input";
        timeout.min = "1";
        timeout.step = "1";

        const tokenState = node("span", "hero-settings-token-state", "Unknown");
        const status = node("div", "hero-page-note hero-settings-status", "Loading …");
        status.setAttribute("role", "status");
        const tokenHelp = column("hero-settings-field hero-settings-token-row", node("span", "hero-settings-label", "Stored credential"), tokenState);
        const schemaStatus = node("div", "hero-settings-status hero-settings-schema-state", "Schema status is not loaded yet.");
        schemaStatus.setAttribute("role", "status");
        // The last schema check is shown directly below its interval.
        const schemaRefreshField = field("Schema refresh (minutes)", schemaInterval, "10–240 minutes, once the token is verified.");
        schemaRefreshField.append(schemaStatus);

        const connection = section("HERO Connection", "connection", "",
            field("Enabled", enabled, "Enable or disable local HERO Integration configuration."),
            field("API URL", url, "HTTPS GraphQL endpoint used by the connector."),
            field("API Token", token, "The current token is never returned to the browser. An empty field keeps it unchanged."),
            tokenHelp,
            field("Timeout (seconds)", timeout, "Maximum duration of a HERO API request."),
            schemaRefreshField,
            node("p", "hero-settings-warning", "Changing the API URL, token or source instance opens a new source binding. Runs, MONITOR and AUTO_SYNC stay blocked until it is confirmed.")
        );
        const sources = section("Sources", "sources", "",
            field("Allowed hosts", allowedHosts, "One exact HTTPS hostname per line; the endpoint host must be present."),
            field("Source instance", sourceInstance, "Stable identity for this external source."),
            field("Confirm source binding", bindingConfirm, "Confirm that the endpoint and API token belong to the HERO account named by the source instance. The token is never used as an identity."),
            bindingStatus
        );
        const securitySection = section("Safety and conflict notifications", "safety", "",
            field("Allow deletion", allowDeletion, "OFF blocks every HERO Integration deletion route on the server. Turning it on is only a necessary prerequisite."),
        );

        const testingStatus = node("div", "hero-settings-status", "Testing settings are not loaded yet.");
        testingStatus.setAttribute("role", "status");
        const testingSave = node("button", "hero-page-button hero-settings-button", "Save Testing Settings");
        testingSave.type = "button";
        const testingCard = section("Testing", "testing", "",
            field("Enabled", testingEnabled),
            field("Max. Actions", testingMaxActions, "When enabled, manual synchronization previews are limited to the configured number of HERO API requests and start with the newest available records. No business data is modified."),
            column("hero-settings-actions", testingSave, testingStatus)
        );

        const saveBar = column("hero-settings-savebar", reset, save);

        const pollingCard = node("section", "hero-page-panel hero-settings-polling");
        const pollingDetails = document.createElement("details");
        pollingDetails.className = "hero-settings-polling-details";
        const pollingSummary = node("summary", "hero-settings-polling-summary");
        const pollingSummaryTitle = node("span", "hero-settings-polling-title", "Polling Settings");
        pollingSummary.append(pollingSummaryTitle);
        const pollingContent = node("div", "hero-settings-polling-content");
        pollingContent.append(
            pollingGroup("Cadence and scope",
                field("Polling mode", pollingMode, "OFF never polls HERO records automatically; the schema refresh is separate. MONITOR records internal run metadata only; AUTO_SYNC requires active approved HERO-to-Frappe mappings."),
                field("Polling interval (minutes)", pollingInterval, "1–1440 minutes; the Frappe scheduler evaluates cadence server-side."),
                field("Timezone", pollingTimezone), field("Execution window", pollingWindow, "Optional local time window in HH:MM-HH:MM.")),
            pollingGroup("Data sources",
                field("Validated snapshot", pollingSnapshot, "Always the newest validated snapshot; selected automatically."),
                field("Allow bootstrap", pollingBootstrap, "Stores permission for an initial load; the current polling reader does not perform bootstrap.")),
            pollingGroup("Processing",
                field("Page size", pollingPageSize), field("Pagination strategy", pollingPagination, "Cursor watermarks are advanced only after complete successful processing. Offset pagination does not invent watermarks."),
                field("Allow backfill", pollingBackfill, "Stores permission to fetch older records; the current polling reader does not perform backfill."), field("Allow full inventory", pollingInventory, "Allows a bounded scan of up to 100 pages per source instead of one page."), field("Allow reconciliation", pollingReconciliation, "Reconciliation reports discrepancies and never auto-deletes ERPNext records.")),
            pollingGroup("Reliability and notifications",
                field("Retry limit", pollingRetryLimit), field("Retry backoff seconds", pollingBackoff), field("Polling timeout seconds", pollingTimeout), field("Rate limit", pollingRate), field("Concurrency", pollingConcurrency, "The approved V1 source reader is intentionally single-worker."), field("Notifications", pollingNotifications))
        );
        pollingDetails.append(pollingSummary, pollingContent);
        pollingCard.append(pollingDetails);

        const mutationEnabled = document.createElement("input");
        mutationEnabled.type = "checkbox";
        mutationEnabled.className = "hero-settings-check";
        const mutationStatus = node("div", "hero-settings-status", "Loading …");
        mutationStatus.setAttribute("role", "status");
        const mutationSave = node("button", "hero-page-button hero-settings-button", "Apply mutation setting");
        mutationSave.type = "button";
        const mutationCard = section("Mutation Security", "mutations", "GraphQL Mutations remain disabled until the server-side security gates are satisfied.",
            field("Allow mutation execution", mutationEnabled, "Only System Managers can change this global gate. It is OFF by default."),
            column("hero-settings-aside",
                node("p", "", "An enabled global gate does not execute anything by itself: each mutation needs a compatible snapshot, an active per-operation contract, validated variables, preview, and explicit confirmation."),
                mutationStatus),
            column("hero-settings-actions", mutationSave)
        );

        // Complete configuration package: export here, import and activate on another site in one step.
        const packageStatus = node("div", "hero-settings-status", "");
        packageStatus.setAttribute("role", "status");
        const packageExport = node("button", "hero-page-button hero-settings-button", "Export configuration package");
        packageExport.type = "button";
        const packageFile = document.createElement("input");
        packageFile.type = "file";
        packageFile.accept = "application/json,.json";
        packageFile.className = "hero-settings-file";
        packageFile.setAttribute("aria-label", uiText("Configuration package file"));
        const packageCard = section("Configuration package", "package",
            "One file with the whole HERO setup: schema snapshot, settings, field mappings with their HERO fields and DocTypes, and the read services. Importing it on another site activates everything in one step.",
            column("hero-settings-aside",
                node("p", "", "Never included: HERO URL and API token, allowed hosts, notification recipients, the mutation and deletion switches, testing mode and data. The mutation switch stays off after an import.")),
            field("Import package", packageFile, "Choose an exported file. You see a preview and confirm once."),
            column("hero-settings-actions", packageExport, packageStatus)
        );

        root.append(header, status, connection, sources, securitySection, testingCard, saveBar, pollingCard, packageCard, mutationCard);
        workspace.replaceChildren(root);

        let destroyed = false, loaded = null;

        function busy(value) {
            refresh.disabled = value;
            save.disabled = value;
            reset.disabled = value;
            schemaInterval.disabled = value;
            enabled.disabled = value;
            url.disabled = value;
            token.disabled = value;
            timeout.disabled = value;
            allowedHosts.disabled = value;
            sourceInstance.disabled = value;
            bindingConfirm.disabled = value;
            allowDeletion.disabled = value;
            pollingMode.disabled = value;
            pollingInterval.disabled = value;
            [pollingTimezone, pollingWindow, pollingBootstrap, pollingPageSize, pollingPagination, pollingBackfill, pollingInventory, pollingReconciliation, pollingRetryLimit, pollingBackoff, pollingTimeout, pollingRate, pollingConcurrency, pollingNotifications].forEach((control) => { control.disabled = value; });
            mutationEnabled.disabled = value;
            mutationSave.disabled = value;
            testingEnabled.disabled = value;
            testingMaxActions.disabled = value;
            testingSave.disabled = value;
        }

        function apply(data) {
            loaded = data;
            enabled.checked = Boolean(data.enabled);
            url.value = data.api_url || "";
            timeout.value = String(data.timeout_seconds || 30);
            allowedHosts.value = data.allowed_hero_hosts || "";
            sourceInstance.value = data.hero_source_instance || "default";
            bindingConfirm.checked = false;
            bindingStatus.textContent = data.source_binding_confirmed
                ? format("Source binding confirmed (revision {0}).", data.source_binding_revision || 0)
                : uiText("Source binding changed and is not confirmed. HERO reads are blocked.");
            bindingStatus.dataset.state = data.source_binding_confirmed ? "success" : "error";
            schemaInterval.value = String(data.schema_refresh?.refresh_interval_minutes || 60);
            allowDeletion.checked = Boolean(data.allow_deletion);
            pollingMode.value = data.polling?.mode || "OFF";
            pollingInterval.value = String(data.polling?.interval_minutes || 60);
            pollingTimezone.value = data.polling?.timezone || "UTC";
            pollingWindow.value = data.polling?.execution_window || "";
            pollingSnapshot.textContent = data.polling?.snapshot?.display_name || uiText("No validated snapshot available");
            pollingBootstrap.checked = Boolean(data.polling?.bootstrap);
            pollingPageSize.value = String(data.polling?.page_size || 100);
            pollingPagination.value = data.polling?.pagination_strategy || "OFFSET";
            pollingBackfill.checked = Boolean(data.polling?.allow_backfill);
            pollingInventory.checked = Boolean(data.polling?.full_inventory);
            pollingReconciliation.checked = Boolean(data.polling?.reconciliation);
            pollingRetryLimit.value = String(data.polling?.retry_limit || 0);
            pollingBackoff.value = String(data.polling?.retry_backoff_seconds || 30);
            pollingTimeout.value = String(data.polling?.timeout_seconds || data.timeout_seconds || 30);
            pollingRate.value = String(data.polling?.rate_limit || 30);
            pollingConcurrency.value = String(data.polling?.concurrency || 1);
            pollingNotifications.checked = Boolean(data.polling?.notifications);
            testingEnabled.checked = Boolean(data.testing?.enabled);
            testingMaxActions.value = String(data.testing?.max_actions || 5);
            token.value = "";
            applyTokenState(data);
            applyMutationState(data);
        }

        function applyTokenState(data) {
            const schema = data.schema_refresh || {};
            const configured = data.api_token_status === "CONFIGURED";
            const failed = configured && schema.last_status === "FAILED";
            const verified = configured && schema.token_verified && !failed;
            tokenState.textContent = uiText(
                data.api_token_status === "CREDENTIAL_ERROR" ? "Credential error"
                    : !configured ? "Not configured"
                    : failed ? "Check failed"
                    : verified ? "Verified" : "Not verified yet");
            tokenState.dataset.state = verified ? "configured" : failed || data.api_token_status === "CREDENTIAL_ERROR" ? "error" : "missing";
            schemaStatus.textContent = schema.last_checked_on
                ? uiText("Last check:") + " " + String(schema.last_checked_on).slice(0, 16) + " – " + uiText(schema.last_message || "")
                : uiText("No check yet. Saving enabled settings with an API token loads the schema.");
            schemaStatus.dataset.state = schema.last_status === "OK" ? "success" : schema.last_status === "FAILED" ? "error" : "";
        }

        function applyMutationState(data) {
            mutationEnabled.checked = Boolean(data.mutation_execution_requested);
            mutationStatus.textContent = format("Effective mutation security status: {0}. {1}", data.mutation_execution_status || "DISABLED", uiText(data.mutation_execution_reason || ""));
        }

        async function load() {
            busy(true);
            status.textContent = uiText("Loading …");
            try {
                const data = await platform.api(GET);
                if (!destroyed) {
                    apply(data);
                    status.textContent = uiText("Settings loaded.");
                    status.dataset.state = "success";
                }
            } catch (error) {
                if (!destroyed) {
                    status.textContent = format("Could not load HERO settings. {0}", errorMessage(error));
                    status.dataset.state = "error";
                }
            } finally {
                if (!destroyed) busy(false);
            }
        }

        async function persist() {
            busy(true);
            status.textContent = uiText("Saving …");
            try {
                const data = await platform.api(SAVE, {
                    enabled: enabled.checked,
                    api_url: url.value.trim(),
                    api_token: token.value,
                    timeout_seconds: Number(timeout.value),
                    schema_refresh_interval_minutes: schemaInterval.value,
                    allowed_hero_hosts: allowedHosts.value.trim(),
                    hero_source_instance: sourceInstance.value.trim(),
                    confirm_source_binding: bindingConfirm.checked,
                    allow_deletion: allowDeletion.checked,
                    polling_mode: pollingMode.value,
                    polling_interval_minutes: Number(pollingInterval.value),
                    polling_timezone: pollingTimezone.value.trim(), polling_execution_window: pollingWindow.value.trim(),
                    polling_bootstrap: pollingBootstrap.checked,
                    polling_page_size: Number(pollingPageSize.value), polling_pagination_strategy: pollingPagination.value,
                    polling_allow_backfill: pollingBackfill.checked, polling_full_inventory: pollingInventory.checked, polling_reconciliation: pollingReconciliation.checked,
                    polling_retry_limit: Number(pollingRetryLimit.value), polling_retry_backoff_seconds: Number(pollingBackoff.value), polling_timeout_seconds: Number(pollingTimeout.value),
                    polling_rate_limit: Number(pollingRate.value), polling_concurrency: Number(pollingConcurrency.value), polling_notifications: pollingNotifications.checked,
                    testing_enabled: testingEnabled.checked, testing_max_actions: testingMaxActions.value
                }, {write: true});
                if (!destroyed) {
                    apply(data);
                    status.textContent = uiText("Settings saved.");
                    status.dataset.state = "success";
                    if (data.schema_refresh?.check_due) await verifyToken();
                }
            } catch (error) {
                if (!destroyed) {
                    status.textContent = format("Could not save HERO settings. {0}", errorMessage(error));
                    status.dataset.state = "error";
                }
            } finally {
                if (!destroyed) busy(false);
            }
        }

        async function persistTesting() {
            busy(true);
            testingStatus.textContent = uiText("Saving testing settings …");
            try {
                const data = await platform.api(SAVE_TESTING, {
                    testing_enabled: testingEnabled.checked,
                    testing_max_actions: testingMaxActions.value
                }, {write: true});
                if (!destroyed) {
                    apply(data);
                    testingStatus.textContent = uiText("Testing settings saved.");
                    testingStatus.dataset.state = "success";
                }
            } catch (error) {
                if (!destroyed) {
                    testingStatus.textContent = format("Could not save testing settings. {0}", errorMessage(error));
                    testingStatus.dataset.state = "error";
                }
            } finally {
                if (!destroyed) busy(false);
            }
        }

        // Saved, enabled settings with an unverified token load the schema; this also proves the token.
        async function verifyToken() {
            status.textContent = uiText("Settings saved. Loading the HERO schema to verify the API token …");
            status.dataset.state = "";
            let message, state;
            try {
                const result = await platform.api(REFRESH_SCHEMA, {}, {write: true});
                message = result.status === "UNCHANGED"
                    ? "Settings saved. API token is valid; the HERO schema is unchanged."
                    : "Settings saved. API token is valid; the HERO schema was loaded.";
                state = "success";
            } catch (error) {
                message = format("Settings saved, but the API token could not be verified. {0}", errorMessage(error));
                state = "error";
            }
            try {
                const data = await platform.api(GET);
                if (!destroyed) apply(data);
            } catch (_) { /* The status line below still reports the check result. */ }
            if (!destroyed) {
                status.textContent = uiText(message);
                status.dataset.state = state;
            }
        }

        function resetForm() {
            if (!loaded) return;
            apply(loaded);
            status.textContent = uiText("Unsaved changes discarded.");
            status.dataset.state = "";
        }

        async function persistMutationSetting() {
            busy(true);
            mutationStatus.textContent = uiText("Saving mutation setting …");
            try {
                const data = await platform.api(SET_MUTATION_EXECUTION, {allow_execution: mutationEnabled.checked});
                if (!destroyed) {
                    applyMutationState(data);
                }
            } catch (error) {
                if (!destroyed) {
                    mutationStatus.textContent = uiText("Mutation setting was not saved. The global gate remains fail-closed until the server accepts it.");
                    await load();
                }
            } finally {
                if (!destroyed) busy(false);
            }
        }

        refresh.addEventListener("click", load);
        save.addEventListener("click", persist);
        testingSave.addEventListener("click", persistTesting);

        const showPackageState = (text, state = "") => {
            packageStatus.textContent = text;
            packageStatus.dataset.state = state;
        };
        packageExport.addEventListener("click", async () => {
            showPackageState(uiText("Exporting configuration package …"));
            try {
                const result = await platform.api("config_export", {});
                const link = document.createElement("a");
                link.href = URL.createObjectURL(new Blob([result.content], {type: "application/json"}));
                link.download = result.filename;
                document.body.append(link); link.click(); link.remove();
                setTimeout(() => URL.revokeObjectURL(link.href), 1000);
                showPackageState(uiText("Configuration package exported."), "success");
            } catch (error) {
                showPackageState(`${uiText("Export failed.")} ${errorMessage(error)}`, "error");
            }
        });
        packageFile.addEventListener("change", async () => {
            const file = packageFile.files?.[0];
            packageFile.value = "";
            if (!file) return;
            showPackageState(uiText("Checking package …"));
            try {
                const payload = await file.text();
                const preview = await platform.api("config_import_preview", {payload_json: payload});
                const counts = preview.mapping_package
                    ? Object.entries(preview.mapping_package).filter(([, count]) => count).map(([state, count]) => `${state} ${count}`).join(", ")
                    : uiText("checked during import");
                const services = preview.read_services.map((row) => `${row.target_operation}: ${row.state}`).join(", ");
                const settings = preview.settings.length ? preview.settings.join(", ") : uiText("none");
                const summary = `${uiText("Schema snapshot")}: ${preview.snapshot}. ${uiText("Mappings")}: ${counts}. ` +
                    `${uiText("Read services")}: ${services}. ${uiText("Changed settings")}: ${settings}.`;
                showPackageState(summary);
                frappe.confirm(`${summary}<br><br>${uiText("Import and activate this configuration now?")}`, async () => {
                    showPackageState(uiText("Importing …"));
                    try {
                        const result = await platform.api("config_import", {payload_json: payload, confirmed: 1});
                        const approved = result.read_services.filter((row) => row.state !== "Unchanged").length;
                        showPackageState(`${uiText("Imported")}: ${uiText("snapshot")} ${result.snapshot}, ${result.adopted} ${uiText("mapping entries")}, ` +
                            `${result.settings.length} ${uiText("settings")}, ${approved} ${uiText("read services approved")}.`, "success");
                        load();
                    } catch (error) {
                        showPackageState(`${uiText("Import failed; the configuration was not activated.")} ${errorMessage(error)}`, "error");
                    }
                });
            } catch (error) {
                showPackageState(`${uiText("The file cannot be imported.")} ${errorMessage(error)}`, "error");
            }
        });
        reset.addEventListener("click", resetForm);
        mutationSave.addEventListener("click", persistMutationSetting);
        await load();

        return {
            destroy() {
                destroyed = true;
                refresh.replaceWith(refresh.cloneNode(true));
                save.replaceWith(save.cloneNode(true));
                reset.replaceWith(reset.cloneNode(true));
                root.remove();
            }
        };
    }

    window.hero_integration_settings_ui = Object.freeze({mount});
})();
