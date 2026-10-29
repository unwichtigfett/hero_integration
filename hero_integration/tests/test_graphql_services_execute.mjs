import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

// Minimal DOM: enough for text-only rendering; there is no innerHTML at all.
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.listeners = {}; this._text = ''; this.disabled = false; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  get childElementCount() { return this.children.length; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  click() { for (const listener of this.listeners.click || []) listener({preventDefault() {}}); }
  find(predicate) { if (predicate(this)) return this; for (const child of this.children) { const hit = child.find?.(predicate); if (hit) return hit; } return null; }
}
const document = {createElement: (tag) => new Node(tag), createElementNS: (_ns, tag) => new Node(tag)};

function load() {
  const rpc = [], dialogs = [], copied = [];
  const window = {
    hero_integration_rpc: {call: async (method, args, options) => { rpc.push({method, args, options}); return window.rpcResult(method, args); }},
    rpcResult: () => ({}),
    confirm: () => true,
  };
  class Dialog {
    constructor(options) { this.options = options; this.shown = false; const wrapper = new Node('div'); this.fields_dict = {result: {$wrapper: [wrapper]}}; this.$wrapper = {classes: [], addClass: (c) => this.$wrapper.classes.push(c)}; dialogs.push(this); }
    show() { this.shown = true; }
    hide() { this.shown = false; }
  }
  const frappe = {ui: {Dialog}, utils: {copy_to_clipboard: (text) => copied.push(text)}};
  window.frappe = frappe;
  vm.runInNewContext(readFileSync(new URL('../public/js/hero_tools.js', import.meta.url), 'utf8'), {window, document, frappe});
  return {window, rpc, dialogs, copied, Workspace: window.hero_workspace_tools.Workspace};
}

function editorState(Workspace, overrides = {}) {
  const ws = Object.create(Workspace.prototype);
  const field = (value = '') => ({value});
  Object.assign(ws, {
    serviceKind: 'query', schema: {}, queryEpoch: 1, name: field('Contact'), category: field('Custom Queries'), description: field(''),
    operation: field('Contact'), variables: field('id := "7"'), queryOutput: new Node('pre'), validation: null,
    editor: {text: 'query Contact($id: ID!) { contact(id: $id) { id } }', getValue() { return this.text; }},
    serviceScope: () => ({snapshot: 'SNAPSHOT'}), serviceLabel: () => 'Query', updateActions() {},
    query: {query_id: 'Q1', revision: 3, status: 'Validated'},
  }, overrides);
  ws.base = ws.draftString();
  return ws;
}

const OK = {status: 'OK', operation: 'Contact', revision: 3, schema_fingerprint: 'abcdef', size_bytes: 40,
  data: {contact: {id: '7', name: 'Photovoltaik und Speicher', power_kwp: 14.5}}};

test('a saved, unchanged query executes that revision once with converted JSON variables', async () => {
  const {window, rpc, dialogs, Workspace} = load();
  window.rpcResult = (method) => method === 'execute_service' ? OK : {};
  const ws = editorState(Workspace);
  await ws.executeQuery();
  assert.deepEqual(rpc.map((entry) => entry.method), ['execute_service']);
  assert.equal(JSON.stringify(rpc[0].args), JSON.stringify({snapshot: 'SNAPSHOT', query_id: 'Q1', expected_revision: 3, variables: {id: '7'}}));
  assert.equal(dialogs.length, 1);
  assert.equal(dialogs[0].options.title, 'GraphQL Query Result');
  assert.equal(ws.editor.text, 'query Contact($id: ID!) { contact(id: $id) { id } }', 'the editor content is unchanged');
});

test('changing only variables does not mark the service as unsaved', () => {
  const {Workspace} = load();
  const ws = editorState(Workspace);
  ws.variables.value = 'id := "8"\nlang := "de"';
  assert.equal(ws.dirty, false);
  ws.editor.text += ' ';
  ws.editor.text = ws.editor.text.trim() + '\n# changed';
  assert.equal(ws.dirty, true);
});

test('unsaved changes never run the older saved revision silently', async () => {
  const {window, rpc, Workspace} = load();
  const ws = editorState(Workspace);
  ws.editor.text = 'query Contact($id: ID!) { contact(id: $id) { id email } }';
  window.confirm = () => false;
  let saves = 0;
  ws.saveQuery = async () => { saves += 1; };
  await ws.executeQuery();
  assert.equal(saves, 0);
  assert.deepEqual(rpc, [], 'declining the save request executes nothing');

  window.confirm = () => true;
  ws.saveQuery = async () => { saves += 1; ws.query = {query_id: 'Q1', revision: 4, status: 'Draft'}; ws.base = ws.draftString(); };
  window.rpcResult = (method) => method === 'validate_saved_query' ? {query: {status: 'Validated'}} : OK;
  await ws.executeQuery();
  assert.equal(saves, 1);
  assert.deepEqual(rpc.map((entry) => [entry.method, entry.args.expected_revision]), [['validate_saved_query', 4], ['execute_service', 4]]);
});

test('a save that leaves the editor changed aborts before any request', async () => {
  const {rpc, Workspace} = load();
  const ws = editorState(Workspace, {query: null});
  ws.editor.text = 'query Contact($id: ID!) { contact(id: $id) { id email } }';
  ws.saveQuery = async () => { ws.query = {query_id: 'Q9', revision: 1, status: 'Draft'}; /* base not updated: still dirty */ };
  await assert.rejects(ws.executeQuery(), /changed while saving. Nothing was executed/);
  assert.deepEqual(rpc, []);
});

test('an unvalidatable saved query is not executed', async () => {
  const {window, rpc, Workspace} = load();
  const ws = editorState(Workspace, {query: {query_id: 'Q1', revision: 3, status: 'Draft'}});
  window.rpcResult = () => ({});
  await assert.rejects(ws.executeQuery(), /could not be validated. Nothing was executed/);
  assert.deepEqual(rpc.map((entry) => entry.method), ['validate_saved_query']);
});

test('invalid variables and mutations are rejected before any request', async () => {
  const {rpc, Workspace} = load();
  const ws = editorState(Workspace);
  ws.variables.value = 'id := seven';
  await assert.rejects(ws.executeQuery(), /Line 1: invalid value for "id"/);
  const mutation = editorState(Workspace, {serviceKind: 'mutation'});
  await assert.rejects(mutation.executeQuery(), /HERO_MUTATION_EXECUTION_DISABLED/);
  assert.equal(mutation.canExecuteQuery(), false);
  assert.deepEqual(rpc, []);
});

test('result dialog: readable default, raw JSON toggle and copy of the visible view', () => {
  const {dialogs, copied, Workspace} = load();
  const ws = editorState(Workspace);
  ws.renderExecution(OK);
  const root = dialogs[0].fields_dict.result.$wrapper[0];
  const output = root.find((node) => node.attributes['aria-label'] === 'GraphQL Query Result');
  assert.equal(output.textContent, 'contact.id = "7"\ncontact.name = "Photovoltaik und Speicher"\ncontact.power_kwp = 14.5');
  const byText = (text) => root.find((node) => node.tag === 'button' && node._text === text);
  byText('Copy Result').click();
  assert.equal(copied.at(-1), output.textContent);
  byText('Raw JSON').click();
  assert.equal(output.textContent, JSON.stringify(OK.data, null, 2));
  byText('Copy Result').click();
  assert.equal(copied.at(-1), JSON.stringify(OK.data, null, 2));
  assert.match(root.textContent, /Delivered: contact: object/);
  assert.ok(dialogs[0].$wrapper.classes.join(' ').includes('admin-compat-modal'));
  assert.equal(dialogs[0].options.primary_action_label, 'Close');
});

test('result dialog: coloured readable view copies exactly the plain text', () => {
  const {dialogs, copied, Workspace} = load();
  editorState(Workspace).renderExecution(OK);
  const root = dialogs[0].fields_dict.result.$wrapper[0];
  const output = root.find((node) => node.attributes['aria-label'] === 'GraphQL Query Result');
  const classes = output.children.map((child) => child.className);
  assert.ok(classes.includes('ht-tok ht-tok-key') && classes.includes('ht-tok ht-tok-string') && classes.includes('ht-tok ht-tok-number'));
  root.find((node) => node.tag === 'button' && node._text === 'Copy Result').click();
  assert.equal(copied.at(-1), 'contact.id = "7"\ncontact.name = "Photovoltaik und Speicher"\ncontact.power_kwp = 14.5');
});

test('result dialog: very large results stay complete but without colours', () => {
  const {dialogs, Workspace} = load();
  const items = Array.from({length: 20001}, (_, index) => index);
  editorState(Workspace).renderExecution({...OK, data: {items}});
  const root = dialogs[0].fields_dict.result.$wrapper[0];
  const output = root.find((node) => node.attributes['aria-label'] === 'GraphQL Query Result');
  assert.equal(output.children.length, 0);
  assert.equal(output.textContent.split('\n').length, 20001);
  assert.match(root.textContent, /Colours are turned off for this large result; the text is complete/);
});

test('result dialog: partial data stays visible and GraphQL errors are flagged', () => {
  const {dialogs, Workspace} = load();
  const ws = editorState(Workspace);
  ws.renderExecution({...OK, status: 'PARTIAL', error_kind: 'GRAPHQL', error_code: 'GRAPHQL_ERRORS', message: 'HERO GraphQL rejected the request.',
    graphql_errors: ['email: not allowed'], data: {contact: {id: '7', email: null}}});
  const root = dialogs[0].fields_dict.result.$wrapper[0];
  assert.match(root.textContent, /Partial HERO response with GraphQL errors/);
  assert.match(root.textContent, /GraphQL errors.*email: not allowed/s);
  assert.match(root.textContent, /contact\.id = "7"\ncontact\.email = null/);
});

test('result dialog: transport errors are never shown as an empty success', () => {
  const {dialogs, Workspace} = load();
  const ws = editorState(Workspace);
  ws.renderExecution({status: 'ERROR', error_kind: 'TRANSPORT', error_code: 'HTTP_503', message: 'HERO request failed.', graphql_errors: [], revision: 3});
  const root = dialogs[0].fields_dict.result.$wrapper[0];
  assert.match(root.textContent, /HTTP\/transport error/);
  assert.match(root.textContent, /No data was delivered\./);
  assert.doesNotMatch(root.textContent, /HERO response \(OK/);
  ws.renderExecution({status: 'TOO_LARGE', size_bytes: 2000001, limit_bytes: 1000000, message: 'too large', revision: 3});
  assert.equal(dialogs[0].shown, false, 'a new result replaces the previous dialog');
  assert.match(dialogs[1].fields_dict.result.$wrapper[0].textContent, /Nothing was truncated silently/);
});
