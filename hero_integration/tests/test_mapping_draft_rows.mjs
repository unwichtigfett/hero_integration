import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = readFileSync(new URL('../public/js/hero_mapping_editor.js', import.meta.url), 'utf8');
const window = {};
vm.runInNewContext(source, {window});
const {referencedRuleRow, relationshipDescription, uniqueVisibleMappingRows} = window.hero_integration_mapping_editor;

test('a saved parentless rule renders without an active rule document', () => {
    const definition = {field_rules: {draft: {source_path: 'name', target_doctype: 'Project', target_field: 'custom_hero_designation'}}};
    const row = referencedRuleRow(definition, 'draft');
    assert.equal(row.source, 'name');
    assert.equal(row.target, 'Project.custom_hero_designation');
});

test('the saved private rule wins over a different active cached rule', () => {
    const definition = {field_rules: {same: {source_path: 'partner_notes', target_doctype: 'Project', target_field: 'notes'}}};
    const cache = new Map([['same', {source_path: 'wrong', target_doctype: 'Project', target_field: 'old_notes'}]]);
    assert.equal(referencedRuleRow(definition, 'same', cache).target, 'Project.notes');
    assert.equal(referencedRuleRow({}, 'same', cache).target, 'Project.old_notes');
});

test('price rules display their price lists so buying and selling do not look duplicated', () => {
    const definition = {field_rules: {
        buying: {source_path: 'base_price', target_doctype: 'Item Price', target_field: 'price_list_rate', relation: 'Standard Buying'},
        selling: {source_path: 'sales_prices', target_doctype: 'Item Price', target_field: 'price_list_rate', relation: 'Standard Selling'},
    }};
    assert.equal(referencedRuleRow(definition, 'buying').target, 'Item Price.price_list_rate (Standard Buying)');
    assert.equal(referencedRuleRow(definition, 'selling').target, 'Item Price.price_list_rate (Standard Selling)');
});

test('confirmed Company is identified as the own business, not copied from the customer ID', () => {
    const rule = {source_path: 'customer_id', target_doctype: 'Project', target_field: 'company',
        purpose: 'Confirmed Company', transformation: 'Confirmed ERPNext Company'};
    const row = referencedRuleRow({target_operation: 'HeroProjectMatches', field_rules: {company: rule}}, 'company');
    assert.equal(row.source, 'Confirmed ERPNext Company');
    assert.match(relationshipDescription(rule, 'HeroProjectMatches').identity, /customer ID is not copied/);
    assert.equal(referencedRuleRow({target_operation: 'Other', field_rules: {company: rule}}, 'company').source, 'customer_id');
});

test('unresolved active references remain explicit rather than inventing a target', () => {
    const row = referencedRuleRow({}, 'missing');
    assert.equal(row.target, 'Not provided');
    assert.equal(row.source, 'Referenced field (details required)');
});

test('a visible parent or private draft suppresses only its identical standalone rule', () => {
    const parent = {kind: 'graphql', item: {name: 'parent'}, row: {ruleId: 'selling'}};
    const selling = {kind: 'frappe', item: {definition_id: 'selling'}, row: {target: 'Item Price.price_list_rate'}};
    const buying = {kind: 'frappe', item: {definition_id: 'buying'}, row: {target: 'Item Price.price_list_rate'}};
    assert.deepEqual([...uniqueVisibleMappingRows([selling, parent, buying])], [parent, buying]);
});

test('a parent outside the visible page or filters cannot hide its standalone rule', () => {
    const standalone = {kind: 'frappe', item: {definition_id: 'price'}, row: {}};
    const unrelatedParent = {kind: 'graphql', item: {}, row: {ruleId: 'other'}};
    assert.deepEqual([...uniqueVisibleMappingRows([standalone, unrelatedParent])], [standalone, unrelatedParent]);
    assert.deepEqual([...uniqueVisibleMappingRows([standalone])], [standalone]);
});
