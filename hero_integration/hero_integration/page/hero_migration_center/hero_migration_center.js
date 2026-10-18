frappe.pages["hero-migration-center"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({parent: wrapper, title: __("HERO Migration Center"), single_column: true});
    const actions = ["Customer Migration", "Project Migration", "Cheap Data Migration", "Full Data Migration", "Sync"];
    actions.forEach((action) => page.add_menu_item(__(action + " Preview"), () => start(action, 1)));
    actions.forEach((action) => page.add_action_item(__(action), () => confirm_start(action)));
    function confirm_start(action) {
        frappe.confirm(__("Start {0}? It runs in the background and uses normal target validation.", [action]), () => start(action, 0));
    }
    function start(action, dry_run) {
        frappe.call({method: "hero_integration.api.start_migration", args: {action, dry_run}}).then((response) => {
            if (response.message.run) {
                frappe.set_route("Form", "HERO Integration Run", response.message.run);
            } else {
                frappe.msgprint(__(response.message.detail || response.message.status));
            }
        });
    }
};
