(() => {
    "use strict";

    // Helpdesk and CRM only know light, dark and system. Navy is added to their theme choice in
    // Settings > Preferences: it keeps their dark behavior and changes colors only. Their own
    // themes stay exactly as they are, and choosing one of them ends Navy.
    const root = document.documentElement;
    if (root.hasAttribute("data-hero-spa")) return;
    root.setAttribute("data-hero-spa", "1");

    const KEY = "hero-theme";
    const PAGE_KEY = "theme";
    const read = (key) => { try { return window.localStorage.getItem(key); } catch (error) { return null; } };
    const write = (key, value) => { try { window.localStorage.setItem(key, value); } catch (error) { /* private mode */ } };
    const page_theme = () => {
        const saved = read(PAGE_KEY);
        if (saved === "dark" || saved === "light") return saved;
        const system_dark = saved === "system" && window.matchMedia
            && window.matchMedia("(prefers-color-scheme: dark)").matches;
        return system_dark ? "dark" : "light";
    };

    let navy = read(KEY) === "navy";
    let page_choice = read(PAGE_KEY);

    const apply = () => {
        if (navy) {
            root.setAttribute("data-hero-theme", "navy");
            if (root.getAttribute("data-theme") !== "dark") root.setAttribute("data-theme", "dark");
        } else if (root.hasAttribute("data-hero-theme")) {
            root.removeAttribute("data-hero-theme");
            root.setAttribute("data-theme", page_theme());
        }
    };

    const set_navy = (value) => {
        navy = value;
        page_choice = read(PAGE_KEY);
        write(KEY, navy ? "navy" : "standard");
        apply();
    };

    // The page restores its saved theme while loading; that must not end Navy. A new saved
    // value means the user chose light or dark in the page itself, and that choice wins.
    const follow_page_choice = () => {
        if (navy && read(PAGE_KEY) !== page_choice) set_navy(false);
    };

    new MutationObserver(() => {
        if (!navy) return;
        apply();
        window.setTimeout(follow_page_choice, 80);
    }).observe(root, {attributes: true, attributeFilter: ["data-theme"]});

    // Helpdesk and CRM choose the theme in Settings > Preferences from a row of cards (light,
    // dark, system). Navy joins that row as one more card, built from their dark card.
    const CARD = ".flex-1.rounded-6.border.cursor-pointer";
    const theme_cards = () => {
        for (const row of document.querySelectorAll("div.flex.gap-3")) {
            const own = Array.from(row.children).filter((child) => !child.hasAttribute("data-hero-navy-card"));
            if (own.length === 3 && own.every((child) => child.matches(CARD))) return {row, own};
        }
        return null;
    };

    const add_card = () => {
        const found = theme_cards();
        if (!found || found.row.querySelector("[data-hero-navy-card]")) return;
        const {row, own} = found;
        const card = own[1].cloneNode(true);
        card.setAttribute("data-hero-navy-card", "");
        card.setAttribute("role", "radio");
        card.setAttribute("tabindex", "0");
        const footer = card.lastElementChild;
        if (footer && footer.firstElementChild) footer.firstElementChild.textContent = "Navy";
        row.setAttribute("data-hero-theme-cards", "");
        row.appendChild(card);
        const choose = () => set_navy(true);
        card.addEventListener("click", choose);
        card.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); }
        });
        // Picking one of the page's own themes ends Navy, also when it is the one already saved.
        for (const native of own) {
            native.addEventListener("click", () => window.setTimeout(() => { if (navy) set_navy(false); }, 0));
        }
    };

    let scheduled = false;
    const watch_settings = () => {
        add_card();
        new MutationObserver(() => {
            if (scheduled) return;
            scheduled = true;
            window.setTimeout(() => { scheduled = false; add_card(); }, 50);
        }).observe(document.body, {childList: true, subtree: true});
    };

    apply();
    if (document.body) watch_settings();
    else document.addEventListener("DOMContentLoaded", watch_settings);
})();
