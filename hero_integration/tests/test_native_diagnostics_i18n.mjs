import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

test('native diagnostics translate read-only displays without changing records or stacking handlers', () => {
  const handlers = {};
  const frappe = {ui: {form: {on: (doctype, callbacks) => {
    assert.equal(handlers[doctype], undefined);
    handlers[doctype] = callbacks;
  }}}};
  const original = 'Record is already synchronized.';
  const window = {__: value => value === original ? 'Der Datensatz ist bereits synchronisiert.' : value};
  const context = {window, frappe};
  vm.runInNewContext(readFileSync(new URL('../public/js/hero_diagnostics.js', import.meta.url), 'utf8'), context);
  const script = readFileSync(new URL('../public/js/hero_native_diagnostics.js', import.meta.url), 'utf8');
  vm.runInNewContext(script, context);
  vm.runInNewContext(script, context);
  const display = {textContent: original};
  const field = {disp_area: display, value: original, set_disp_area(value) { this.disp_area.textContent = value; }};
  const frm = {doc: {error_message: original}, fields_dict: {error_message: field}};
  handlers['HERO Integration Run'].refresh(frm);
  assert.equal(display.textContent, 'Der Datensatz ist bereits synchronisiert.');
  field.set_disp_area(original);
  assert.equal(display.textContent, 'Der Datensatz ist bereits synchronisiert.');
  assert.equal(frm.doc.error_message, original);
  window.__ = value => value;
  field.set_disp_area(original);
  assert.equal(display.textContent, original);
});
