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
        'Schema-Snapshot: Neu. Mappings: Geändert 2, Konflikt 1. Leseservices: HeroCustomers: Ungültig: schema mismatch. Geänderte Einstellungen: Polling-Modus (polling_mode). Affected areas (1): Customers.');
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

test('settings load errors translate known details and the fallback', async () => {
    const messages = {'Could not load HERO settings. {0}':'Einstellungen konnten nicht geladen werden. {0}',
        'Known error':'Bekannter Fehler', 'The server did not provide additional details.':'Keine weiteren Details.'};
    for (const [error, expected] of [[new Error('Known error'), 'Bekannter Fehler'], [{}, 'Keine weiteren Details.']]) {
        const window = {hero_integration_platform:{api:async () => { throw error; }},
            hero_integration_i18n:{text:value => messages[value] || value}};
        vm.runInNewContext(source, {window, document:{createElement:tag => new Element(tag)}});
        const workspace = new Element('main');
        await window.hero_integration_settings_ui.mount(workspace);
        assert.ok(workspace.find(element => element.textContent === `Einstellungen konnten nicht geladen werden. ${expected}`));
    }
});

test('package import reports all affected areas once, including unchanged read services', () => {
    const areas = ['HeroCustomers', 'HeroProjectMatches', 'HeroCalendarEvents', 'HeroTasks', 'HeroEmployees',
        'HeroProducts', 'HeroServices', 'HeroDocuments', 'HeroPictures'];
    const messages = {'Affected areas ({0}): {1}.':'Betroffene Bereiche ({0}): {1}.',
        Customers:'Kunden', Projects:'Projekte', Calendar:'Kalender', Tasks:'Aufgaben', Employees:'Mitarbeiter',
        Products:'Artikel', Services:'Leistungen', Documents:'Dokumente', Pictures:'Bilder'};
    const window = {hero_integration_platform:{}, hero_integration_i18n:{text:value => messages[value] || value}};
    vm.runInNewContext(source, {window});
    const result = {snapshot:'Unchanged', adopted:145, settings:[],
        read_services:areas.map(target_operation => ({target_operation, state:'Unchanged'}))};
    const summary = window.hero_integration_settings_ui.packageImportSummary(result);
    assert.match(summary, /0 read services approved/);
    assert.match(summary, /Betroffene Bereiche \(9\): Kunden, Projekte, Kalender, Aufgaben, Mitarbeiter, Artikel, Leistungen, Dokumente, Bilder\./);
    assert.equal(summary.includes('HeroEmployees'), false);
    result.read_services.push(result.read_services[0]);
    assert.match(window.hero_integration_settings_ui.packageImportSummary(result), /Betroffene Bereiche \(9\)/);
});

test('replacement import previews affected areas and counts before the existing single confirmation', async () => {
    const replacement = {operations:['HeroCustomers', 'HeroEmployees'], field_mappings:47, graphql_mappings:3};
    const preview = {snapshot:'Unchanged', mapping_package:{Changed:47}, read_services:[], settings:[],
        replace_mappings:true, replacement};
    const messages = {'Affected areas ({0}): {1}.':'Betroffene Bereiche ({0}): {1}.', Customers:'Kunden', Employees:'Mitarbeiter',
        'Existing mappings in these areas will be replaced: {0} field mappings and {1} GraphQL mappings. Previous versions remain in history.':
            'Bestehende Mappings in diesen Bereichen werden ersetzt: {0} Feldmappings und {1} GraphQL-Mappings. Bisherige Versionen bleiben im Verlauf erhalten.'};
    const calls = [];
    const payload = JSON.stringify({replace_mappings:true});
    const result = {...preview, adopted:47, replacement:{...replacement, field_mappings:46}};
    const api = async (method, args) => {
        calls.push([method,args]);
        if (method === 'workspace_settings_get') return {polling:{}, testing:{}};
        if (method === 'config_import_preview') return preview;
        if (method === 'config_import') return result;
        throw new Error(method);
    };
    let confirmation, accept, confirmations = 0;
    const frappe = {confirm:(message, callback) => { confirmation = message; accept = callback; confirmations++; }};
    const window = {hero_integration_platform:{api}, hero_integration_i18n:{text:value => messages[value] || value}};
    vm.runInNewContext(source, {window, frappe, document:{createElement:tag => new Element(tag)}});
    const workspace = new Element('main');
    await window.hero_integration_settings_ui.mount(workspace);
    const input = workspace.find(element => element.type === 'file');
    input.files = [{text:async () => payload}];
    await input.handlers.change();
    assert.match(confirmation, /Betroffene Bereiche \(2\): Kunden, Mitarbeiter/);
    assert.match(confirmation, /werden ersetzt: 47 Feldmappings und 3 GraphQL-Mappings/);
    assert.equal(calls.some(([method]) => method === 'config_import'), false);
    await accept();
    assert.equal(confirmations, 1);
    assert.deepEqual(JSON.parse(JSON.stringify(calls.find(([method]) => method === 'config_import')[1])),
        {payload_json:payload, confirmed:1});
    const status = workspace.find(element => element.dataset.state === 'success' && element.textContent.includes('Betroffene Bereiche (2)'));
    assert.ok(status);
    assert.match(status.textContent, /Replaced: 46 field mappings and 3 GraphQL mappings/);
    assert.equal(window.hero_integration_settings_ui.packagePreviewSummary({...preview, replace_mappings:false}).includes('werden ersetzt'), false);
});

test('configuration summaries distinguish requested active contracts from mapping drafts', () => {
    const translations = {
        'Mutation contracts: {0}; requested active status: {1}.':'Mutationsverträge: {0}; gewünschter Status Aktiv: {1}.',
        'Mutation contracts imported: {0}; active: {1}.':'Mutationsverträge importiert: {0}; aktiv: {1}.',
        'Mutation mappings to import and activate: {0}.':'Als Entwürfe zu importierende Mutationsmappings: {0}.',
        'Mutation mappings imported and activated: {0}.':'Als Entwürfe importierte Mutationsmappings: {0}.',
    };
    const window = {hero_integration_platform:{},hero_integration_i18n:{text:value=>translations[value]||value}};
    vm.runInNewContext(source,{window});
    const contracts=Array.from({length:9},(_,index)=>({operation_name:`operation${index}`,status:index<8?'Active':'Draft',state:index<4?'New':'Unchanged'}));
    const value={snapshot:'Unchanged',mapping_package:{New:1},read_services:[],settings:[],adopted:1,
        mutation_contracts:{counts:{New:4,Unchanged:5},contracts,can_apply:true,execution:false},
        mutation_mappings:{count:9,state:'Draft',execution:false}};
    const preview=window.hero_integration_settings_ui.packagePreviewSummary(value);
    assert.match(preview,/Mutationsverträge: 9; gewünschter Status Aktiv: 8\./);
    assert.match(preview,/Als Entwürfe zu importierende Mutationsmappings: 9\./);
    assert.match(preview,/No HERO operation will be executed/);
    const imported=window.hero_integration_settings_ui.packageImportSummary(value);
    assert.match(imported,/Mutationsverträge importiert: 9; aktiv: 8\./);
    assert.match(imported,/Als Entwürfe importierte Mutationsmappings: 9\./);
    assert.match(imported,/No HERO operation was executed/);
    assert.equal(value.mutation_contracts.contracts.filter(row=>row.status==='Active').length,8);
});

test('configuration without mutation contracts or drafts adds no empty counts', () => {
    const window={hero_integration_platform:{}};vm.runInNewContext(source,{window});
    const value={snapshot:'Unchanged',mapping_package:{},read_services:[],settings:[],adopted:0,
        mutation_contracts:{counts:{New:0,Approve:0,Unchanged:0,Conflict:0},contracts:[]},mutation_mappings:{count:0}};
    assert.doesNotMatch(window.hero_integration_settings_ui.packagePreviewSummary(value),/Mutation|HERO operation/);
    assert.doesNotMatch(window.hero_integration_settings_ui.packageImportSummary(value),/Mutation|HERO operation/);
});

test('the existing configuration confirmation explicitly announces eight active contracts', async () => {
    const preview={snapshot:'Unchanged',mapping_package:{},read_services:[],settings:[],
        mutation_contracts:{counts:{Approve:8},contracts:Array.from({length:8},(_,i)=>({operation_name:`update${i}`,status:'Active',state:'Approve'})),can_apply:true,execution:false},
        mutation_mappings:{count:8,state:'Draft'}};
    const calls=[];let confirmation;
    const window={hero_integration_platform:{api:async method=>{calls.push(method);if(method==='workspace_settings_get')return {polling:{},testing:{}};
        if(method==='config_import_preview')return preview;throw new Error('Unconfirmed import');}}};
    const frappe={confirm:message=>{confirmation=message;}};
    vm.runInNewContext(source,{window,frappe,document:{createElement:tag=>new Element(tag)}});
    const workspace=new Element('main');await window.hero_integration_settings_ui.mount(workspace);
    const input=workspace.find(element=>element.type==='file');input.files=[{text:async()=>'{}'}];await input.handlers.change();
    assert.match(confirmation,/Mutation contracts: 8; requested active status: 8\./);
    assert.match(confirmation,/Mutation mappings to import and activate: 8\./);
    assert.match(confirmation,/No HERO operation will be executed/);
    assert.deepEqual(calls,['workspace_settings_get','config_import_preview']);
});
