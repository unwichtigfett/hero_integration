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

    async function mount(workspace) {
        const root = node("section", "hero-settings");
        const header = node("div", "hero-settings-header");
        const heading = node("div");
        heading.append(
            node("h2", "", "HERO Settings"),
            node("p", "", "Configure the connection stored by HERO Integration.")
        );
        const refresh = node("button", "hero-settings-button secondary", "Refresh");
        refresh.type = "button";
        header.append(heading, refresh);

        const card = node("div", "hero-settings-card");
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
        const companyConfirmed = document.createElement("input");
        companyConfirmed.type = "checkbox";
        companyConfirmed.className = "hero-settings-check";
        const pollingMode = document.createElement("select");
        pollingMode.className = "hero-settings-input";
        ["OFF", "MONITOR", "AUTO_SYNC"].forEach(value => {
            const option = document.createElement("option"); option.value = value; option.textContent = value; pollingMode.append(option);
        });
        const pollingInterval = document.createElement("input");
        pollingInterval.type = "number"; pollingInterval.min = "1"; pollingInterval.max = "1440"; pollingInterval.className = "hero-settings-input";
        const pollingTimezone = document.createElement("input"); pollingTimezone.className = "hero-settings-input";
        const pollingWindow = document.createElement("input"); pollingWindow.className = "hero-settings-input"; pollingWindow.placeholder = "HH:MM-HH:MM";
        const pollingSources = document.createElement("textarea"); pollingSources.className = "hero-settings-input"; pollingSources.rows = 3;
        const pollingFilters = document.createElement("textarea"); pollingFilters.className = "hero-settings-input"; pollingFilters.rows = 4; pollingFilters.spellcheck = false;
        const pollingBootstrap = document.createElement("input"); pollingBootstrap.type = "checkbox"; pollingBootstrap.className = "hero-settings-check";
        const pollingPageSize = document.createElement("input"); pollingPageSize.type = "number"; pollingPageSize.min = "1"; pollingPageSize.max = "1000"; pollingPageSize.className = "hero-settings-input";
        const pollingPagination = document.createElement("select"); pollingPagination.className = "hero-settings-input"; ["OFFSET", "CURSOR"].forEach(value => { const option = document.createElement("option"); option.value = value; option.textContent = value; pollingPagination.append(option); });
        const pollingBackfill = document.createElement("input"); pollingBackfill.type = "checkbox"; pollingBackfill.className = "hero-settings-check";
        const pollingInventory = document.createElement("input"); pollingInventory.type = "checkbox"; pollingInventory.className = "hero-settings-check";
        const pollingReconciliation = document.createElement("input"); pollingReconciliation.type = "checkbox"; pollingReconciliation.className = "hero-settings-check";
        const pollingRetryLimit = document.createElement("input"); pollingRetryLimit.type = "number"; pollingRetryLimit.min = "0"; pollingRetryLimit.max = "10"; pollingRetryLimit.className = "hero-settings-input";
        const pollingBackoff = document.createElement("input"); pollingBackoff.type = "number"; pollingBackoff.min = "0"; pollingBackoff.max = "3600"; pollingBackoff.className = "hero-settings-input";
        const pollingTimeout = document.createElement("input"); pollingTimeout.type = "number"; pollingTimeout.min = "1"; pollingTimeout.max = "600"; pollingTimeout.className = "hero-settings-input";
        const pollingRate = document.createElement("input"); pollingRate.type = "number"; pollingRate.min = "1"; pollingRate.className = "hero-settings-input";
        const pollingConcurrency = document.createElement("input"); pollingConcurrency.type = "number"; pollingConcurrency.min = "1"; pollingConcurrency.max = "10"; pollingConcurrency.className = "hero-settings-input";
        const pollingNotifications = document.createElement("input"); pollingNotifications.type = "checkbox"; pollingNotifications.className = "hero-settings-check";

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
        const status = node("div", "hero-settings-status", "Loading …");
        status.setAttribute("role", "status");

        const tokenHelp = node("span", "hero-settings-token-row");
        tokenHelp.append(node("span", "", "Stored credential:"), tokenState);

        const mutationCard = node("section", "hero-settings-card hero-settings-mutations");
        const mutationEnabled = document.createElement("input");
        mutationEnabled.type = "checkbox";
        mutationEnabled.className = "hero-settings-check";
        const mutationStatus = node("div", "hero-settings-status", "Loading …");
        mutationStatus.setAttribute("role", "status");
        const mutationSave = node("button", "hero-settings-button secondary", "Apply mutation setting");
        mutationSave.type = "button";
        mutationCard.append(
            node("h3", "", "GraphQL Mutations"),
            field("Allow mutation execution", mutationEnabled, "Only System Managers can change this global gate. It is OFF by default."),
            node("p", "hero-settings-warning", "An enabled global gate does not execute anything by itself: each mutation needs a compatible snapshot, an active per-operation contract, validated variables, preview, and explicit confirmation."),
            mutationStatus,
            mutationSave
        );

        const save = node("button", "hero-settings-button primary", "Save Settings");
        save.type = "button";
        const pollingCard = node("section", "hero-settings-card hero-settings-polling");
        pollingCard.append(
            node("h3", "", "Polling controls"),
            field("Polling mode", pollingMode, "OFF never contacts HERO automatically. MONITOR records internal run metadata only; AUTO_SYNC requires active idempotent mappings."),
            field("Polling interval (minutes)", pollingInterval, "1–1440 minutes; the Frappe scheduler evaluates cadence server-side."),
            field("Timezone", pollingTimezone), field("Execution window", pollingWindow, "Optional local time window in HH:MM-HH:MM."),
            field("Validated snapshot data sources", pollingSources, "One Query-root operation per line. Empty input is rejected."), field("Operation filters JSON", pollingFilters, "JSON object only; this stores no secrets and does not contact HERO."),
            field("Allow bootstrap", pollingBootstrap), field("Page size", pollingPageSize), field("Pagination strategy", pollingPagination, "Cursor watermarks are advanced only after complete successful processing. Offset pagination does not invent watermarks."),
            field("Allow backfill", pollingBackfill), field("Allow full inventory", pollingInventory), field("Allow reconciliation", pollingReconciliation, "Reconciliation reports discrepancies and never auto-deletes ERPNext records."),
            field("Retry limit", pollingRetryLimit), field("Retry backoff seconds", pollingBackoff), field("Polling timeout seconds", pollingTimeout), field("Rate limit", pollingRate), field("Concurrency", pollingConcurrency), field("Notifications", pollingNotifications)
        );

        card.append(
            field("Enabled", enabled, "Enable or disable local HERO Integration configuration."),
            field("API URL", url, "HTTPS GraphQL endpoint used by the connector."),
            field("Allowed HERO hosts", allowedHosts, "One exact HTTPS hostname per line; the endpoint host must be present."),
            field("HERO source instance", sourceInstance, "Stable identity for this external source."),
            field("API Token", token, "The current token is never returned to the browser. An empty field keeps it unchanged."),
            tokenHelp,
            field("Timeout (seconds)", timeout, "Maximum duration of a HERO API request."),
            field("ERPNext Company", company, "Existing ERPNext Company used for HERO projects."),
            field("Confirm ERPNext Company", companyConfirmed, "A company change clears this confirmation server-side."),
            node("p", "hero-settings-warning", "Changing the API URL or token rotates the HERO binding generation and clears the current schema binding."),
            status,
            save
        );

        root.append(header, card, pollingCard, mutationCard);
        workspace.replaceChildren(root);

        let destroyed = false;

        function busy(value) {
            refresh.disabled = value;
            save.disabled = value;
            enabled.disabled = value;
            url.disabled = value;
            token.disabled = value;
            timeout.disabled = value;
            allowedHosts.disabled = value;
            sourceInstance.disabled = value;
            company.disabled = value;
            companyConfirmed.disabled = value;
            pollingMode.disabled = value;
            pollingInterval.disabled = value;
            [pollingTimezone, pollingWindow, pollingSources, pollingFilters, pollingBootstrap, pollingPageSize, pollingPagination, pollingBackfill, pollingInventory, pollingReconciliation, pollingRetryLimit, pollingBackoff, pollingTimeout, pollingRate, pollingConcurrency, pollingNotifications].forEach((control) => { control.disabled = value; });
            mutationEnabled.disabled = value;
            mutationSave.disabled = value;
        }

        function apply(data) {
            enabled.checked = Boolean(data.enabled);
            url.value = data.api_url || "";
            timeout.value = String(data.timeout_seconds || 30);
            allowedHosts.value = data.allowed_hero_hosts || "";
            sourceInstance.value = data.hero_source_instance || "default";
            company.value = data.erpnext_company || "";
            companyConfirmed.checked = Boolean(data.erpnext_company_confirmed);
            pollingMode.value = data.polling?.mode || "OFF";
            pollingInterval.value = String(data.polling?.interval_minutes || 60);
            pollingTimezone.value = data.polling?.timezone || "UTC";
            pollingWindow.value = data.polling?.execution_window || "";
            pollingSources.value = (data.polling?.data_sources || []).join("\n");
            pollingFilters.value = JSON.stringify(data.polling?.filters || {}, null, 2);
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
            token.value = "";
            const configured = data.api_token_status === "CONFIGURED";
            tokenState.textContent = uiText(configured ? "Configured" : data.api_token_status === "CREDENTIAL_ERROR" ? "Credential error" : "Not configured");
            tokenState.dataset.state = configured ? "configured" : data.api_token_status === "CREDENTIAL_ERROR" ? "error" : "missing";
            applyMutationState(data);
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
                }
            } catch (error) {
                if (!destroyed) status.textContent = uiText("Failed to load HERO settings.");
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
                    allowed_hero_hosts: allowedHosts.value.trim(),
                    hero_source_instance: sourceInstance.value.trim(),
                    erpnext_company: company.value.trim(),
                    erpnext_company_confirmed: companyConfirmed.checked,
                    polling_mode: pollingMode.value,
                    polling_interval_minutes: Number(pollingInterval.value),
                    polling_timezone: pollingTimezone.value.trim(), polling_execution_window: pollingWindow.value.trim(),
                    polling_data_sources: pollingSources.value.trim(), polling_filters_json: pollingFilters.value.trim(), polling_bootstrap: pollingBootstrap.checked,
                    polling_page_size: Number(pollingPageSize.value), polling_pagination_strategy: pollingPagination.value,
                    polling_allow_backfill: pollingBackfill.checked, polling_full_inventory: pollingInventory.checked, polling_reconciliation: pollingReconciliation.checked,
                    polling_retry_limit: Number(pollingRetryLimit.value), polling_retry_backoff_seconds: Number(pollingBackoff.value), polling_timeout_seconds: Number(pollingTimeout.value),
                    polling_rate_limit: Number(pollingRate.value), polling_concurrency: Number(pollingConcurrency.value), polling_notifications: pollingNotifications.checked
                }, {write: true});
                if (!destroyed) {
                    apply(data);
                    status.textContent = uiText("Settings saved.");
                }
            } catch (error) {
                if (!destroyed) status.textContent = uiText("Failed to save HERO settings.");
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
