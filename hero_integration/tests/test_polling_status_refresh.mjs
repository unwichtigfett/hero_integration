import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.textContent = ''; this.isConnected = true; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { if (name.startsWith('data-')) this.dataset[name.slice(5)] = value; if (name === 'disabled') this.disabled = true; }
    addEventListener(name, handler) { this.listeners ||= {}; this.listeners[name] = handler; }
    querySelectorAll(tag) { return this.children.flatMap(child => child instanceof Element ? [...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)] : []); }
}
const flush = () => new Promise(resolve => setImmediate(resolve));

function setup(dialogs = {}) {
    const timers = new Map(), calls = [], host = new Element('main');
    let id = 0;
    const data = {active_run: null, areas: [], file_areas: [], service: {mode: 'AUTO_SYNC', running: true, interval_minutes: 60},
        metrics: {records: {}, polling: {runs: 0, pass: 0, fail: 0, incomplete: 0, skipped: 0, period_days: 30}}};
    const document = {hidden: false, createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag)};
    const context = {window: {}, document, crypto: {randomUUID: () => "synthetic-request"}, setTimeout(fn, ms) { timers.set(++id, {fn, ms}); return id; }, clearTimeout(key) { timers.delete(key); },
        frappe: {...dialogs, utils: {escape_html: String}, call({method, args, type, callback}) {
            calls.push({method, type, args});
            callback({message: method.endsWith('sync_page_state') ? data : method.endsWith('sync_page_pending_relationships')
                ? data.pending_response : method.endsWith('sync_page_conflicts') ? data.conflicts_response
                    : method.endsWith('runtime_plan_status') ? data.plan : {rows: [], start: 0, total: 0}});
            return Promise.resolve();
        }}};
    vm.runInNewContext(readFileSync(new URL('../public/js/hero_migration.js', import.meta.url), 'utf8'), context);
    const main = {empty() { return main; }, addClass() { return main; }, get() { return host; }};
    const view = context.window.hero_integration_migration.mount({main});
    async function tick() {
        // Advance the live snapshot heartbeat and the independently scheduled page refresh.
        assert.equal(timers.size, 2);
        for (const [key, timer] of [...timers]) {
            if (timers.delete(key)) timer.fn();
        }
        await flush();
    }
    return {data, document, view, calls, timers, tick, host};
}

test('idle page discovers automatic runs, shows outcomes, and resumes idle refresh', async () => {
    const app = setup(); await flush();
    assert.deepEqual([...app.timers.values()].map(t => t.ms).sort((a,b) => a-b), [5000, 30000]);
    app.data.active_run = {name: 'RUN-1'};
    app.data.metrics.polling = {...app.data.metrics.polling, runs: 1, running: 1, last_status: 'Running'};
    await app.tick();
    assert.deepEqual([...app.timers.values()].map(t => t.ms), [5000, 5000]);
    const text = app.host.querySelectorAll('span').map(el => el.textContent);
    assert.ok(text.includes('Running'));
    const metric = label => app.host.querySelectorAll('div').find(el => el.className === 'hero-sync-metric' && el.children[0].textContent === label).children[1].textContent;
    assert.equal(metric('Runs'), '1');
    assert.equal(metric('Running'), '1');
    assert.equal(metric('Pass'), '0');
    app.data.active_run = null;
    app.data.metrics.polling = {...app.data.metrics.polling, running: 0, pass: 1, last_status: 'Succeeded'};
    await app.tick();
    assert.deepEqual([...app.timers.values()].map(t => t.ms).sort((a,b) => a-b), [5000, 30000]);
    assert.ok(app.host.querySelectorAll('span').some(el => el.textContent === 'Succeeded'));
    assert.equal(metric('Runs'), '1');
    assert.equal(metric('Running'), '0');
    assert.equal(metric('Pass'), '1');
    assert.ok(app.calls.every(call => call.type === 'GET'));
    assert.ok(app.calls.some(call => call.method.endsWith('sync_page_runs') && call.args.live === 1));
    assert.ok(app.host.querySelectorAll('section').some(el => el.className.includes('hero-sync-live')));
    assert.equal(app.host.querySelectorAll('progress').length, 0);
    assert.ok(app.calls.every(call => /\.(sync_page_state|sync_page_runs)$/.test(call.method)));
    app.view.destroy(); assert.equal(app.timers.size, 0);
});

test('hidden tabs do not query and teardown cancels the refresh timer', async () => {
    const app = setup(); await flush();
    const previous = app.calls.length;
    app.document.hidden = true;
    await app.tick();
    assert.equal(app.calls.length, previous);
    app.document.hidden = false;
    await app.tick();
    assert.ok(app.calls.length > previous);
    app.view.destroy();
    assert.equal(app.timers.size, 0);
});


test('detached pages stop refreshing even before teardown', async () => {
    const app = setup(); await flush();
    const previous = app.calls.length;
    app.host.querySelectorAll('section').find(el => el.className === 'hero-sync').isConnected = false;
    await app.tick();
    assert.equal(app.calls.length, previous);
    assert.equal(app.timers.size, 0);
});


test('States includes employees and services, including a zero service count', async () => {
    const app = setup(); await flush();
    const area = (key, label) => ({key, label, action_label: label, ready: false, kind: 'direct',
        description: '', scope_note: '', blocker: 'Pending approval', scope: {objects: [], object_count: 0, field_count: 0}});
    app.data.file_areas = [area('employees', 'Employees'), area('services', 'Services')];
    app.data.metrics.records = {employees: 5, services: 0};
    await app.tick();
    const metric = label => app.host.querySelectorAll('div').find(el => el.className === 'hero-sync-metric' && el.children[0].textContent === label)?.children[1].textContent;
    assert.equal(metric('Employees'), '5');
    assert.equal(metric('Services'), '0');
    app.view.destroy();
});

test('manual cards follow the per-area server blockers after each refresh', async () => {
    const app = setup(); await flush();
    const keys = ['contacts','projects','events','tasks','employees','products','services','documents','pictures','full'];
    app.data.file_areas = keys.map(key => ({key, label:key, ready:true, button:'Start'}));
    const action = key => app.host.querySelectorAll('button').find(el => el.dataset.area === key && el.className === 'hero-sync-action');
    for (const blockers of [{projects:'Migrate Employees first', tasks:'Migrate Employees first',
        documents:'Migrate Contacts first', pictures:'Migrate Contacts first'}, {tasks:'Migrate Employees first'}, {}]) {
        app.data.manual_start_blockers = blockers;
        await app.tick();
        for (const key of keys) {
            assert.equal(Boolean(action(key).disabled), Boolean(blockers[key]), key);
            assert.equal(action(key).children[0], blockers[key] ? 'Waiting' : 'Start');
        }
    }
    app.view.destroy();
});

test('pending relationships disappear from the list after resolution', async () => {
    const app = setup(); await flush();
    app.data.pending_response = {available: true, start: 0, total: 1, rows: [{operation: 'HeroTasks', source_id: '7',
        display_status: 'Waiting to import', dependencies: [{source_type: 'project', source_id: '5'}]}]};
    app.host.querySelectorAll('button').find(el => el.textContent === 'Pending relationships').listeners.click();
    await flush();
    assert.ok(app.host.querySelectorAll('td').some(el => el.textContent === 'Waiting to import'));
    app.data.pending_response = {available: true, start: 0, total: 0, rows: []};
    await app.tick();
    assert.ok(app.host.querySelectorAll('p').some(el => el.textContent === 'No pending relationships.'));
    app.view.destroy();
});

test('a linked import conflict in the Conflicts tab can be reported without approval actions', async () => {
    const app = setup(); await flush();
    app.data.conflicts_response = {start: 0, total: 1, rows: [{kind: 'record', name: 'observation',
        source_id: '6', area: 'Contacts', sync_status: 'Import conflict', run: 'RUN-1'}]};
    app.host.querySelectorAll('button').find(button => button.textContent === 'Conflicts').listeners.click();
    await flush();
    const row = app.host.querySelectorAll('tr').find(tr => tr.children.some(cell => cell.textContent === 'HERO 6'));
    assert.ok(row.children.some(cell => cell.textContent === 'Import conflict (RUN-1)'));
    assert.deepEqual(row.querySelectorAll('button').map(button => button.textContent), ['Report']);
    assert.ok(!row.querySelectorAll('span').some(span => span.textContent.startsWith('Fix in HERO')));
    app.view.destroy();
});

test('execution evidence and the unconfirmed one-shot dialog only read the approved plan', async () => {
    const shown = [];
    const app = setup({msgprint: () => shown.push('status'), confirm: () => shown.push('confirmation')});
    await flush();
    app.data.source_instance = 'fixture';
    app.data.service.mode = 'OFF';
    app.data.plan = {state: 'APPROVED', plan_key: 'fixture-plan', generation: 3};
    await app.tick();
    app.calls.length = 0;
    for (const label of ['Execution status', 'Compare bound objects once']) {
        const button = app.host.querySelectorAll('button').find(button => button.textContent === label);
        assert.equal(button.className, 'hero-sync-small');
        await button.listeners.click();
    }
    assert.deepEqual(shown, ['status', 'confirmation']);
    assert.deepEqual(app.calls.map(c => [c.method, c.type, c.args.source_instance, c.args.include_execution]), [
        ['hero_integration.api.runtime_plan_status', 'GET', 'fixture', 1],
        ['hero_integration.api.runtime_plan_status', 'GET', 'fixture', undefined],
    ]);
    app.view.destroy();
});
