(() => {
    "use strict";

    if (window.hero_integration_migration) return;

    function mount(page) {
        page.main.empty().addClass("hero-integration-ui");
        const actions = ["Customer Migration", "Project Migration", "Cheap Data Migration", "Full Data Migration", "Sync"];
        actions.forEach((action) => page.add_menu_item(__(action + " Preview"), () => start(action, 1)));
        actions.forEach((action) => page.add_action_item(__(action), () => confirmStart(action)));
        page.main.append($("<section class='hero-migration-center'><h2>HERO Migration / Synchronisation</h2><p class='text-muted'>Customer migration always completes before Project migration. Execute actions require an explicit confirmation.</p></section>"));

        function confirmStart(action) {
            frappe.confirm(__("Start {0}? It runs in the background and uses normal target validation.", [action]), () => start(action, 0));
        }
        function start(action, dryRun) {
            frappe.call({method: "hero_integration.api.start_migration", args: {action, dry_run: dryRun}}).then((response) => {
                if (response.message.run) frappe.set_route("Form", "HERO Integration Run", response.message.run);
                else frappe.msgprint(__(response.message.detail || response.message.status));
            });
        }
    }

    window.hero_integration_migration = Object.freeze({mount});
})();
