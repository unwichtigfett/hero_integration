(() => {
    "use strict";

    frappe.pages["hero-integration"].on_page_load = (wrapper) => {
        const start = () => window.hero_integration_ui.mount(wrapper);
        if (window.hero_integration_ui) start();
        else frappe.require("/assets/hero_integration/js/hero_workspace.js", start);
    };
    frappe.pages["hero-integration"].on_page_show = () => window.hero_integration_ui?.showRoute();
})();
