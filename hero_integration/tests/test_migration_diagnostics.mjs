import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const translations = {
  'Field conflict requires review: {0}.': 'Feldkonflikt erfordert Prüfung: {0}.',
  'Task fields require review: {0}': 'Task-Felder erfordern Prüfung: {0}',
  'Another HERO run is active: {0}.': 'Ein anderer HERO-Lauf ist aktiv: {0}.',
  'Recheck requested: {0}': 'Erneute Prüfung angefordert: {0}',
  'A known static error.': 'Ein bekannter Fehler.',
};
const window = {hero_integration_i18n: {text: value => translations[value] || value}};
vm.runInNewContext(readFileSync(new URL('../public/js/hero_migration.js', import.meta.url), 'utf8'), {window});
const {diagnostic} = window.hero_integration_migration;

test('stored diagnostics translate at display time while preserving identifiers', () => {
  assert.equal(diagnostic('Field conflict requires review: custom_id, name.'), 'Feldkonflikt erfordert Prüfung: custom_id, name.');
  assert.equal(diagnostic('Recheck requested: Task fields require review: owner'), 'Erneute Prüfung angefordert: Task-Felder erfordern Prüfung: owner');
  assert.equal(diagnostic('Another HERO run is active: RUN-007.'), 'Ein anderer HERO-Lauf ist aktiv: RUN-007.');
  assert.equal(diagnostic('A known static error.'), 'Ein bekannter Fehler.');
  assert.equal(diagnostic('External system: arbitrary text'), 'External system: arbitrary text');
});

test('the same stored English message follows the active language', () => {
  window.hero_integration_i18n.text = value => value;
  assert.equal(diagnostic('Field conflict requires review: name.'), 'Field conflict requires review: name.');
});
