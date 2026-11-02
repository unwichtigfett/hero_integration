import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const diagnostics = readFileSync(new URL('../public/js/hero_diagnostics.js', import.meta.url), 'utf8');
const source = readFileSync(new URL('../hero_integration/doctype/hero_integration_link/hero_integration_link.js', import.meta.url), 'utf8');
function refreshWith(translate, detail) {
  let handler;
  const frappe = {ui:{form:{on:(_doctype, callbacks) => {handler = callbacks.refresh;}}}, user:{has_role:() => false}};
  const window = {__:translate};
  vm.runInNewContext(diagnostics, {window});
  vm.runInNewContext(source, {window, frappe, __:translate});
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


test('native link translates dynamic diagnostics and keeps the canonical record unchanged', () => {
  const original = 'Field conflict requires review: customer_name.';
  const translate = value => value === 'Field conflict requires review: {0}.' ? 'Feldkonflikt erfordert Prüfung: {0}.' : value;
  const {frm, display} = refreshWith(translate, original);
  assert.equal(display.textContent, 'Feldkonflikt erfordert Prüfung: customer_name.');
  assert.equal(frm.doc.conflict_detail, original);
});

test('native link falls back to Frappe translation if the shared module is unavailable', () => {
  let handler;
  const frappe = {ui:{form:{on:(_doctype, callbacks) => {handler = callbacks.refresh;}}}, user:{has_role:() => false}};
  vm.runInNewContext(source, {window:{}, frappe, __:value => value === 'Known' ? 'Bekannt' : value});
  const display = {textContent:'Known'};
  const frm = {doc:{sync_status:'Conflict', conflict_detail:'Known'}, fields_dict:{conflict_detail:{disp_area:display}}};
  handler(frm);
  assert.equal(display.textContent, 'Bekannt');
  assert.equal(frm.doc.conflict_detail, 'Known');
});

test('field-only refresh retains translation without stacking wrappers or changing the document', () => {
  let handler;
  const frappe = {ui:{form:{on:(_doctype, callbacks) => {handler = callbacks.refresh;}}}, user:{has_role:() => false}};
  const window = {__:value => value === 'Known' ? 'Bekannt' : value};
  vm.runInNewContext(diagnostics, {window});
  vm.runInNewContext(source, {window, frappe, __:window.__});
  const display = {textContent:'Known'};
  let originalCalls = 0;
  const field = {disp_area:display, value:'Known', set_disp_area(value) { originalCalls++; this.disp_area.textContent = value; return 'done'; }};
  const frm = {doc:{sync_status:'Conflict', conflict_detail:'Known'}, fields_dict:{conflict_detail:field}};
  handler(frm);
  handler(frm);
  assert.equal(field.set_disp_area('Known'), 'done');
  assert.equal(originalCalls, 1);
  assert.equal(display.textContent, 'Bekannt');
  assert.equal(frm.doc.conflict_detail, 'Known');
});
