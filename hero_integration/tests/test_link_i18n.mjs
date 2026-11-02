import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../hero_integration/doctype/hero_integration_link/hero_integration_link.js', import.meta.url), 'utf8');
function refreshWith(translate, detail) {
  let handler;
  const frappe = {ui:{form:{on:(_doctype, callbacks) => {handler = callbacks.refresh;}}}, user:{has_role:() => false}};
  vm.runInNewContext(source, {frappe, __:translate});
  const display = {textContent:detail};
  const frm = {doc:{sync_status:'Conflict', conflict_detail:detail}, fields_dict:{conflict_detail:{disp_area:display}}};
  handler(frm);
  return {frm, display};
}
test('native link shows a known translation without changing the stored detail', () => {
  const original = 'Record is already synchronized.';
  const {frm, display} = refreshWith(value => value === original ? 'Der Datensatz ist bereits synchronisiert.' : value, original);
  assert.equal(display.textContent, 'Der Datensatz ist bereits synchronisiert.');
  assert.equal(frm.doc.conflict_detail, original);
});
test('unknown and English diagnostics retain their original display', () => {
  const original = 'Unknown external detail <tag>';
  const {frm, display} = refreshWith(value => value, original);
  assert.equal(display.textContent, original);
  assert.equal(frm.doc.conflict_detail, original);
});
