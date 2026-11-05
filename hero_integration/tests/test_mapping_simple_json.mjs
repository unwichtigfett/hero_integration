import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source = readFileSync(new URL('../public/js/hero_mapping_simple.js', import.meta.url), 'utf8');
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.handlers = {}; this.value = ''; this.disabled = false; this.text = ''; }
    setAttribute(key, value) { this.attributes[key] = value; if (key === 'value') this.value = value; }
    append(...children) { for (const child of children) { this.children.push(child); if (typeof child === 'object') child.parent = this; } }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    set textContent(value) { this.text = String(value ?? ''); this.children = []; }
    get textContent() { return this.text + this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    click() { if (!this.disabled) return this.handlers.click?.(); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
    querySelectorAll(selector) {
        const matches = node => selector.split(',').some(part => part.startsWith('[')
            ? Object.hasOwn(node.attributes, part.slice(1, -1)) : node.tag === part);
        return this.children.flatMap(child => typeof child === 'object'
            ? [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)] : []);
    }
}
const snapshot = {name:'snapshot', display_name:'Schema', source_instance:'source', analysis_status:'Validated', configured_source:true, creation:'2026'};
const item = {definition_id:'definition', state:'NEW', expected:{generation:0,draft_revision:0,active_revision:0,reconciliation_revision:0}};
const queryFile = JSON.stringify({format_version:'hero-integration-mappings-v1',schema_fingerprint:'schema-fingerprint',replace_mappings:true,mapping_package:{},read_services:[]});
const mutationFile = JSON.stringify({format_version:'hero-personal-mapping-portable-v2', direction:'FRAPPE_TO_MUTATION'});
async function setup(overrides = {}, manager = true) {
    const calls = [], confirmations = [], downloads = [];
    const body = new Element('body'), host = new Element('main'); body.append(host);
    const methods = {
        simple_mapping_state: () => ({rules:[], sends:[{operation:'updateCustomer', hero:'customer.name', target:'Customer.customer_name', app:'erpnext', status:'Active',revision:1}], operations:[]}),
        schema_snapshot_list: () => [snapshot],
        simple_mapping_export: ({direction}) => ({filename:direction==='QUERY_TO_FRAPPE'?'query-mappings.json':'mutation-mappings.json',content:direction==='QUERY_TO_FRAPPE'?queryFile:mutationFile}),
        simple_mapping_import_preview: ({direction}) => direction==='QUERY_TO_FRAPPE'
            ? {can_apply:true,read_services:[{target_operation:'HeroCustomers',state:'Changed'}],replace_mappings:true,replacement:{field_mappings:17,graphql_mappings:1},preview_fingerprint:'fingerprint',activation:true,execution:false}
            : {can_apply:true,can_save:true,items:[item],preview_fingerprint:'fingerprint',activation:true,execution:false},
        simple_mapping_import_apply: () => ({activation:true,execution:false,status:'IMPORTED_MAPPINGS'}),
        ...overrides,
    };
    const window = {hero_integration_ui:{isSystemManager:manager}};
    const frappe = {utils:{escape_html: text => text.replaceAll('<','&lt;').replaceAll('>','&gt;')},
        call: async request => { const method = request.method.split('.').at(-1); calls.push({...request,method});
            assert.ok(methods[method], `unexpected RPC ${method}`); return {message:await methods[method](request.args)}; },
        confirm: (message, accept, cancel) => confirmations.push({message,accept,cancel})};
    vm.runInNewContext(source, {window,frappe,document:{body,createElement:tag => new Element(tag)},
        URL:{createObjectURL: blob => { downloads.push(blob); return 'blob:fixture'; },revokeObjectURL(){}},Blob:class {constructor(values){this.values=values;}},setTimeout:fn=>fn()});
    const controller = window.hero_integration_mapping_simple.mount({main:{empty(){host.replaceChildren();return this;},addClass(){return [host];}}});
    const flush = async () => { for (let i=0;i<12;i++) await Promise.resolve(); };
    const button = name => host.querySelectorAll('button').find(node => node.textContent === name);
    const click = async name => { assert.ok(button(name), name); await button(name).click(); await flush(); };
    const upload = async (payload, size=payload.length) => { const input=host.querySelectorAll('input').find(node=>node.attributes.type==='file');
        input.files=[{text:async()=>payload,size}]; input.handlers.change(); await flush(); };
    await flush();
    return {window,host,calls,confirmations,downloads,controller,flush,click,upload,button};
}

test('list has no visual editor and exports only Query mappings via GET', async () => {
    const app=await setup();
    assert.equal(app.host.querySelectorAll('textarea,details').length,0);
    assert.equal(app.button('Create Mapping'),undefined);
    await app.click('Export JSON');
    const request=app.calls.find(call=>call.method==='simple_mapping_export');
    assert.equal(request.type,'GET');
    assert.equal(request.args.direction,'QUERY_TO_FRAPPE');
    assert.equal(request.args.snapshot_name,'snapshot');
    assert.equal(app.host.querySelectorAll('[data-mapping-snapshot]').length,1);
    assert.equal(JSON.parse(app.downloads[0].values[0]).format_version,'hero-integration-mappings-v1');
    assert.equal(app.calls.some(call=>call.method.startsWith('config_')),false);
    assert.equal(app.downloads.length,1);
    assert.equal(app.calls.some(call=>call.type==='POST'),false);
});

test('Mutation export includes active mappings and uses only configured source snapshots', async () => {
    const app=await setup({schema_snapshot_list:()=>[{...snapshot,name:'foreign',configured_source:false,creation:'9999'},snapshot]});
    await app.click('Mutation'); await app.click('Export JSON');
    const request=app.calls.find(call=>call.method==='simple_mapping_export');
    assert.equal(request.type,'GET');
    assert.equal(request.args.drafts_only,undefined);
    assert.equal(request.args.direction,'FRAPPE_TO_MUTATION');
    assert.equal(request.args.snapshot_name,'snapshot');
    assert.equal(request.args.source_instance,undefined);
    assert.equal(app.host.querySelectorAll('[data-mapping-snapshot]').length,1);
    assert.match(app.host.textContent,/Source Frappe/);
    assert.equal(app.host.querySelectorAll('option').some(node=>node.value==='HeroCustomers'),false);
});

test('Query import previews replacement counts, confirms once and prevents duplicate apply', async () => {
    const app=await setup(); const payload=queryFile;
    await app.upload(payload);
    assert.equal(app.calls.some(call=>call.method==='simple_mapping_import_apply'),false);
    assert.match(app.confirmations[0].message,/17 field mappings, 1 GraphQL mappings/);
    assert.equal(app.button('Mutation').disabled,true);
    await app.confirmations[0].accept(); await app.confirmations[0].accept();
    assert.equal(app.calls.filter(call=>call.method==='simple_mapping_import_apply').length,1);
    assert.equal(app.calls.find(call=>call.method==='simple_mapping_import_apply').args.payload_json,payload);
    assert.equal(app.button('Mutation').disabled,false);
    const applied=app.calls.find(call=>call.method==='simple_mapping_import_apply');
    assert.equal(applied.args.direction,'QUERY_TO_FRAPPE');
    assert.equal(applied.args.snapshot_name,'snapshot');
    assert.equal(applied.args.preview_fingerprint,'fingerprint');
    assert.equal(applied.args.expected_states_json,'[]');
    assert.equal(applied.args.confirmed,1);
    assert.doesNotMatch(app.confirmations[0].message,/settings|configuration|contract/i);
    assert.equal(app.calls.some(call=>call.method.startsWith('config_')),false);
});

test('Mutation import publishes only after confirmation with exact fingerprint and expected states', async () => {
    const app=await setup(); await app.click('Mutation'); await app.upload(mutationFile);
    assert.equal(app.calls.some(call=>call.method==='simple_mapping_import_apply'),false);
    assert.match(app.confirmations[0].message,/No HERO operation will be executed/);
    await app.confirmations[0].accept();
    const request=app.calls.find(call=>call.method==='simple_mapping_import_apply');
    assert.equal(request.type,'POST'); assert.equal(request.args.preview_fingerprint,'fingerprint');
    assert.deepEqual(JSON.parse(request.args.expected_states_json),[{definition_id:'definition',...item.expected}]);
    assert.equal(request.args.direction,'FRAPPE_TO_MUTATION'); assert.equal(request.args.confirmed,1);
    assert.equal(app.calls.some(call=>/execute|workspace_service_execute/.test(call.method)),false);
});

test('blocked Mutation preview never offers confirmation or publication', async () => {
    const app=await setup({simple_mapping_import_preview:()=>({can_apply:false,blockers:[{reason:'Missing reviewed contract'}]})});
    await app.click('Mutation'); await app.upload(mutationFile);
    assert.equal(app.confirmations.length,0);
    assert.match(app.host.textContent,/Missing reviewed contract/);
    assert.equal(app.calls.some(call=>call.method==='simple_mapping_import_apply'),false);
});

test('wrong file direction and cancellation cause no import writes', async () => {
    const app=await setup(); await app.upload(mutationFile);
    assert.equal(app.confirmations.length,0);
    await app.click('Mutation'); await app.upload(mutationFile);
    app.confirmations[0].cancel(); await app.confirmations[0].accept();
    assert.equal(app.calls.some(call=>call.method==='simple_mapping_import_apply'),false);
});

test('destroying the page invalidates pending confirmation', async () => {
    const app=await setup(); await app.upload(queryFile);
    app.controller.destroy(); await app.confirmations[0].accept();
    assert.equal(app.calls.some(call=>call.method==='simple_mapping_import_apply'),false);
});

test('read-only users see the list without import/export controls', async () => {
    const app=await setup({},false); await app.click('Mutation');
    assert.equal(app.button('Import JSON'),undefined); assert.equal(app.button('Export JSON'),undefined);
    assert.equal(app.calls.some(call=>call.method==='schema_snapshot_list'),false);
});

test('legacy draft files are rejected before requesting Mutation publication', async () => {
    const app=await setup(); await app.click('Mutation');
    await app.upload(JSON.stringify({format_version:'hero-personal-mapping-draft-exchange-v1',direction:'FRAPPE_TO_MUTATION'}));
    assert.equal(app.calls.some(call=>call.method==='simple_mapping_import_preview'),false);
    assert.equal(app.confirmations.length,0);
});


test('configuration packages are rejected in both tabs with a Settings pointer and no preview RPC', async () => {
    for (const tab of ['Query','Mutation']) {
        const app=await setup(); if(tab==='Mutation') await app.click(tab);
        for(const format_version of ['hero-integration-config-v1','hero-integration-config-v2']) {
            await app.upload(JSON.stringify({format_version,settings:{any:'value'},contracts:[{}]}));
            assert.match(app.host.textContent,/Configuration packages belong in Settings/);
        }
        assert.equal(app.confirmations.length,0);
        assert.equal(app.calls.some(call=>call.type==='POST'),false);
    }
});

test('Query format cannot smuggle configuration sections through the mapping UI', async () => {
    const app=await setup();
    for(const key of ['settings','schema_snapshot','mutation_mappings','mutation_contracts','contracts']) {
        await app.upload(JSON.stringify({...JSON.parse(queryFile),[key]:{}}));
        assert.match(app.host.textContent,/belong in Settings/);
    }
    assert.equal(app.calls.some(call=>call.method==='simple_mapping_import_preview'),false);
});

test('both directions require a configured validated snapshot before import or export', async () => {
    const app=await setup({schema_snapshot_list:()=>[{...snapshot,configured_source:false},{...snapshot,name:'pending',analysis_status:'Pending'}]});
    for(const [tab,payload] of [['Query',queryFile],['Mutation',mutationFile]]) {
        if(tab==='Mutation') await app.click(tab);
        await app.upload(payload); await app.click('Export JSON');
        assert.match(app.host.textContent,/Choose a validated schema snapshot first/);
    }
    assert.equal(app.confirmations.length,0);
    assert.equal(app.calls.some(call=>['simple_mapping_import_preview','simple_mapping_export'].includes(call.method)),false);
});

test('Query preview blockers or missing fingerprint cannot open activation confirmation', async () => {
    for(const preview of [{can_apply:false,blockers:[{reason:'Reviewed source required'}]},{can_apply:true,read_services:[]}]) {
        const app=await setup({simple_mapping_import_preview:()=>preview}); await app.upload(queryFile);
        assert.equal(app.confirmations.length,0);
        assert.equal(app.calls.some(call=>call.method==='simple_mapping_import_apply'),false);
        assert.equal(app.button('Mutation').disabled,false);
    }
});

test('Query preview omits settings and contract summaries even if a server adds them', async () => {
    const app=await setup();
    const text=app.window.hero_integration_mapping_simple.previewText({read_services:[{target_operation:'HeroTasks',state:'Replace'}],
        settings:['private_setting'],contracts:[{}],mutation_mappings:{count:12}});
    assert.match(text,/Tasks/);
    assert.doesNotMatch(text,/settings|private_setting|contract|Mutation|12/i);
});

test('snapshot selection is preserved across tabs and sent to preview/apply', async () => {
    const app=await setup({schema_snapshot_list:()=>[snapshot,{...snapshot,name:'older',creation:'2025'}]});
    const picker=app.host.querySelectorAll('[data-mapping-snapshot]')[0];picker.value='older';picker.handlers.change();
    await app.click('Mutation'); await app.click('Query');
    assert.equal(app.host.querySelectorAll('[data-mapping-snapshot]')[0].value,'older');
    await app.upload(queryFile);await app.confirmations[0].accept();
    assert.equal(app.calls.find(call=>call.method==='simple_mapping_import_preview').args.snapshot_name,'older');
    assert.equal(app.calls.find(call=>call.method==='simple_mapping_import_apply').args.snapshot_name,'older');
});

test('destroying the page while a file is read cannot request a preview', async () => {
    const app=await setup();let resolve;
    const input=app.host.querySelectorAll('input').find(node=>node.attributes.type==='file');
    input.files=[{text:()=>new Promise(done=>{resolve=done;}),size:queryFile.length}];input.handlers.change();
    app.controller.destroy();resolve(queryFile);await app.flush();
    assert.equal(app.calls.some(call=>call.method==='simple_mapping_import_preview'),false);
});

test('a configuration export response is rejected instead of downloading settings from Mapping', async () => {
    const app=await setup({simple_mapping_export:()=>({filename:'configuration.json',content:JSON.stringify({format_version:'hero-integration-config-v2',settings:{}})})});
    await app.click('Export JSON');
    assert.equal(app.downloads.length,0);
    assert.match(app.host.textContent,/Invalid mapping export response/);
});
