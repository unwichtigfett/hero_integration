import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../public/js/hero_overview.js', import.meta.url), 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.handlers = {}; this.textContent = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  querySelectorAll() { return []; }
  remove() {}
  get lastElementChild() { return this.children.at(-1); }
  find(predicate) { if (predicate(this)) return this; for (const child of this.children) { const found = child.find(predicate); if (found) return found; } }
}
test('schema refresh error detail uses the current language', async () => {
  const messages = {'Known error':'Bekannter Fehler', 'Failed to refresh HERO schema.':'HERO-Schema konnte nicht aktualisiert werden.'};
  const api = async method => {
    if (method === 'workspace_settings_get') return {enabled:true, api_token_configured:true, polling:{mode:'OFF'}};
    if (method === 'workspace_context') return {can_write:true, schema_refresh_available:true};
    if (method === 'workspace_schema_refresh') throw new Error('Known error');
    throw new Error('Unexpected method');
  };
  const window = {hero_integration_platform:{api, url:() => '/app/hero-integration'},
    hero_integration_i18n:{text:value => messages[value] || String(value)}};
  vm.runInNewContext(source, {window, URLSearchParams, location:{search:''}, document:{createElement:tag => new Element(tag)}});
  const workspace = new Element('main');
  await window.hero_integration_overview.mount(workspace);
  await workspace.find(item => item.tag === 'button' && item.textContent === 'Refresh Schema').handlers.click();
  assert.ok(workspace.find(item => item.textContent === 'HERO-Schema konnte nicht aktualisiert werden. Bekannter Fehler'));
});
