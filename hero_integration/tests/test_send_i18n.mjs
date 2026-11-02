import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

class Element {
  constructor() { this.children = []; this.value = ''; }
  empty() { this.children = []; return this; }
  append(...children) { this.children.push(...children); return this; }
  text(value) { this.value = String(value); return this; }
  prop() { return this; }
}
test('customer send preview and success translate field labels without changing values', async () => {
  const source = readFileSync(new URL('../public/js/hero_customer_send.js', import.meta.url), 'utf8');
  const messages = {'First name':'Vorname', 'Sent to HERO: {0}':'An HERO gesendet: {0}'};
  const translate = (value, args) => (messages[value] || value).replace(/\{(\d+)\}/g, (_, index) => args[index]);
  const calls = [], wrapper = new Element(); let controller, dialog, button, alert;
  class Dialog {
    constructor(options) { this.options = options; this.fields_dict = {summary:{$wrapper:wrapper}}; dialog = this; }
    get_primary_btn() { return new Element(); }
    show() {}
    hide() {}
  }
  const frappe = {user:{has_role:() => true}, ui:{Dialog, form:{on:(_doctype, value) => {controller = value;}}},
    call:({method,args}) => { calls.push({method,args}); return Promise.resolve({message:method.endsWith('_preview')
      ? {sending_enabled:true, changes:[{label:'First name', hero:'Old', new:'New'}], conflicts:[], can_send:true, fingerprint:'fp'}
      : {changes:['First name']}}); }, show_alert:value => {alert=value.message;}};
  vm.runInNewContext(source, {frappe, __:translate, $:() => new Element()});
  const frm = {doc:{name:'CUST-1',custom_hero_source_id:'123'}, is_new:() => false, is_dirty:() => false,
    add_custom_button:(_label, action) => {button=action;}, reload_doc() {}};
  controller.refresh(frm); button();
  await new Promise(resolve => setImmediate(resolve));
  const cells = wrapper.children[0].children[1].children;
  assert.equal(cells[0].value, 'Vorname');
  assert.equal(cells[1].value, 'Old');
  assert.equal(cells[2].value, 'New');
  dialog.options.primary_action();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(alert, 'An HERO gesendet: Vorname');
  assert.equal(calls[1].args.fingerprint, 'fp');
  assert.equal(calls[1].args.confirmed, 1);
});

test('writeback confirmation translates the effect while preserving its target and value', async () => {
  const source = readFileSync(new URL('../public/js/hero_writeback.js', import.meta.url), 'utf8');
  const effect = 'One explicitly confirmed bound-field HERO mutation will be sent once; no automatic retry is performed.';
  const messages = {[effect]:'Eine bestätigte HERO-Mutation wird einmal gesendet.'};
  let controller, button; const wrapper = new Element();
  class Widget extends Element {
    find() { return this; }
    first() { return this; }
    remove() { return this; }
    on(_name, handler) { button = handler; return this; }
    get length() { return 1; }
  }
  class Dialog {
    constructor() { this.fields_dict = {preview:{$wrapper:wrapper}}; }
    set_primary_action() {}
    show() {}
  }
  const frappe = {ui:{Dialog, form:{on:(_doctype, value) => {controller = value;}}}, call:({method}) => Promise.resolve({message:
    method.endsWith('writeback_available') ? {actions:[{field:'subject',mapping_name:'MAP-1'}]} :
      {effect_summary:effect, target:{source_instance:'main',external_id:'123'},source:{doctype:'Task',field:'subject',value:'Value'},
        hero:{operation:'change',field:'title',value:'Old'},can_execute:true}})};
  vm.runInNewContext(source, {frappe, __:value => messages[value] || value, $:() => new Widget()});
  const fieldWrapper = new Widget();
  controller.refresh({doctype:'Project',doc:{name:'PRJ-1'},is_new:() => false,
    $wrapper:new Widget(),get_field:() => ({$wrapper:fieldWrapper})});
  await new Promise(resolve => setImmediate(resolve));
  button();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(wrapper.children[0].value, 'Eine bestätigte HERO-Mutation wird einmal gesendet.');
  assert.equal(wrapper.children[1].children[1].value, 'main:123');
  assert.equal(wrapper.children[1].children[5].value, 'Value');
});
