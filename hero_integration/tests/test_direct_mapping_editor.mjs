import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = readFileSync(new URL('../public/js/hero_mapping_editor.js', import.meta.url), 'utf8');
const registry = readFileSync(new URL('../services/direct_operations.py', import.meta.url), 'utf8');
const window = {};
vm.runInNewContext(source, {window});
const {directRuleChoices, mappingMatchesContext, mappingRowCanOpen, DraftState} = window.hero_integration_mapping_editor;
const content = {target_operation: 'HeroProducts', query_adapter: {version: 2}};
const rule = {target_doctype: 'Item Price', target_field: 'price_list_rate'};
const plain = (value) => JSON.parse(JSON.stringify(value));

test('all three direct Query adapters offer exactly the registry HERO resolvers', () => {
    const block = registry.match(/TRANSFORM_TARGETS = \{([\s\S]*?)\n\}/)[1];
    const expected = [...block.matchAll(/^    "([^"]+)":/gm)].map((match) => match[1]);
    assert.equal(expected.length, 6);
    for (const target_operation of ['HeroProducts', 'HeroDocuments', 'HeroPictures']) {
        assert.deepEqual(plain(directRuleChoices({...content, target_operation}, rule).transformation), expected);
    }
});

test('price-list choices apply only to direct Item Price rules and retain canonical values', () => {
    assert.deepEqual(plain(directRuleChoices(content, rule).relation), ['Standard Buying', 'Standard Selling']);
    for (const target_doctype of ['Item', 'File', 'HERO Integration Link']) {
        assert.equal(directRuleChoices(content, {target_doctype}).relation, undefined);
    }
    for (const other of [{}, {...content, target_operation: 'HeroCustomers'},
        {...content, query_adapter: {version: 1}}, {...content, direction: 'FRAPPE_TO_MUTATION'},
        {...content, operation_kind: 'MUTATION'}]) {
        assert.deepEqual(plain(directRuleChoices(other, rule)), {});
    }
});

test('rule selections persist in the personal draft without changing the input content', () => {
    const initial = {...content, field_rules: {price: {...rule, relation: 'Standard Buying', transformation: 'HERO Price'}}};
    const state = new DraftState({content: initial});
    state.set(['field_rules', 'price', 'relation'], 'Standard Selling');
    state.set(['field_rules', 'price', 'transformation'], 'HERO Default Sales Price');
    assert.equal(state.content.field_rules.price.relation, 'Standard Selling');
    assert.equal(state.content.field_rules.price.transformation, 'HERO Default Sales Price');
    assert.equal(initial.field_rules.price.relation, 'Standard Buying');
});

test('built-in rows survive snapshot filtering and still obey operation filtering', () => {
    const builtin = {builtin: 1, operation: 'HeroProducts'};
    assert.equal(mappingMatchesContext(builtin, 'builtin', 'snapshot-A', ''), true);
    assert.equal(mappingMatchesContext(builtin, 'builtin', 'snapshot-B', 'HeroProducts'), true);
    assert.equal(mappingMatchesContext(builtin, 'builtin', 'snapshot-A', 'HeroPictures'), false);
    for (const kind of ['graphql', 'frappe']) {
        const row = {schema_snapshot: 'snapshot-A', operation: 'HeroProducts'};
        assert.equal(mappingMatchesContext(row, kind, 'snapshot-A', 'HeroProducts'), true);
        assert.equal(mappingMatchesContext(row, kind, 'snapshot-B', ''), false);
        assert.equal(mappingMatchesContext(row, kind, '', ''), true);
    }
});

test('built-ins cannot open even if passed with another mapping kind', () => {
    assert.equal(mappingRowCanOpen({}, 'builtin'), false);
    assert.equal(mappingRowCanOpen({builtin: 1}, 'graphql'), false);
    assert.equal(mappingRowCanOpen({name: 'own'}, 'graphql'), true);
    assert.match(source, /async function open\(item, kind, focus = true\) \{\s*if \(!mappingRowCanOpen\(item, kind\) \|\| locked\(\)\) return;/);
    assert.match(source, /if \(!mappingRowCanOpen\(item, kind\)\) \{ say\(format\([\s\S]*?return; \}/);
    assert.match(source, /entries\.filter\([\s\S]*?mappingMatchesContext\(item, kind, snapshotFilter, operation\.val\(\)\)/);
});

test('rendered rule controls wire choices to the stored rule and preserve free-form transformations', () => {
    assert.match(source, /const choices = directRuleChoices\(model\.content, rule\)\[key\]/);
    assert.match(source, /model\.set\(\["field_rules", row\.id, key\], next\)/);
    assert.match(source, /key === "relation" && choices/);
    assert.match(source, /select\.val\(rule\[key\] \|\| ""\)\.on\("change", \(\) => \{ change\(select\.val\(\)\); updateDirty\(\); \}\)/);
    assert.match(source, /const input = field\(label, rule\[key\] \?\? "", change, destination\)/);
    assert.match(source, /input\.attr\("list", id\)/);
});
