import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = readFileSync(new URL('../public/js/hero_mapping_editor.js', import.meta.url), 'utf8');
const window = {crypto:{randomUUID}};
vm.runInNewContext(source, {window});
const {DraftState, canSaveDraft} = window.hero_integration_mapping_editor;
const fresh = () => new DraftState({definition_id:'', generation:0, content_revision:0, base_active_revision:0,
    content:{target_operation:'HeroCustomers', mapping_definition_ids:[], field_rules:{}, query_adapter:{version:2}}});
const selection = () => ({source:{field:'first_name', source_type:'string|null', object_type:'Contact', schema_context:'{"path":"first_name"}'},
    target:{doctype:'Customer', field:'customer_name'}, target_type:'Data'});

test('first Query assignment becomes a complete UUID field rule rather than unsupported additional mappings', () => {
    const state = fresh();
    state.creation = selection();
    state.createAdditional({reference:true});
    const [id] = state.content.mapping_definition_ids;
    assert.match(id, /^[0-9a-f]{8}-[0-9a-f-]{27}$/);
    assert.deepEqual(JSON.parse(JSON.stringify(state.content.field_rules[id])), {
        source_path:'first_name', source_type:'string|null', target_doctype:'Customer', target_field:'customer_name',
        purpose:'Field', transformation:'Identity', relation:null, priority:0,
    });
    assert.equal(state.content.field_contexts[id].object_type, 'Contact');
    assert.equal(state.content.field_contexts[id].schema_context, '{"path":"first_name"}');
    assert.equal(state.content.additional_mappings, undefined);
    assert.equal(state.content.query_adapter.transform_options[id].target_type, 'Data');
    assert.equal(state.content.field_rules[id].target_type, undefined);
    assert.equal(state.creation, null);
    assert.equal(state.saved.content.mapping_definition_ids.length, 0);
    state.remove(0);
    assert.equal(state.content.mapping_definition_ids.length, 0);
    state.restore();
    assert.equal(state.content.mapping_definition_ids[0], id);
});

test('new Query assignments prevent duplicate targets and preserve pending selections', () => {
    const state = fresh();
    state.creation = selection(); state.createAdditional({reference:true});
    state.creation = selection(); state.creation.source.field = 'last_name';
    assert.throws(() => state.createAdditional({reference:true}), /already exists/);
    assert.equal(state.content.mapping_definition_ids.length, 1);
    assert.equal(state.creation.source.field, 'last_name');
});

test('incomplete assignments cannot be added to a newly created Query draft', () => {
    const state = fresh();
    state.creation = {source:{field:'id'}, target:{}};
    assert.throws(() => state.createAdditional({reference:true}), /both source and target/);
    assert.equal(state.content.mapping_definition_ids.length, 0);
});

test('an unpersisted mapping remains dirty until the server accepts its first save', () => {
    const state = fresh();
    state.unsavedNew = true;
    assert.equal(state.dirty, true);
    assert.equal(canSaveDraft(state, {definition_id:'', revision:0}), true);
    state.accept({definition_id:'new-server-id', generation:1, content_revision:1, base_active_revision:0, content:state.content});
    assert.equal(state.unsavedNew, false);
    assert.equal(state.dirty, false);
    assert.equal(canSaveDraft(state, {definition_id:'', revision:0}), false);
    assert.equal(canSaveDraft(state, {definition_id:'new-server-id', revision:0}), true);
});
