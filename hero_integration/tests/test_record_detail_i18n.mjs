import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

test('editable record detail shows a separate translation and preserves the editable value', () => {
  let handlers;
  const frappe = {ui: {form: {on: (_doctype, callbacks) => { handlers = callbacks; }}}};
  const source = 'Field conflict requires review: <field>.';
  const text = 'Feldkonflikt erfordert Prüfung: <field>.';
  const window = {hero_integration_diagnostics: {translate: value => value === source ? text : value}};
  const document = {createElement: () => ({textContent: '', hidden: false})};
  const __ = (template, values) => template.replace('{0}', values[0]);
  vm.runInNewContext(readFileSync(new URL('../hero_integration/doctype/hero_integration_record/hero_integration_record.js', import.meta.url), 'utf8'), {window, frappe, document, __});
  const children = [];
  const field = {$wrapper: [{appendChild: child => children.push(child)}]};
  const frm = {doc: {detail: source}, fields_dict: {detail: field}};
  handlers.refresh(frm);
  handlers.refresh(frm);
  assert.equal(children.length, 1);
  assert.equal(children[0].textContent, `Translation: ${text}`);
  assert.equal(children[0].hidden, false);
  assert.equal(frm.doc.detail, source);
  frm.doc.detail = 'Unknown detail';
  handlers.detail(frm);
  assert.equal(children[0].hidden, true);
  assert.equal(frm.doc.detail, 'Unknown detail');
});
