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

        const save = node("button", "hero-settings-button primary", "Save Settings");
        save.type = "button";

        card.append(
            field("Enabled", enabled, "Enable or disable local HERO Integration configuration."),
            field("API URL", url, "HTTPS GraphQL endpoint used by the connector."),
            field("API Token", token, "The current token is never returned to the browser. An empty field keeps it unchanged."),
            tokenHelp,
            field("Timeout (seconds)", timeout, "Maximum duration of a HERO API request."),
            node("p", "hero-settings-warning", "Changing the API URL or token rotates the HERO binding generation and clears the current schema binding."),
            status,
            save
        );

        root.append(header, card);
        workspace.replaceChildren(root);

        let destroyed = false;

        function busy(value) {
            refresh.disabled = value;
            save.disabled = value;
            enabled.disabled = value;
            url.disabled = value;
            token.disabled = value;
            timeout.disabled = value;
        }

        function apply(data) {
            enabled.checked = Boolean(data.enabled);
            url.value = data.api_url || "";
            timeout.value = String(data.timeout_seconds || 30);
            token.value = "";
            const configured = data.api_token_status === "CONFIGURED";
            tokenState.textContent = uiText(configured ? "Configured" : data.api_token_status === "CREDENTIAL_ERROR" ? "Credential error" : "Not configured");
            tokenState.dataset.state = configured ? "configured" : data.api_token_status === "CREDENTIAL_ERROR" ? "error" : "missing";
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
                    timeout_seconds: Number(timeout.value)
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

        refresh.addEventListener("click", load);
        save.addEventListener("click", persist);
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
