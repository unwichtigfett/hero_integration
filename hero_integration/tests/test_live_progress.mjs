import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../public/js/hero_migration.js', import.meta.url), 'utf8');

test('fallback discovers and follows a run, then removes its timer on close', async () => {
    const window = {}, pending = new Map(), rendered = [], requests = [];
    let seq = 0;
    vm.runInNewContext(source, {window, setTimeout(fn, ms) { pending.set(++seq, {fn, ms}); return seq; },
        clearTimeout(id) { pending.delete(id); }});
    let release;
    const stop = window.hero_integration_migration.watchProgress({
        visible: () => true, realtime: {doctype_subscribe: assert.fail}, fetchRows: async names => {
            requests.push([...names]);
            if (requests.length === 3) await new Promise(resolve => { release = resolve; });
            return [{name: 'run-A', active: requests.length === 1}];
        },
        render: rows => rendered.push(rows[0].active), failed: assert.fail
    });
    await new Promise(setImmediate);
    assert.equal([...pending.values()][0].ms, 5000);
    await [...pending.values()][0].fn();
    const delayed = [...pending.values()][0].fn();
    stop(); release(); await delayed;
    assert.deepEqual(requests, [[], ['run-A'], []]);
    assert.deepEqual(rendered,[true,false]);
    assert.equal(pending.size,0);
});

test('native notifications coalesce; unsubscribe removes only this listener', async () => {
    const window = {}, handlers = new Map(), timers = new Map(), rooms = new Set();
    let seq = 0;
    vm.runInNewContext(source, {window, setTimeout(fn, ms) { timers.set(++seq,{fn,ms}); return seq; },
        clearTimeout(id) { timers.delete(id); }});
    const realtime = {socket: {}, on: (k, cb) => handlers.set(k, cb), off: (k, cb) => {
        assert.equal(handlers.get(k),cb); handlers.delete(k); },
        doctype_subscribe: t => rooms.add(t), doctype_unsubscribe: t => rooms.delete(t)};
    const stop = window.hero_integration_migration.watchProgress({fetchRows: async () => [],
        render() {}, failed: assert.fail, visible: () => true, realtime});
    await new Promise(setImmediate);
    handlers.get('list_update')({doctype:'HERO Integration Run',name:'run-A'});
    handlers.get('list_update')({doctype:'HERO Sync Run Ledger',name:'run-B'});
    assert.equal(timers.size,1);
    assert.ok([...timers.values()][0].ms <= 1000);
    stop(); assert.equal(rooms.size + timers.size + handlers.size, 0);
});
