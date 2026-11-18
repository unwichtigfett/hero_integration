// One own-user design preference in the native Appearance dialog.
(() => {
    "use strict";
    const mode = frappe.boot?.hero_design_mode || "Custom";
    const native = mode === "Native";
    const customAssets = ["/hero_integration/css/navy_theme.css", "/hero_integration/css/site_logo.css",
        "/hero_integration/css/project_workspace.css"];
    for (const link of document.querySelectorAll('link[rel="stylesheet"]')) {
        if (customAssets.some(path => link.href.includes(path))) link.disabled = native;
    }
    if (native) {
        document.documentElement.removeAttribute("data-hero-theme");
        if (document.documentElement.getAttribute("data-theme-mode") === "navy") {
            document.documentElement.setAttribute("data-theme-mode", "dark");
            frappe.ui.set_theme("dark");
        }
    }
    if (frappe.ui.ThemeSwitcher.hero_design_switcher) return;
    frappe.ui.ThemeSwitcher = class DesignThemeSwitcher extends frappe.ui.ThemeSwitcher {
        static hero_design_switcher = true;

        setup_dialog() {
            super.setup_dialog();
            this.dialog.set_title?.(__("Appearance"));
            const section = document.createElement("section");
            section.className = "form-section";
            const label = document.createElement("label");
            label.textContent = __("Design");
            const select = document.createElement("select");
            select.className = "form-control";
            select.setAttribute("aria-label", __("Design"));
            for (const value of ["Native", "Custom"]) {
                const option = document.createElement("option");
                option.value = value; option.textContent = __(value, null, "Design mode");
                select.append(option);
            }
            select.value = mode;
            label.append(select); section.append(label);
            const hint = document.createElement("p");
            hint.className = "text-muted";
            hint.textContent = __("One package for the custom Desk and Project appearance. Data and permissions do not change.");
            section.append(hint);
            this.dialog.$body[0].insertBefore(section, this.body[0]);
            section.addEventListener("keydown", event => {
                if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) event.stopPropagation();
            });
            select.addEventListener("change", () => {
                const selected = select.value;
                if (selected === mode) return;
                if (window.cur_frm?.is_dirty()) {
                    select.value = mode;
                    frappe.msgprint(__("Save or discard open changes before switching designs."));
                    return;
                }
                select.disabled = true;
                const reset = () => { select.value = mode; select.disabled = false; };
                frappe.confirm(__("Apply this Design and reload the Desk? Save open work first."), async () => {
                    try {
                        const response = await frappe.call({
                            method: "hero_integration.services.design_mode.set_design_mode",
                            type: "POST", args: {mode: selected}, quiet: true,
                        });
                        if (response.exc || response.message?.mode !== selected) throw new Error("Invalid Design response");
                        const route = frappe.get_route?.();
                        if (selected === "Native" && route?.[0] === "Form" && route[1] === "Project"
                            && ["#custom_project_documents_tab", "#custom_project_images_tab"].includes(window.location.hash)) {
                            const url = new URL(window.location.href);
                            url.hash = "";
                            window.history.replaceState(null, "", url.href);
                        }
                        window.location.reload();
                    } catch (_) {
                        reset(); frappe.msgprint(__("The Design preference could not be saved."));
                    }
                }, reset);
            });
        }
    };
})();
