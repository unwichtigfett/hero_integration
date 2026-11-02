import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../public/js/hero_api_summary.js', import.meta.url), 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.textContent = ''; this.value = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener() {}
  remove() {}
  get options() { return this.children; }
  find(predicate) { if (predicate(this)) return this; for (const child of this.children) { const found = child.find(predicate); if (found) return found; } }
}
const translations = {'{0} types':'{0} Typen', 'Known error':'Bekannter Fehler'};
function setup(api) {
  const window = {hero_integration_platform:{api, url:() => '/app/hero-integration'},
    hero_integration_i18n:{text:value => translations[value] || String(value)}};
  vm.runInNewContext(source, {window, document:{createElement:tag => new Element(tag)}});
  return window.hero_integration_api_summary;
}
test('schema count follows the active Frappe language', async () => {
  const workspace = new Element('main');
  const api = async method => method === 'workspace_context'
    ? {selected_snapshot:'SNAP-1', snapshots:[{name:'SNAP-1', usable:true, coverage:{type_count:42}}]}
    : {enabled:true};
  await setup(api).mount(workspace);
  assert.ok(workspace.find(item => item.tag === 'option' && item.textContent === 'SNAP-1 · 42 Typen'));
});
test('stored API error message is translated when a catalog entry exists', async () => {
  const workspace = new Element('main');
  await setup(async () => { throw new Error('Known error'); }).mount(workspace);
  assert.ok(workspace.find(item => item.textContent === 'Bekannter Fehler' && item.dataset.state === 'error'));
});
