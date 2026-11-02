(() => {
    "use strict";

    const ui = frappe.ui;
    if (ui.ThemeSwitcher.hero_navy_theme) return;

    const setTheme = ui.set_theme;
    ui.set_theme = (theme) => {
        const root = document.documentElement;
        const navy = (theme || root.getAttribute("data-theme-mode")) === "navy";
        if (navy) root.setAttribute("data-hero-theme", "navy");
        else root.removeAttribute("data-hero-theme");
        // Keep Frappe's dark component behavior; Navy changes its color tokens.
        return setTheme(navy ? "dark" : theme);
    };

    ui.ThemeSwitcher = class NavyThemeSwitcher extends ui.ThemeSwitcher {
        static hero_navy_theme = true;

        fetch_themes() {
            return super.fetch_themes().then(() => {
                this.themes.push({name: "navy", label: __("Navy"), info: __("GitHub-style navy colors")});
                return this.themes;
            });
        }
    };

    ui.set_theme();
})();
