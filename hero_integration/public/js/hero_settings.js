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

    if (window.hero_integration_settings_ui) return;

    const platform = window.hero_integration_platform;
    const GET = "workspace_settings_get";
    const SAVE = "workspace_settings_save";
    const SAVE_TESTING = "workspace_testing_settings_save";
    const SAVE_COMPANY = "workspace_erpnext_company_save";
    const CONFIRM_COMPANY = "workspace_erpnext_company_confirm";
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
        save.title = uiText("Saves connection, sources, company, safety and polling settings.");
        const headerActions = node("div", "hero-page-header-actions");
        headerActions.append(refresh, save);
        header.append(heading, headerActions);

        const enabled = document.createElement("input");
        enabled.type = "checkbox";
        enabled.className = "hero-settings-check";

        const url = document.createElement("input");
        url.type = "url";
        url.className = "hero-settings-input";
        url.placeholder = uiText("https://…/graphql");
        url.autocomplete = "off";

        const allowedHosts = document.createElement("textarea");
        allowedHosts.className = "hero-settings-input";
        allowedHosts.rows = 2;
        const sourceInstance = document.createElement("input");
        sourceInstance.className = "hero-settings-input";
        const company = document.createElement("input");
        company.className = "hero-settings-input";
        const bindingConfirm = document.createElement("input");
        bindingConfirm.type = "checkbox";
        bindingConfirm.className = "hero-settings-check";
        const bindingStatus = node("div", "hero-settings-status", "Source binding is not loaded yet.");
        bindingStatus.setAttribute("role", "status");
        const companyConfirmed = document.createElement("input");
        companyConfirmed.type = "checkbox";
        companyConfirmed.className = "hero-settings-check";
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

        const timeout = document.createElement("input");
        timeout.type = "number";
        timeout.className = "hero-settings-input";
        timeout.min = "1";
        timeout.step = "1";

        const tokenState = node("span", "hero-settings-token-state", "Unknown");
        const status = node("div", "hero-page-note hero-settings-status", "Loading …");
        status.setAttribute("role", "status");
        const tokenHelp = column("hero-settings-field hero-settings-token-row", node("span", "hero-settings-label", "Stored credential"), tokenState);

        const connection = section("HERO Connection", "connection", "",
            field("Enabled", enabled, "Enable or disable local HERO Integration configuration."),
            field("API URL", url, "HTTPS GraphQL endpoint used by the connector."),
            field("API Token", token, "The current token is never returned to the browser. An empty field keeps it unchanged."),
            tokenHelp,
            field("Timeout (seconds)", timeout, "Maximum duration of a HERO API request."),
            node("p", "hero-settings-warning", "Changing the API URL, token or source instance opens a new source binding. Runs, MONITOR and AUTO_SYNC stay blocked until it is confirmed.")
        );
        const sources = section("Sources", "sources", "",
            field("Allowed hosts", allowedHosts, "One exact HTTPS hostname per line; the endpoint host must be present."),
            field("Source instance", sourceInstance, "Stable identity for this external source."),
            field("Confirm source binding", bindingConfirm, "Confirm that the endpoint and API token belong to the HERO account named by the source instance. The token is never used as an identity."),
            bindingStatus
        );
        const companyStatus = node("div", "hero-settings-status hero-settings-company-state", "Company selection is not loaded yet.");
        companyStatus.setAttribute("role", "status");
        const companySave = node("button", "hero-page-button hero-settings-button", "Save Company");
        companySave.type = "button";
        const companyConfirm = node("button", "hero-page-button hero-settings-button", "Confirm Company");
        companyConfirm.type = "button";
        const companySection = section("ERPNext Company", "company", "",
            field("ERPNext Company", company, "Existing ERPNext Company used for HERO projects."),
            field("Confirm ERPNext Company", companyConfirmed, "A company change clears this confirmation server-side."),
            column("hero-settings-field", node("span", "hero-settings-label", "Confirmed"), companyStatus),
            column("hero-settings-actions", companySave, companyConfirm)
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

        const pollingCard = node("section", "hero-page-panel hero-settings-polling");
        const pollingDetails = document.createElement("details");
        pollingDetails.className = "hero-settings-polling-details";
        const pollingSummary = node("summary", "hero-settings-polling-summary");
        const pollingSummaryTitle = node("span", "hero-settings-polling-title", "Polling Settings");
        pollingSummary.append(pollingSummaryTitle);
        const pollingContent = node("div", "hero-settings-polling-content");
        pollingContent.append(
            pollingGroup("Cadence and scope",
                field("Polling mode", pollingMode, "OFF never contacts HERO automatically. MONITOR records internal run metadata only; AUTO_SYNC requires active approved HERO-to-Frappe mappings."),
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

        root.append(header, status, connection, sources, companySection, securitySection, testingCard, pollingCard, mutationCard);
        workspace.replaceChildren(root);

        let destroyed = false, isBusy = false, storedCompany = "";

        function busy(value) {
            isBusy = value;
            refresh.disabled = value;
            save.disabled = value;
            enabled.disabled = value;
            url.disabled = value;
            token.disabled = value;
            timeout.disabled = value;
            allowedHosts.disabled = value;
            sourceInstance.disabled = value;
            bindingConfirm.disabled = value;
            company.disabled = value;
            companyConfirmed.disabled = true;
            allowDeletion.disabled = value;
            pollingMode.disabled = value;
            pollingInterval.disabled = value;
            [pollingTimezone, pollingWindow, pollingBootstrap, pollingPageSize, pollingPagination, pollingBackfill, pollingInventory, pollingReconciliation, pollingRetryLimit, pollingBackoff, pollingTimeout, pollingRate, pollingConcurrency, pollingNotifications].forEach((control) => { control.disabled = value; });
            mutationEnabled.disabled = value;
            mutationSave.disabled = value;
            testingEnabled.disabled = value;
            testingMaxActions.disabled = value;
            testingSave.disabled = value;
            companySave.disabled = value;
            updateCompanyControls();
        }

        function updateCompanyControls() {
            const selected = company.value.trim();
            const savedSelection = Boolean(storedCompany) && selected === storedCompany;
            companyConfirm.disabled = isBusy || !savedSelection;
            companyConfirm.title = savedSelection ? "Confirm the saved ERPNext Company." : "Save the selected ERPNext Company before confirming it.";
        }

        function apply(data) {
            enabled.checked = Boolean(data.enabled);
            url.value = data.api_url || "";
            timeout.value = String(data.timeout_seconds || 30);
            allowedHosts.value = data.allowed_hero_hosts || "";
            sourceInstance.value = data.hero_source_instance || "default";
            bindingConfirm.checked = false;
            bindingStatus.textContent = uiText(data.source_binding_confirmed
                ? "Source binding confirmed (revision " + (data.source_binding_revision || 0) + ")."
                : "Source binding changed and is not confirmed. HERO reads are blocked.");
            bindingStatus.dataset.state = data.source_binding_confirmed ? "success" : "error";
            storedCompany = data.erpnext_company || "";
            company.value = storedCompany;
            companyConfirmed.checked = Boolean(data.erpnext_company_confirmed);
            companyStatus.textContent = uiText(!storedCompany ? "Save an ERPNext Company before confirming it." : companyConfirmed.checked ? "Confirmed" : "Company saved. Confirmation required.");
            companyStatus.dataset.state = companyConfirmed.checked ? "success" : "";
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
            const configured = data.api_token_status === "CONFIGURED";
            tokenState.textContent = uiText(configured ? "Configured" : data.api_token_status === "CREDENTIAL_ERROR" ? "Credential error" : "Not configured");
            tokenState.dataset.state = configured ? "configured" : data.api_token_status === "CREDENTIAL_ERROR" ? "error" : "missing";
            applyMutationState(data);
            updateCompanyControls();
        }

        function applyMutationState(data) {
            mutationEnabled.checked = Boolean(data.mutation_execution_requested);
            mutationStatus.textContent = uiText(`Effective mutation security status: ${data.mutation_execution_status || "DISABLED"}. ${data.mutation_execution_reason || ""}`);
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
                    status.textContent = `Could not load HERO settings. ${errorMessage(error)}`;
                    status.dataset.state = "error";
                }
            } finally {
                if (!destroyed) busy(false);
            }
        }

        async function persist() {
            busy(true);
            status.textContent = uiText("Saving …");
            const companyBeforeSave = storedCompany;
            const requestedCompany = company.value.trim();
            try {
                const data = await platform.api(SAVE, {
                    enabled: enabled.checked,
                    api_url: url.value.trim(),
                    api_token: token.value,
                    timeout_seconds: Number(timeout.value),
                    allowed_hero_hosts: allowedHosts.value.trim(),
                    hero_source_instance: sourceInstance.value.trim(),
                    confirm_source_binding: bindingConfirm.checked,
                    erpnext_company: company.value.trim(),
                    erpnext_company_confirmed: companyConfirmed.checked,
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
                    if (requestedCompany !== companyBeforeSave && !data.erpnext_company_confirmed) {
                        companyStatus.textContent = uiText("Company saved. Confirmation was reset because the company changed.");
                        companyStatus.dataset.state = "";
                    }
                }
            } catch (error) {
                if (!destroyed) {
                    status.textContent = `Could not save HERO settings. ${errorMessage(error)}`;
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
                    testingStatus.textContent = `Could not save testing settings. ${errorMessage(error)}`;
                    testingStatus.dataset.state = "error";
                }
            } finally {
                if (!destroyed) busy(false);
            }
        }

        async function persistCompany() {
            busy(true);
            companyStatus.textContent = uiText("Saving company …");
            try {
                const data = await platform.api(SAVE_COMPANY, {erpnext_company: company.value.trim()}, {write: true});
                if (!destroyed) {
                    apply(data);
                    companyStatus.textContent = uiText(data.erpnext_company_confirmed ? "Confirmed" : "Company saved. Confirmation required.");
                    companyStatus.dataset.state = data.erpnext_company_confirmed ? "success" : "";
                }
            } catch (error) {
                if (!destroyed) {
                    companyStatus.textContent = `Could not save ERPNext Company. ${errorMessage(error)}`;
                    companyStatus.dataset.state = "error";
                }
            } finally {
                if (!destroyed) busy(false);
            }
        }

        async function confirmCompany() {
            if (company.value.trim() !== storedCompany || !storedCompany) {
                companyStatus.textContent = uiText("Save the selected ERPNext Company before confirming it.");
                companyStatus.dataset.state = "error";
                return;
            }
            busy(true);
            companyStatus.textContent = uiText("Confirming company …");
            try {
                const data = await platform.api(CONFIRM_COMPANY, {erpnext_company: company.value.trim()}, {write: true});
                if (!data.erpnext_company_confirmed) throw new Error("The server did not confirm the selected ERPNext Company.");
                if (!destroyed) {
                    apply(data);
                    companyStatus.textContent = uiText("Confirmed");
                    companyStatus.dataset.state = "success";
                }
            } catch (error) {
                if (!destroyed) {
                    companyStatus.textContent = `Could not confirm ERPNext Company. ${errorMessage(error)}`;
                    companyStatus.dataset.state = "error";
                }
            } finally {
                if (!destroyed) busy(false);
            }
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
        companySave.addEventListener("click", persistCompany);
        companyConfirm.addEventListener("click", confirmCompany);
        company.addEventListener("input", updateCompanyControls);
        mutationSave.addEventListener("click", persistMutationSetting);
        await load();

        return {
            destroy() {
                destroyed = true;
                refresh.replaceWith(refresh.cloneNode(true));
                save.replaceWith(save.cloneNode(true));
                root.remove();
            }
        };
    }

    window.hero_integration_settings_ui = Object.freeze({mount});
})();
