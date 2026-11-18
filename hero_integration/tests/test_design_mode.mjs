import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import test from "node:test";

const source = readFileSync(new URL("../public/js/design_mode.js", import.meta.url), "utf8");
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.events = {}; }
    append(...children) { this.children.push(...children); }
    insertBefore(child, before) { this.children.splice(this.children.indexOf(before), 0, child); }
    setAttribute(key, value) { this.attributes[key] = value; }
    getAttribute(key) { return this.attributes[key]; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(name, callback) { this.events[name] = callback; }
    find(tag) { if (this.tag === tag) return this; for (const child of this.children) { const found = child.find(tag); if (found) return found; } }
}
function setup(mode = "Custom", dirty = false, fail = false) {
    const root = new Element("html"), links = [
        {href: "/assets/frappe/css/desk.css"}, {href: "/assets/hero_integration/css/navy_theme.css"},
        {href: "/assets/hero_integration/css/site_logo.css"}];
    root.setAttribute("data-theme-mode", "navy");
    class ThemeSwitcher {
        constructor() { this.setup_dialog(); }
        setup_dialog() {
            const body = new Element("body"), grid = new Element("div"); body.append(grid);
            this.dialog = {$body: [body], set_title: value => { this.title = value; }};
            this.body = [grid];
        }
    }
    const calls = [], messages = []; let confirmation, reloads = 0;
    const frappe = {
        boot: {hero_design_mode: mode}, ui: {ThemeSwitcher, set_theme: () => {}},
        msgprint: value => messages.push(value),
        confirm: (text, yes, no) => { confirmation = {text, yes, no}; },
        call: async request => { calls.push(request); if (fail) throw Error("request failed"); return {message: {mode: request.args.mode}}; },
    };
    const window = {cur_frm: {is_dirty: () => dirty}, location: {reload: () => { reloads++; }}};
    vm.runInNewContext(source, {frappe, window,
        __: value => ({Native: "Nativ", Custom: "Angepasst"}[value] || value),
        document: {documentElement: root, querySelectorAll: () => links, createElement: tag => new Element(tag)}});
    const switcher = new frappe.ui.ThemeSwitcher();
    const select = switcher.dialog.$body[0].find("select");
    return {frappe, select, links, messages, calls, confirmation: () => confirmation, reloads: () => reloads};
}
test("Appearance contains exactly Native and Custom as one Design package", () => {
    const app = setup();
    assert.deepEqual(app.select.children.map(option => option.value), ["Native", "Custom"]);
    assert.deepEqual(app.select.children.map(option => option.textContent), ["Nativ", "Angepasst"]);
    assert.equal(app.select.value, "Custom");
});
test("Native disables only custom appearance assets, never core Desk styles", () => {
    const app = setup("Native");
    assert.equal(app.links[0].disabled, undefined);
    assert.equal(app.links[1].disabled, true); assert.equal(app.links[2].disabled, true);
});
test("design switching is own-user POST plus explicit reload confirmation", async () => {
    const app = setup();
    app.select.value = "Native"; app.select.events.change();
    assert.equal(app.calls.length, 0);
    await app.confirmation().yes();
    assert.equal(app.calls[0].type, "POST");
    assert.equal(app.calls[0].args.mode, "Native");
    assert.equal(Object.hasOwn(app.calls[0].args, "user"), false);
    assert.equal(app.reloads(), 1);
});
test("dirty forms block saving/reloading the design preference", () => {
    const app = setup("Custom", true);
    app.select.value = "Native"; app.select.events.change();
    assert.equal(app.select.value, "Custom");
    assert.equal(app.calls.length, 0); assert.equal(app.reloads(), 0);
    assert.match(app.messages[0], /Save or discard/);
});
test("cancel and failed save preserve the current appearance", async () => {
    const app = setup();
    app.select.value = "Native"; app.select.events.change(); app.confirmation().no();
    assert.equal(app.select.value, "Custom"); assert.equal(app.calls.length, 0);
    const failed = setup("Custom", false, true);
    failed.select.value = "Native"; failed.select.events.change(); await failed.confirmation().yes();
    assert.equal(failed.select.value, "Custom"); assert.equal(failed.reloads(), 0);
});
