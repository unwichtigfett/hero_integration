import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../public/js/hero_migration.js", import.meta.url), "utf8");

class Element {
    constructor(tag) {
        this.tag = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.handlers = {};
    }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    addEventListener(event, fn) { this.handlers[event] = fn; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    *descendants() {
        yield this;
        for (const child of this.children) if (child instanceof Element) yield* child.descendants();
    }
    querySelectorAll(tag) { return [...this.descendants()].filter(el => el.tag === tag); }
}

async function mount({testing = true, mode = "MONITOR", polling = {}, planState = "APPROVED", crypto = {randomUUID:()=>"request"}} = {}) {
    const host = new Element("main");
    const calls = [], confirmations = [];
    const state = {source_instance:"source-A", source_revision:4, test_limit:10, testing_enabled:testing,
        areas:[], metrics:{records:{},polling}, service:{mode,polled_sources:["HeroCustomers"]}};
    const responses = {sync_page_state:state, sync_page_runs:{rows:[],total:0},
        runtime_plan_status:{state:planState, plan_key:"plan", generation:3, monitor_operation:"HeroCustomers",
            graphql_service:"reader", graphql_mapping:"mapping", schema_snapshot:"snapshot"},
        start_monitor_test:{run:"observed",selected:2,processed:2,limit:10,conflicts:0,cohort_digest:"digest"}};
    const frappe = {boot:{lang:"en"},utils:{escape_html:String}, confirm:(message,yes)=>confirmations.push({message,yes}),
        call: async ({method,args,type,callback}) => {
            const key = method.split(".").at(-1);
            calls.push({key,type,args});
            assert.ok(Object.hasOwn(responses,key),key);
            callback({message:responses[key]});
        }};
    const window = {};
    const document = {createElement: tag => new Element(tag), createElementNS: (_,tag) => new Element(tag)};
    vm.runInNewContext(source,{frappe,window,document,clearTimeout,setTimeout:()=>0,crypto});
    const main = {empty(){return this;},addClass(){return this;},get(){return host;}};
    window.hero_integration_migration.mount({main});
    await new Promise(resolve=>setImmediate(resolve));
    const find = check => [...host.descendants()].find(check);
    return {host, calls, confirmations, button:find(el=>el.attributes["data-action"]==="manual-monitor-test"),
        notice:find(el=>el.className==="hero-sync-notice")};
}

test("MONITOR starts only after the operator reviews the active plan, source revision and next limit", async () => {
    const {button,calls,confirmations,notice} = await mount();
    assert.equal(button.attributes.disabled,undefined);
    await button.handlers.click();
    assert.match(confirmations[0].message,/10.*HeroCustomers.*reader.*mapping.*snapshot/);
    assert.equal(calls.at(-1).key,"runtime_plan_status");
    assert.equal(calls.at(-1).type,"GET");
    assert.ok(!calls.some(c=>c.type==="POST"));
    await confirmations[0].yes();
    const posts = calls.filter(c=>c.type==="POST");
    assert.equal(posts.length,1);
    assert.deepEqual({...posts[0].args},{request_id:"request",plan_key:"plan",expected_plan_generation:3,
        expected_source_revision:4,operation:"HeroCustomers",expected_limit:10,confirmed:1});
    assert.equal(posts[0].key,"start_monitor_test");
    assert.match(notice.textContent,/2\/10 selected, 2 observed/);
});

test("missing approval or disabled testing never starts a manual observation", async () => {
    const unapproved = await mount({planState:"DRAFT"});
    await unapproved.button.handlers.click();
    assert.equal(unapproved.confirmations.length,0);
    assert.match(unapproved.notice.textContent,/approved runtime plan/);
    assert.ok(!unapproved.calls.some(c=>c.type==="POST"));
    assert.equal((await mount({testing:false})).button.attributes.disabled,"");
    assert.equal((await mount({mode:"AUTO_SYNC"})).button,undefined);
});

test("HTTP Desk uses secure random bytes and sets RFC 4122 version and variant", async () => {
    let reads = 0;
    const crypto = {getRandomValues(bytes) { reads++; return bytes.fill(255); }};
    const {button, confirmations, calls} = await mount({crypto});
    await button.handlers.click();
    assert.equal(calls.filter(c=>c.type==="POST").length,0);
    await confirmations[0].yes();
    assert.equal(reads,1);
    assert.equal(calls.find(c=>c.type==="POST").args.request_id,"ffffffff-ffff-4fff-bfff-ffffffffffff");
});

test("polling cards keep execution success separate from unfinished inventory and errors", async () => {
    const {host} = await mount({mode:"OFF", polling:{runs:10, pass:6, partial_inventory:4,
        fail:2, incomplete:1, conflicts:1}});
    const cards = Object.fromEntries([...host.descendants()]
        .filter(el => el.className === "hero-sync-metric")
        .map(el => el.children.map(child => String(child.textContent))));
    for (const [label, value] of Object.entries({Runs:10, Pass:6, "Of which: partial inventory":4,
        Fail:2, Incomplete:1, Conflicts:1})) {
        assert.equal(cards[label], String(value));
    }
});
