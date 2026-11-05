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

function setup() {
    const timers = new Map(), calls = [], host = new Element('main');
    let id = 0;
    const data = {active_run: null, areas: [], file_areas: [], service: {mode: 'AUTO_SYNC', running: true, interval_minutes: 60},
        metrics: {records: {}, polling: {runs: 0, pass: 0, fail: 0, incomplete: 0, skipped: 0, period_days: 30}}};
    const document = {hidden: false, createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag)};
    const context = {window: {}, document, setTimeout(fn, ms) { timers.set(++id, {fn, ms}); return id; }, clearTimeout(key) { timers.delete(key); },
        frappe: {call({method, type, callback}) {
            calls.push({method, type});
            callback({message: method.endsWith('sync_page_state') ? data : method.endsWith('sync_page_pending_relationships')
                ? data.pending_response : {rows: [], start: 0, total: 0}});
            return Promise.resolve();
        }}};
    vm.runInNewContext(readFileSync(new URL('../public/js/hero_migration.js', import.meta.url), 'utf8'), context);
    const main = {empty() { return main; }, addClass() { return main; }, get() { return host; }};
    const view = context.window.hero_integration_migration.mount({main});
    async function tick() {
        assert.equal(timers.size, 1);
        const [key, timer] = [...timers][0]; timers.delete(key); timer.fn(); await flush();
    }
    return {data, document, view, calls, timers, tick, host};
}

test('idle page discovers automatic runs, shows outcomes, and resumes idle refresh', async () => {
    const app = setup(); await flush();
    assert.equal([...app.timers.values()][0].ms, 30000);
    app.data.active_run = {name: 'RUN-1'};
    app.data.metrics.polling = {...app.data.metrics.polling, runs: 1, running: 1, last_status: 'Running'};
    await app.tick();
    assert.equal([...app.timers.values()][0].ms, 5000);
    const text = app.host.querySelectorAll('span').map(el => el.textContent);
    assert.ok(text.includes('Running'));
    const metric = label => app.host.querySelectorAll('div').find(el => el.className === 'hero-sync-metric' && el.children[0].textContent === label).children[1].textContent;
    assert.equal(metric('Runs'), '1');
    assert.equal(metric('Running'), '1');
    assert.equal(metric('Pass'), '0');
    app.data.active_run = null;
    app.data.metrics.polling = {...app.data.metrics.polling, running: 0, pass: 1, last_status: 'Succeeded'};
    await app.tick();
    assert.equal([...app.timers.values()][0].ms, 30000);
    assert.ok(app.host.querySelectorAll('span').some(el => el.textContent === 'Succeeded'));
    assert.equal(metric('Runs'), '1');
    assert.equal(metric('Running'), '0');
    assert.equal(metric('Pass'), '1');
    assert.ok(app.calls.every(call => call.type === 'GET'));
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

test('manual prerequisites expose only the allowed start button', async () => {
    const app = setup(); await flush();
    const area = key => ({key, label: key, action_label: key, button: 'Start', ready: true,
        kind: 'direct', description: '', scope_note: '', blocker: '', scope: {objects: [], object_count: 0, field_count: 0}});
    app.data.areas = [area('contacts'), area('projects')];
    app.data.file_areas = [area('employees')];
    app.data.manual_prerequisite = 'employees';
    await app.tick();
    const action = key => app.host.querySelectorAll('button').find(el => el.dataset.area === key && el.className === 'hero-sync-action');
    assert.equal(action('employees').disabled, undefined);
    assert.equal(action('contacts').disabled, true);
    assert.equal(action('contacts').children[0], 'Waiting');
    assert.ok(app.host.querySelectorAll('p').some(el => el.textContent === 'Migrate Employees first and Contacts second'));
    app.data.manual_prerequisite = 'contacts'; await app.tick();
    assert.equal(action('contacts').disabled, undefined);
    assert.equal(action('employees').disabled, true);
    app.data.manual_prerequisite = ''; await app.tick();
    assert.equal(action('projects').disabled, undefined);
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
