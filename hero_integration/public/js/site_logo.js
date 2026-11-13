(() => {
    "use strict";

    // The top-left mark of the Desk is the site's own logo, with the app's name beside it so the
    // replaced app mark is still named. The sidebar's module menu moves one row down, level with
    // the rail's search button. Apps keep their own marks on the apps screen and in menus.
    const ui = frappe.ui;
    const logo = frappe.boot && frappe.boot.hero_site_logo;
    if (!logo || ui.hero_site_logo) return;
    ui.hero_site_logo = true;

    const mark = () => `<img src="${frappe.utils.escape_html(logo)}" alt="" />`;

    // The rail's search and notification buttons sit beside the app's name while the rail owns
    // them; the nodes are moved, so their handlers and tooltips stay. With them gone the rail's
    // first module tile rises level with the module menu.
    const place_tools = (dock, row_stays = true) => {
        const node = dock && dock.$dock && dock.$dock[0];
        const rail = node && node.querySelector(".dock-utilities");
        if (!rail) return;
        const tools = document.querySelector(".body-sidebar .hero-brand-tools");
        const beside = row_stays && !!tools && !!dock.enabled && node.classList.contains("dock-owns-utilities");
        const [from, to] = beside ? [rail, tools] : [tools, rail];
        while (from && from.firstChild) to.appendChild(from.firstChild);
        node.classList.toggle("hero-tools-beside-name", beside);
    };

    if (ui.Dock) {
        // Whether the rail owns the buttons is decided here, after the sidebar header exists.
        const apply_pin = ui.Dock.prototype.apply_pin;
        ui.Dock.prototype.apply_pin = function (...args) {
            const result = apply_pin.apply(this, args);
            place_tools(this);
            return result;
        };
        const render = ui.Dock.prototype.render_logo;
        ui.Dock.prototype.render_logo = function (...args) {
            const result = render.apply(this, args);
            if (this.$header_logo) this.$header_logo.html(mark());
            return result;
        };
    }

    // One row above the sidebar header: the app's name, led by the logo where no rail shows it.
    const brand = (header) => {
        const node = header.wrapper && header.wrapper[0];
        if (!node || !node.parentNode) return;
        const panel = node.parentNode;
        let row = panel.querySelector(".hero-brand-row");
        const app = frappe.utils.app_logo(header.sidebar.get_sidebar_app());
        if (!app) {
            if (row) {
                // Hand the buttons back to the rail before the row goes.
                place_tools(header.sidebar.dock, false);
                row.remove();
            }
            return;
        }
        if (!row) {
            row = document.createElement("div");
            row.className = "hero-brand-row";
            row.innerHTML = '<span class="hero-brand-logo"></span><span class="hero-brand-name"></span>'
                + '<span class="hero-brand-tools"></span>';
        }
        panel.insertBefore(row, node);
        row.querySelector(".hero-brand-logo").innerHTML = header.sidebar.dock_enabled() ? "" : mark();
        row.querySelector(".hero-brand-name").textContent = app.title;
        place_tools(header.sidebar.dock);
    };

    if (ui.SidebarHeader) {
        for (const method of ["make", "refresh"]) {
            const original = ui.SidebarHeader.prototype[method];
            ui.SidebarHeader.prototype[method] = function (...args) {
                const result = original.apply(this, args);
                brand(this);
                return result;
            };
        }
    }
})();
