import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const extension = readFileSync(new URL('../public/js/helpdesk_desk_return.js', import.meta.url), 'utf8');

test('only the Desk entry is redirected, once, with its target kept', () => {
    const opened = [];
    const window = {open: (...args) => { opened.push(args); return 'opened'; }};
    const context = vm.createContext({window});
    vm.runInContext(extension, context);
    vm.runInContext(extension, context);
    assert.equal(window.open('/desk/helpdesk', '_self'), 'opened');
    window.open('/crm', '_self');
    window.open('/desk/helpdesk/tickets', '_blank');
    assert.deepEqual(opened, [['/desk', '_self'], ['/crm', '_self'], ['/desk/helpdesk/tickets', '_blank']]);
});
