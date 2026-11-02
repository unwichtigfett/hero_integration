import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const extension = readFileSync(new URL('../public/js/navy_theme.js', import.meta.url), 'utf8');
const frameworkPath = process.env.FRAPPE_THEME_SWITCHER || new URL('../../../frappe/frappe/public/js/frappe/ui/theme_switcher.js', import.meta.url);
let framework;
try { framework = readFileSync(frameworkPath, 'utf8'); }
catch { framework = null; }

function setup(mode = 'light') {
    const attributes = new Map([['data-theme-mode', mode], ['data-theme', mode]]);
    const calls = [];
    const root = {getAttribute: name => attributes.get(name) || null,
        setAttribute: (name, value) => attributes.set(name, value), removeAttribute: name => attributes.delete(name)};
    const media = {matches: false, addEventListener() {}};
    const frappe = {ui: {}, provide() {}, show_alert() {}, xcall: (method, args) => {calls.push({method,args}); return Promise.resolve();}};
    const context = vm.createContext({frappe, document:{documentElement:root}, window:{matchMedia:() => media},
        __: value => value, toTitle: value => value[0].toUpperCase() + value.slice(1)});
    vm.runInContext(framework, context);
    vm.runInContext(extension, context);
    const switcher = Object.create(frappe.ui.ThemeSwitcher.prototype);
    return {context, frappe, attributes, calls, media, switcher};
}

test('Navy appears alongside the native themes without duplicating registration', {skip: !framework}, async () => {
    const {context,frappe,switcher} = setup();
    await switcher.fetch_themes();
    assert.deepEqual(Array.from(switcher.themes, theme => theme.name), ['automatic','light','dark','navy']);
    const installed = frappe.ui.ThemeSwitcher;
    vm.runInContext(extension, context);
    assert.equal(frappe.ui.ThemeSwitcher, installed);
    await switcher.fetch_themes();
    assert.equal(switcher.themes.filter(theme => theme.name === 'navy').length, 1);
});

test('native selection saves Navy and retains dark component behavior', {skip: !framework}, () => {
    const {frappe,switcher,attributes,calls} = setup();
    switcher.toggle_theme('navy');
    frappe.ui.set_theme(); // Native Desk observes data-theme-mode changes.
    assert.equal(attributes.get('data-theme-mode'), 'navy');
    assert.equal(attributes.get('data-theme'), 'dark');
    assert.equal(attributes.get('data-hero-theme'), 'navy');
    assert.equal(calls[0].method, 'frappe.core.doctype.user.user.switch_theme');
    assert.equal(calls[0].args.theme, 'Navy');
    assert.equal(frappe.ui.get_current_theme(), 'dark');
});

test('saved Navy applies on load and is independent of the OS theme', {skip: !framework}, () => {
    const {frappe,attributes,media} = setup('navy');
    assert.equal(attributes.get('data-theme'), 'dark');
    media.matches = true; frappe.ui.set_theme();
    media.matches = false; frappe.ui.set_theme();
    assert.equal(attributes.get('data-hero-theme'), 'navy');
    assert.equal(attributes.get('data-theme-mode'), 'navy');
});

test('Light, Dark and Automatic clear Navy and preserve native system selection', {skip: !framework}, () => {
    const {frappe,switcher,attributes,media} = setup('navy');
    for (const mode of ['light','dark','automatic']) {
        switcher.toggle_theme(mode); frappe.ui.set_theme();
        assert.equal(attributes.has('data-hero-theme'), false);
        assert.equal(attributes.get('data-theme'), mode === 'automatic' ? 'light' : mode);
    }
    media.matches = true; frappe.ui.set_theme();
    assert.equal(attributes.get('data-theme'), 'dark');
    assert.equal(attributes.get('data-theme-mode'), 'automatic');
});
