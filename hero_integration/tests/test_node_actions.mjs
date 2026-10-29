import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.listeners = {}; this._text = ''; this.className = ''; this.disabled = false; this.title = ''; }
  get classList() { return {add: (name) => { this.className = `${this.className} ${name}`.trim(); }, contains: (name) => this.className.split(/\s+/).includes(name)}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  click() { for (const listener of this.listeners.click || []) listener({preventDefault() {}}); }
  findAll(predicate, out = []) { if (predicate(this)) out.push(this); for (const child of this.children) child.findAll?.(predicate, out); return out; }
}
const document = {createElement: (tag) => new Node(tag), createElementNS: (_ns, tag) => new Node(tag)};

function load() {
  const rpc = [], remembered = [];
  const window = {
    hero_integration_rpc: {call: async (method, args) => { rpc.push({method, args}); return window.rpcResult(method, args); }},
    hero_integration_bookmarks: {remember: (identity) => remembered.push(identity)},
    rpcResult: () => ({}),
  };
  vm.runInNewContext(readFileSync(new URL('../public/js/hero_tools.js', import.meta.url), 'utf8'), {window, document});
  const {Workspace} = window.hero_workspace_tools;
  const ws = Object.create(Workspace.prototype);
  const scope = {connection_id: 'SNAPSHOT', binding_generation: 1, schema_fingerprint: 'fp'};
  Object.assign(ws, {
    typeDetail: new Node('aside'), nodeDetail: new Node('aside'), focusDetail: new Node('aside'), notice: new Node('p'),
    context: {can_write: true}, busy: false, saveButton: null,
    scope: () => scope, scopeKey: (value = scope) => JSON.stringify([value.connection_id, value.binding_generation, value.schema_fingerprint]),
    run: async (action) => action(), serviceUrl: () => '/app/hero', openService: () => false,
  });
  return {ws, scope, rpc, window, remembered, Workspace};
}

const OPERATION = {node: {node_id: 'operation:query:contact', name: 'contact', kind: 'QUERY_OPERATION', description: 'Read one contact'},
  incoming: [{kind: 'ROOT_HAS_OPERATION', source_id: 'root:query', target_id: 'operation:query:contact'}],
  outgoing: [{kind: 'OPERATION_FIELD', source_id: 'operation:query:contact', target_id: 'field:Query.contact'}]};
const ROOT_FIELD = {node: {node_id: 'field:Query.contact', name: 'contact', kind: 'FIELD'},
  incoming: [{kind: 'OPERATION_FIELD', source_id: 'operation:query:contact', target_id: 'field:Query.contact'}], outgoing: []};
const TYPE_FIELD = {node: {node_id: 'field:Contact.email', name: 'email', kind: 'FIELD'}, incoming: [], outgoing: []};
const DRAFT = {operation_kind: 'query', operation_name: 'contact', required_variables: [{name: 'id', type: 'ID!'}],
  payload: {name: 'Contact', operation_name: 'Contact', query: 'query Contact($id: ID!) {\n  contact(id: $id) { __typename }\n}', variables: {}}};

const byClass = (root, name) => root.findAll((node) => node.className.split(/\s+/).includes(name));
const buttonsOf = (target) => byClass(target, 'ht-node-action');

test('graph viewer, pop-up and analyzer show both actions in one group, before description, JSON and relations', () => {
  const {ws, scope} = load();
  for (const target of ['nodeDetail', 'focusDetail', 'typeDetail']) {
    ws.detailContents(ws[target], OPERATION, scope, () => {});
    const children = ws[target].children;
    const groupIndex = children.findIndex((child) => child.className === 'ht-node-actions');
    assert.equal(groupIndex, 2, `${target}: actions directly follow name and node ID`);
    assert.equal(children[groupIndex].attributes['aria-label'], 'Node actions');
    assert.ok(children.slice(groupIndex + 1).some((child) => child.tag === 'pre'), 'JSON comes after the actions');
    assert.equal(byClass(children[groupIndex], 'ht-relation').length, 0, 'relation buttons stay outside the action group');
    const [remember, save] = buttonsOf(children[groupIndex]);
    assert.equal(remember._text, 'Remember for Mapping');
    assert.equal(save._text, 'Save as GraphQL Service');
    assert.ok(remember.className.includes('ht-node-action--remember') && save.className.includes('ht-node-action--service'));
    assert.equal(save.disabled, false);
  }
});

test('a Query root field saves through its operation; other nodes explain why saving is unavailable', async () => {
  const {ws, scope, rpc, window} = load();
  window.rpcResult = () => DRAFT;
  ws.detailContents(ws.nodeDetail, ROOT_FIELD, scope, () => {});
  assert.equal(ws.nodeDetail._heroService.operationId, 'operation:query:contact');
  await buttonsOf(ws.nodeDetail).find((b) => b._text === 'Save as GraphQL Service').listeners.click[0]();
  assert.deepEqual(rpc.map((entry) => [entry.method, entry.args.schema_node_id]), [['get_analyzer_service_draft', 'operation:query:contact']]);

  ws.detailContents(ws.nodeDetail, TYPE_FIELD, scope, () => {});
  const save = buttonsOf(ws.nodeDetail).find((b) => b._text === 'Save as GraphQL Service');
  assert.equal(save.disabled, true);
  assert.match(ws.nodeDetail.textContent, /available for Query and Mutation operations and their root fields/);
});

test('inspector and pop-up keep separate drafts and the Graph Viewer origin is sent', async () => {
  const {ws, scope, rpc, window} = load();
  window.rpcResult = (method) => method === 'get_analyzer_service_draft' ? DRAFT
    : {query: {name: 'Contact', provenance: 'Graph Viewer', service_kind: 'query', query_id: 'Q1'}};
  ws.detailContents(ws.nodeDetail, OPERATION, scope, () => {});
  ws.detailContents(ws.focusDetail, OPERATION, scope, () => {});
  const open = (target) => buttonsOf(target).find((b) => b._text === 'Save as GraphQL Service').listeners.click[0]();
  await open(ws.focusDetail);
  assert.ok(ws.focusDetail._heroService.form, 'pop-up form open');
  assert.equal(ws.nodeDetail._heroService.form, null, 'inspector unaffected');
  assert.match(ws.focusDetail.textContent, /Provenance=Graph Viewer/);
  const saveService = buttonsOf(ws.focusDetail).find((b) => b._text === 'Save Service');
  await saveService.listeners.click[0]();
  const saved = rpc.find((entry) => entry.method === 'save_analyzer_service');
  assert.equal(saved.args.origin, 'Graph Viewer');
  assert.equal(saved.args.schema_node_id, 'operation:query:contact');
  assert.equal('variables' in saved.args.payload, false);
  assert.match(ws.focusDetail.textContent, /Contact saved · Provenance=Graph Viewer/);
  ws.detailContents(ws.typeDetail, OPERATION, scope, () => {});
  assert.equal(ws.typeDetail._heroService.origin, 'API Analyzer');
});

test('a stale draft response never fills a newer node detail', async () => {
  const {ws, scope, window} = load();
  let release;
  window.rpcResult = () => new Promise((resolve) => { release = () => resolve(DRAFT); });
  ws.detailContents(ws.nodeDetail, OPERATION, scope, () => {});
  const pending = buttonsOf(ws.nodeDetail).find((b) => b._text === 'Save as GraphQL Service').listeners.click[0]();
  ws.detailContents(ws.nodeDetail, TYPE_FIELD, scope, () => {});
  release(); await pending;
  assert.equal(ws.nodeDetail._heroService.form, null);
  assert.doesNotMatch(ws.nodeDetail.textContent, /Save Query/);
});

test('Remember for Mapping stores the node identity and shows the snapshot display name', () => {
  const {ws, scope, remembered} = load();
  ws.detailContents(ws.focusDetail, ROOT_FIELD, scope, () => {});
  buttonsOf(ws.focusDetail).find((b) => b._text === 'Remember for Mapping').click();
  assert.equal(JSON.stringify(remembered), JSON.stringify([{snapshot_name: 'SNAPSHOT', snapshot_label: 'SNAPSHOT', schema_fingerprint: 'fp', node_id: 'field:Query.contact', node_name: 'contact', node_kind: 'FIELD'}]));
});

test('the remember dialog shows S-ddmmyy-NN while the UID stays the stored identity', () => {
  const {ws, scope, remembered} = load();
  ws.context = {selected_snapshot_metadata: {display_name: 'S-291026-02'}};
  ws.detailContents(ws.focusDetail, ROOT_FIELD, scope, () => {});
  buttonsOf(ws.focusDetail).find((b) => b._text === 'Remember for Mapping').click();
  assert.equal(remembered.at(-1).snapshot_name, 'SNAPSHOT');
  assert.equal(remembered.at(-1).snapshot_label, 'S-291026-02');
});
