import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = readFileSync(new URL('../public/js/hero_mapping_editor.js', import.meta.url), 'utf8');

function load(messages = {}) {
  const window = {hero_integration_i18n: {text: (value) => messages[value] || value}};
  vm.runInNewContext(source, {window});
  return window.hero_integration_mapping_editor.operationLabel;
}

test('operations show their area name without the Hero prefix', () => {
  const label = load();
  assert.deepEqual(['HeroProducts', 'HeroDocuments', 'HeroPictures', 'HeroCustomers', 'HeroProjectMatches', 'HeroTasks',
    'HeroCalendarEvents'].map(label), ['Products', 'Documents', 'Pictures', 'Customers', 'Projects', 'Tasks', 'Calendar']);
});

test('area names are translated and unknown ids stay visible', () => {
  const label = load({Products: 'Artikel', Documents: 'Dokumente', Pictures: 'Bilder'});
  assert.equal(label('HeroProducts'), 'Artikel');
  assert.equal(label('HeroPictures'), 'Bilder');
  assert.equal(label('contacts'), 'contacts');
  assert.equal(label(''), '');
});
