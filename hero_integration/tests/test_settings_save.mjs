import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../public/js/hero_settings.js", import.meta.url), "utf8");

class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.value = ""; this.checked = false; this.handlers = {}; this.textContent = ""; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    cloneNode() { return new Element(this.tag); }
    replaceWith(replacement) { this.replacement = replacement; }
    remove() { this.removed = true; }
    find(predicate) { if (predicate(this)) return this; for (const child of this.children) { const found = child.find(predicate); if (found) return found; } }
}

test("settings use translated labels and interpolate the confirmed binding revision", async () => {
    const translations = {"HERO Settings": "HERO-Einstellungen", "Save Settings": "Einstellungen speichern",
        "Source binding confirmed (revision {0}).": "Quellenbindung bestätigt (Revision {0})."};
    const settings = {enabled: false, source_binding_confirmed: true, source_binding_revision: 42,
        api_token_status: "NOT_CONFIGURED", polling: {}, testing: {}};
    const window = {hero_integration_platform: {api: async () => settings},
        hero_integration_i18n: {text: value => translations[value] || value}};
    vm.runInNewContext(source, {window, document: {createElement: tag => new Element(tag)}});
    const workspace = new Element("main");
    await window.hero_integration_settings_ui.mount(workspace);
    assert.ok(workspace.find(el => el.textContent === "HERO-Einstellungen"));
    assert.ok(workspace.find(el => el.textContent === "Einstellungen speichern"));
    assert.ok(workspace.find(el => el.textContent === "Quellenbindung bestätigt (Revision 42)."));
});

test("settings save omits removed fields and preserves other controls", async () => {
    const settings = {enabled: true, api_url: "https://example.invalid/graphql", timeout_seconds: 30,
        allowed_hero_hosts: "example.invalid", hero_source_instance: "default", api_token_status: "NOT_CONFIGURED",
        polling: {mode: "OFF", data_sources: [], filters: {HeroCustomers: {search: "unsupported"}}, bootstrap: true},
        testing: {enabled: false, max_actions: 5}};
    const calls = [];
    const api = async (method, payload) => {
        if (method === "workspace_settings_save") {
            calls.push(payload);
            assert.equal("admin_notification_recipients" in payload, false);
            assert.equal("polling_data_sources" in payload, false);
            assert.equal("polling_filters_json" in payload, false);
            return settings;
        }
        assert.equal(method, "workspace_settings_get");
        return settings;
    };
    const sandbox = {window: {hero_integration_platform: {api}}, document: {createElement: tag => new Element(tag)}};
    vm.runInNewContext(source, sandbox);
    const workspace = new Element("main");
    await sandbox.window.hero_integration_settings_ui.mount(workspace);
    assert.equal(workspace.find(el => el.textContent === "Validated snapshot data sources"), undefined);
    assert.equal(workspace.find(el => el.textContent === "Operation filters JSON"), undefined);
    assert.ok(workspace.find(el => el.textContent === "Allow bootstrap"));
    assert.ok(workspace.find(el => el.textContent === "Allowed hosts"));
    assert.ok(workspace.find(el => el.textContent === "Source instance"));
    assert.equal(workspace.find(el => el.textContent === "Administrator notification recipients"), undefined);
    const save = workspace.find(el => el.tag === "button" && el.textContent === "Save Settings");
    await save.handlers.click();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].polling_bootstrap, true);
    assert.equal(calls[0].timeout_seconds, 30);
    assert.ok(workspace.find(el => el.textContent === "Settings saved."));
});

test("settings save announces progress and disables controls until completion", async () => {
    const settings = {enabled: true, api_url: "https://example.invalid/graphql", timeout_seconds: 30,
        allowed_hero_hosts: "example.invalid", hero_source_instance: "default", api_token_status: "NOT_CONFIGURED",
        polling: {mode: "OFF", bootstrap: false}, testing: {enabled: false, max_actions: 5}};
    let finishSave;
    const api = async (method) => {
        if (method === "workspace_settings_get") return settings;
        assert.equal(method, "workspace_settings_save");
        return new Promise(resolve => { finishSave = resolve; });
    };
    const sandbox = {window: {hero_integration_platform: {api}}, document: {createElement: tag => new Element(tag)}};
    vm.runInNewContext(source, sandbox);
    const workspace = new Element("main");
    await sandbox.window.hero_integration_settings_ui.mount(workspace);
    const save = workspace.find(el => el.tag === "button" && el.textContent === "Save Settings");
    const reset = workspace.find(el => el.tag === "button" && el.textContent === "Reset");
    const pending = save.handlers.click();
    const status = workspace.find(el => el.textContent === "Saving …");
    assert.equal(status.attributes.role, "status");
    assert.equal(save.disabled, true);
    assert.equal(reset.disabled, true);
    finishSave(settings);
    await pending;
    assert.equal(status.textContent, "Settings saved.");
    assert.equal(status.attributes.role, "status");
    assert.equal(save.disabled, false);
    assert.equal(reset.disabled, false);
});

test("a late settings response does not update a destroyed view", async () => {
    const settings = {enabled: true, api_url: "https://example.invalid/graphql", timeout_seconds: 30,
        allowed_hero_hosts: "example.invalid", hero_source_instance: "default", api_token_status: "NOT_CONFIGURED",
        polling: {mode: "OFF", bootstrap: false}, testing: {enabled: false, max_actions: 5}};
    let finishSave;
    const calls = [];
    const api = async (method) => {
        calls.push(method);
        if (method === "workspace_settings_get") return settings;
        assert.equal(method, "workspace_settings_save");
        return new Promise(resolve => { finishSave = resolve; });
    };
    const sandbox = {window: {hero_integration_platform: {api}}, document: {createElement: tag => new Element(tag)}};
    vm.runInNewContext(source, sandbox);
    const workspace = new Element("main");
    const view = await sandbox.window.hero_integration_settings_ui.mount(workspace);
    const root = workspace.children[0];
    const save = workspace.find(el => el.tag === "button" && el.textContent === "Save Settings");
    const pending = save.handlers.click();
    const status = workspace.find(el => el.textContent === "Saving …");
    view.destroy();
    finishSave({...settings, schema_refresh: {check_due: true}});
    await pending;
    assert.equal(root.removed, true);
    assert.equal(status.textContent, "Saving …");
    assert.equal(save.disabled, true);
    assert.deepEqual(calls, ["workspace_settings_get", "workspace_settings_save"]);
});

test("failed settings save reports an error and restores controls", async () => {
    const settings = {enabled: true, api_url: "https://example.invalid/graphql", timeout_seconds: 30,
        allowed_hero_hosts: "example.invalid", hero_source_instance: "default", api_token_status: "NOT_CONFIGURED",
        polling: {mode: "OFF", data_sources: [], filters: {}, bootstrap: false},
        testing: {enabled: false, max_actions: 5}};
    const calls = [];
    const api = async (method) => {
        calls.push(method);
        if (method === "workspace_settings_get") return settings;
        if (method === "workspace_settings_save") throw new Error("synthetic save failure");
        throw new Error(`Unexpected method: ${method}`);
    };
    const sandbox = {window: {hero_integration_platform: {api}}, document: {createElement: tag => new Element(tag)}};
    vm.runInNewContext(source, sandbox);
    const workspace = new Element("main");
    await sandbox.window.hero_integration_settings_ui.mount(workspace);
    const save = workspace.find(el => el.tag === "button" && el.textContent === "Save Settings");

    await save.handlers.click();

    assert.deepEqual(calls, ["workspace_settings_get", "workspace_settings_save"]);
    assert.equal(save.disabled, false);
    const status = workspace.find(el => el.textContent === "Could not save HERO settings. synthetic save failure");
    assert.ok(status);
    assert.equal(status.dataset.state, "error");
});

test('package preview translates states while keeping operation and settings identifiers', () => {
    const messages = {'Schema snapshot':'Schema-Snapshot', 'New':'Neu', 'Mappings':'Mappings',
        'Changed':'Geändert', 'Conflict':'Konflikt', 'Read services':'Leseservices',
        'Invalid: {0}':'Ungültig: {0}', 'Changed settings':'Geänderte Einstellungen', 'Polling Mode':'Polling-Modus'};
    const window = {hero_integration_platform: {}, hero_integration_i18n: {text: value => messages[value] || value}};
    vm.runInNewContext(source, {window});
    const preview = {snapshot:'New', mapping_package:{Changed:2, Conflict:1},
        read_services:[{target_operation:'HeroCustomers', state:'Invalid: schema mismatch'}], settings:['polling_mode']};
    assert.equal(window.hero_integration_settings_ui.packagePreviewSummary(preview),
        'Schema-Snapshot: Neu. Mappings: Geändert 2, Konflikt 1. Leseservices: HeroCustomers: Ungültig: schema mismatch. Geänderte Einstellungen: Polling-Modus (polling_mode).');
    assert.equal(preview.mapping_package.Conflict, 1);
    assert.equal(preview.read_services[0].state, 'Invalid: schema mismatch');
});

test('package confirmation shows translated preview without importing before confirmation', async () => {
    const messages = {'Schema snapshot':'Schema-Snapshot', 'New':'Neu', 'Mappings':'Mappings',
        'Changed':'Geändert', 'Conflict':'Konflikt', 'Read services':'Leseservices',
        'Changed settings':'Geänderte Einstellungen', 'none':'keine',
        'Import and activate this configuration now?':'Dieses Konfigurationspaket jetzt importieren und aktivieren?'};
    const preview = {snapshot:'New', mapping_package:{Changed:2, Conflict:1},
        read_services:[{target_operation:'HeroCustomers', state:'New'}], settings:[]};
    const calls = [];
    const api = async method => {
        calls.push(method);
        if (method === 'workspace_settings_get') return {polling:{}, testing:{}};
        if (method === 'config_import_preview') return preview;
        throw new Error('Unexpected import without confirmation');
    };
    let confirmation;
    const window = {hero_integration_platform:{api}, hero_integration_i18n:{text:value => messages[value] || value}};
    const frappe = {confirm: message => { confirmation = message; }};
    vm.runInNewContext(source, {window, frappe, document:{createElement: tag => new Element(tag)}});
    const workspace = new Element('main');
    await window.hero_integration_settings_ui.mount(workspace);
    const input = workspace.find(element => element.type === 'file');
    input.files = [{text:async () => '{}'}];
    await input.handlers.change();
    assert.match(confirmation, /Mappings: Geändert 2, Konflikt 1/);
    assert.match(confirmation, /HeroCustomers: Neu/);
    assert.match(confirmation, /jetzt importieren und aktivieren/);
    assert.deepEqual(calls, ['workspace_settings_get', 'config_import_preview']);

    preview.read_services[0].state = 'Invalid: <img src=x onerror=alert(1)>';
    input.files = [{text:async () => '{}'}];
    await input.handlers.change();
    assert.ok(confirmation.includes('&lt;img src=x onerror=alert(1)&gt;'));
    assert.equal(confirmation.includes('<img'), false);
    assert.ok(confirmation.includes('<br><br>'));
    assert.deepEqual(calls, ['workspace_settings_get', 'config_import_preview', 'config_import_preview']);
});
