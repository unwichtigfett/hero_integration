(() => {
    "use strict";

    frappe.pages["hero-graph-viewer"].on_page_load = (wrapper) => {
        const start = () => window.hero_integration_ui.graphViewer(wrapper);
        if (window.hero_integration_ui) start();
        else frappe.require("/assets/hero_integration/js/hero_workspace.js", start);
    };
})();
