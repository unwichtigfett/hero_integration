import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

test('polling detail follows Frappe language through field refresh without changing the record', () => {
  const original = 'HERO pagination repeated an offset; monitoring stopped safely.';
  const german = 'Die HERO-Seitennavigation hat einen Offset wiederholt; das Monitoring wurde sicher angehalten.';
  let refresh;
  const frappe = {ui: {form: {on: (_doctype, handlers) => { refresh = handlers.refresh; }}}};
  const window = {__: value => value === original ? german : value};
  const context = {window, frappe};
  vm.runInNewContext(readFileSync(new URL('../public/js/hero_diagnostics.js', import.meta.url), 'utf8'), context);
  vm.runInNewContext(readFileSync(new URL('../hero_integration/doctype/hero_polling_event/hero_polling_event.js', import.meta.url), 'utf8'), context);
  const display = {textContent: original};
  let calls = 0;
  const field = {value: original, disp_area: display, set_disp_area(value) { calls++; this.disp_area.textContent = value; }};
  const frm = {doc: {detail: original}, fields_dict: {detail: field}};
  refresh(frm);
  refresh(frm);
  assert.equal(display.textContent, german);
  field.set_disp_area(original);
  assert.equal(calls, 1);
  assert.equal(display.textContent, german);
  assert.equal(frm.doc.detail, original);
  window.__ = value => value;
  field.set_disp_area(original);
  assert.equal(display.textContent, original);
});
