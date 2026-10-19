(() => {
    "use strict";

    frappe.pages["hero-migration-center"].on_page_load = () => {
        const start = () => window.hero_integration_ui.redirectLegacy("migration");
        if (window.hero_integration_ui) start();
        else frappe.require("/assets/hero_integration/js/hero_workspace.js", start);
    };
})();
