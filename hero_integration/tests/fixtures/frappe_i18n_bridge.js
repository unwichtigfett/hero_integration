'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

for (const language of input.languages) {
    const messages = input.catalogs[language];
    const window = {};
    const frappe = {_messages: messages, boot: {lang: language, user: {roles: []}}};
    const context = vm.createContext({window, frappe, URLSearchParams, console});
    vm.runInContext(fs.readFileSync(input.frappeTranslate, 'utf8'), context);
    context.__ = window.__;
    for (const file of ['hero_workspace.js', 'hero_tools.js', 'hero_settings.js']) {
        vm.runInContext(fs.readFileSync(input.jsDir + '/' + file, 'utf8'), context, {filename: file});
    }
    const expectedTitle = language === 'en' ? 'HERO Settings' : 'HERO-Einstellungen';
    assert.equal(window.hero_integration_i18n.text('HERO Settings'), expectedTitle);
    const expectedError = language === 'en'
        ? 'Line 2: variable "id" is defined more than once.'
        : 'Zeile 2: Die Variable „id“ ist mehrfach definiert.';
    assert.throws(() => window.hero_workspace_tools.graphqlText.parseVariablesText('id := 1\nid := 2'),
        error => error.message === expectedError);
    const summary = window.hero_integration_settings_ui.packagePreviewSummary({snapshot: 'New',
        mapping_package: {Changed: 2}, read_services: [], settings: []});
    assert.ok(summary.includes(language === 'en' ? 'New' : 'Neu'));
    assert.ok(summary.includes(language === 'en' ? 'Changed 2' : 'Geändert 2'));
}
