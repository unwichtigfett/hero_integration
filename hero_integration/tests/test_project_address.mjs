import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import test from "node:test";

const source = readFileSync(new URL("../public/js/project_address.js", import.meta.url), "utf8");
const field = "custom_project_address", edit = "custom_edit_project_address";
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
function setup({isNew = true, canWrite = true, address = null, manual = 0, handler, staticReadonly = 0} = {}) {
    let events, saves = 0, dirty = false;
    const calls = [], writes = [], properties = [];
    const frappe = {ui: {form: {on: (doctype, value) => { events = value; }}},
        provide: () => { frappe.hero_project_address = {}; },
        call: async request => {
            calls.push(request);
            return {message: handler ? await handler(request) : {state: "ready", name: "A1", address: "Main 1\nCity"}};
        }};
    vm.runInNewContext(source, {frappe, __: text => text});
    const frm = {
        doc: {name: "P1", customer: "C1", [field]: address, [edit]: manual},
        fields_dict: {[field]: {df: {read_only: staticReadonly}}},
        is_new: () => isNew, has_perm: () => canWrite, save: () => { saves++; },
        set_df_property: (...args) => properties.push(args), refresh_field() {}, set_query() {},
        async set_value(key, value) {
            writes.push([key, value]); this.doc[key] = value; dirty = true;
            await events[key]?.(this);
        },
    };
    const target = {isConnected: true, textContent: ""};
    return {frm, events, calls, writes, properties, target, frappe,
        render: () => frappe.hero_project_address.render(frm, target),
        saves: () => saves, dirty: () => dirty};
}
test("new Project copies Customer Address via native unsaved set_value", async () => {
    const app = setup();
    await app.events.refresh(app.frm);
    assert.equal(app.frm.doc[field], "A1"); assert.equal(app.dirty(), true); assert.equal(app.saves(), 0);
    assert.equal(app.calls[0].type, "GET"); assert.equal(app.calls[0].args.project, null);
});
test("opening an existing Project does not dirty or auto-save it", async () => {
    const app = setup({isNew: false});
    await app.events.refresh(app.frm);
    assert.equal(app.writes.length, 0); assert.equal(app.dirty(), false);
    assert.equal(app.calls[0].args.project, "P1");
});
test("customer changes and toggles preserve the project address", async () => {
    const app = setup({address: "A2"});
    await app.events.customer(app.frm);
    assert.equal(app.frm.doc[field], "A2");
    app.frm.doc[field] = "A2";
    await app.events.custom_edit_project_address(app.frm);
    assert.equal(app.frm.doc[field], "A2"); assert.equal(app.saves(), 0);
});
test("manual Address survives customer changes; preview is literal text", async () => {
    const app = setup({address: "A2", manual: 1, handler: request =>
        ({state: "ready", name: request.args.address, address: "<img onerror=alert(1)>\nSite"})});
    await app.render();
    await app.events.customer(app.frm);
    assert.equal(app.frm.doc[field], "A2"); assert.equal(app.writes.length, 0);
    assert.match(app.target.textContent, /<img onerror/);
});
test("late customer responses cannot overwrite a newer manual selection", async () => {
    const waiting = deferred();
    const app = setup({handler: request => request.args.manual ? {state: "ready", name: "A2", address: "Manual"} : waiting.promise});
    const pending = app.events.customer(app.frm);
    app.frm.doc[edit] = 1; app.frm.doc[field] = "A2";
    await app.events.custom_edit_project_address(app.frm);
    waiting.resolve({state: "ready", name: "A1", address: "Late"});
    await pending;
    assert.equal(app.frm.doc[field], "A2");
});
test("late response for old customer cannot replace latest customer address", async () => {
    const waiting = deferred();
    const app = setup({handler: request => request.args.customer === "C1" ? waiting.promise :
        {state: "ready", name: "A2", address: "New"}});
    const first = app.events.customer(app.frm);
    app.frm.doc.customer = "C2"; await app.events.customer(app.frm);
    waiting.resolve({state: "ready", name: "A1", address: "Old"}); await first;
    assert.equal(app.frm.doc[field], "A2");
});
test("missing default preserves an existing address and remains optional", async () => {
    const app = setup({address: "A2", handler: () => ({state: "missing", name: null, address: null})});
    await app.events.customer(app.frm);
    assert.equal(app.frm.doc[field], "A2");
    await app.render(); assert.match(app.target.textContent, /optional/);
});
test("permission errors are honest and never clear or save the chosen address", async () => {
    const app = setup({address: "A2", handler: () => { throw Error("private upstream details"); }});
    await app.render();
    assert.equal(app.frm.doc[field], "A2"); assert.equal(app.writes.length, 0);
    assert.match(app.target.textContent, /permission/); assert.doesNotMatch(app.target.textContent, /upstream/);
});
test("read-only forms and statically protected fields are not relaxed", async () => {
    const app = setup({isNew: false, canWrite: false, address: "A2", staticReadonly: 1});
    await app.events.customer(app.frm);
    assert.equal(app.writes.length, 0); assert.equal(app.properties.length, 0);
});
test("validation awaits address default without saving business data", async () => {
    const app = setup();
    await app.events.validate(app.frm);
    assert.equal(app.frm.doc[field], "A1"); assert.equal(app.saves(), 0);
});
test("rendering during a pending default cannot cancel it or let validation finish early", async () => {
    const waiting = deferred();
    const app = setup({handler: request => request.args.address ? {state: "ready", name: "A1", address: "Main"} : waiting.promise});
    const change = app.events.customer(app.frm);
    const render = app.render();
    const validate = app.events.validate(app.frm);
    assert.equal(app.calls.length, 1);
    waiting.resolve({state: "ready", name: "A1", address: "Main"});
    await Promise.all([change, render, validate]);
    assert.equal(app.frm.doc[field], "A1"); assert.equal(app.target.textContent, "Main");
});
