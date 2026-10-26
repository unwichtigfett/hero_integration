import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';
const window = {};
vm.runInNewContext(readFileSync(new URL('../public/js/hero_mapping_editor.js', import.meta.url), 'utf8'), {window});
const {DraftState, stable} = window.hero_integration_mapping_editor;
const fixture = () => ({definition_id:'canonical-distinct', generation:2, content_revision:3, base_active_revision:7, state:'OPEN',
 content:{mapping_definition_ids:['a','b','c'],variables:{hidden:['keep',1]},records_path:'things',complex:{other:true}}});
test('roundtrip retains all parameters and CAS identity', () => {
 const state=new DraftState(fixture()); state.set(['records_path'],'other'); const args=state.saveArgs();
 assert.equal(args.definition_id,'canonical-distinct'); assert.equal(args.expected_generation,2); assert.equal(args.expected_draft_revision,3);
 assert.equal(stable(JSON.parse(args.mapping_json).variables),stable({hidden:['keep',1]}));
 assert.equal(stable(JSON.parse(args.mapping_json).complex),stable({other:true}));
});
test('no-op is content-only; changing then restoring is clean', () => {
 const state=new DraftState(fixture()); assert.equal(state.dirty,false); state.set(['records_path'],'x'); assert.equal(state.dirty,true);
 state.set(['records_path'],'things'); assert.equal(state.dirty,false);
});
test('multiple removals undo separately; failed write keeps input and undo', () => {
 const state=new DraftState(fixture()); state.remove(0); state.remove(0); state.blocked=true;
 assert.equal(state.undo.length,2); state.restore(); assert.equal(stable(state.content.mapping_definition_ids),stable(['b','c']));
 state.restore(); assert.equal(stable(state.content),stable(fixture().content));
});
test('successful save alone clears undo and dirty state', () => {
 const state=new DraftState(fixture()); state.remove(1); state.accept({...fixture(),content:state.content,content_revision:4});
 assert.equal(state.dirty,false); assert.equal(state.undo.length,0);
});
test('uncertain save resolves exact same generation and content without retry', () => {
 const state=new DraftState(fixture()); state.remove(0); const submitted=JSON.parse(JSON.stringify(state.content));
 assert.equal(state.resolve({...fixture(),content_revision:4,content:submitted},submitted),true);
 assert.equal(state.dirty,false);
});
test('CAS mismatch preserves local state and undo; no implicit rebase', () => {
 const state=new DraftState(fixture()); state.remove(0); const before=stable(state.content);
 assert.equal(state.resolve({...fixture(),generation:3},state.content),false);
 assert.equal(state.blocked,true); assert.equal(stable(state.content),before); assert.equal(state.undo.length,1);
});

test('Undo removal retains edits made after the removal', () => {
 const state=new DraftState(fixture()); state.remove(0); state.set(['records_path'],'edited-after-removal'); state.restore();
 assert.equal(state.content.records_path,'edited-after-removal');
 assert.equal(stable(state.content.mapping_definition_ids),stable(['a','b','c']));
});

test('private rule edits survive removal Undo and preserve additional mapping values', () => {
 const draft=fixture(); draft.content.field_rules={a:{source_path:'long.path',target_field:'before'}};
 draft.content.field_rule_bases={a:{revision:1}};
 const state=new DraftState(draft); state.set(['field_rules','a','target_field'],'after'); state.remove(0); state.restore();
 assert.equal(state.content.field_rules.a.target_field,'after'); assert.equal(stable(state.content.variables),stable(draft.content.variables));
});
test('read recovery permits only server enrichment for a newly added rule reference', () => {
 const state=new DraftState(fixture()); state.content.field_rules={a:{target_field:'private'}};
 const submitted=JSON.parse(JSON.stringify(state.content));
 const current={...fixture(),content_revision:4,content:{...submitted,field_rule_bases:{a:{revision:1}}}};
 assert.equal(state.resolve(current,submitted),true);
});
test('rule IDs cannot write through inherited object prototypes', () => {
 const state=new DraftState(fixture()); state.set(['field_rules','__proto__','target_field'],'private');
 assert.equal(Object.hasOwn(state.content.field_rules,'__proto__'),true);
 assert.equal(Object.getPrototypeOf(state.content.field_rules).target_field,undefined);
});

test('authoritative unchanged read keeps local edits and permits only an explicit retry', () => {
 const state=new DraftState(fixture()); state.remove(0); const local=stable(state.content);
 assert.equal(state.resolve(fixture(),state.content),false); assert.equal(state.blocked,false);
 assert.equal(stable(state.content),local); assert.equal(state.undo.length,1);
});

test('browser numeric serialization preserves no-op and strict boolean response matching', () => {
 const draft=fixture(); draft.content.additional_parameter=JSON.parse('{"whole":1.0,"negative_zero":-0.0,"flag":false}');
 const state=new DraftState(draft); const submitted=JSON.parse(state.saveArgs().mapping_json);
 assert.equal(state.dirty,false); assert.equal(state.matches(draft.content,submitted),true);
 const altered=JSON.parse(JSON.stringify(draft.content)); altered.additional_parameter.flag=0;
 assert.equal(state.matches(altered,submitted),false);
});


test('exact integer tokens survive raw HTTP content, no-op, edits and read recovery', () => {
 const raw='{"records_path":"before","extra":{"safe":9007199254740991,"edge":9007199254740992,"positive":9007199254740993,"negative":-9007199254740993,"nested":[9007199254740993]}}';
 const draft={...fixture(),content:JSON.parse(raw),content_json:raw};
 const state=new DraftState(draft); assert.equal(state.dirty,false);
 assert.match(state.saveArgs().mapping_json,/"positive":9007199254740993/);
 assert.match(state.saveArgs().mapping_json,/"negative":-9007199254740993/);
 state.set(['records_path'],'after');const submitted=state.content;
 const saved={...draft,content_revision:4,content:JSON.parse(raw.replace('before','after')),content_json:raw.replace('before','after')};
 assert.equal(state.resolve(saved,submitted),true);assert.equal(state.dirty,false);
 assert.match(state.saveArgs().mapping_json,/"nested":\[9007199254740993\]/);
});
test('re-adding removed identities consumes their Undo without overwriting private edits', () => {
 const d=fixture();d.content.field_rules={a:{target_field:'initial'}};
 const s=new DraftState(d);s.remove(0);s.remove(0);s.set(['field_rules','a','target_field'],'current');
 s.addReference('a');assert.equal(s.undo.length,1);s.restore();
 assert.equal(stable(s.content.mapping_definition_ids),stable(['a','b','c']));
 assert.equal(s.content.field_rules.a.target_field,'current');assert.equal(new Set(s.content.mapping_definition_ids).size,3);
 s.remove(1);s.blocked=true;const before=stable(s.content);assert.equal(s.undo.length,1);assert.equal(stable(s.content),before);
 s.restore();assert.equal(stable(s.content.mapping_definition_ids),stable(['a','b','c']));
 const two=new DraftState({...fixture(),content:{mapping_definition_ids:['a','b']}});two.remove(0);two.remove(0);two.addReference('a');two.restore();assert.equal(stable(two.content.mapping_definition_ids),stable(['a','b']));
});
test('long active rule IDs resolve exactly after multiple prefix pages',async()=>{
 const resolveRule=window.hero_integration_mapping_editor.readActiveRule;
 const prefix='r'.repeat(120), ids=[prefix,prefix+'a',prefix+'a'.repeat(19)+'A',prefix+'a'.repeat(19)+'B'];
 for(const id of ids){const calls=[];const found=await resolveRule(id,async(args)=>{calls.push(args);assert.ok(args.search.length<=120);return args.start===0?{frappe_mappings:[{definition_id:prefix+'wrong'}],pagination:{frappe_total:101}}:{frappe_mappings:[{definition_id:id,target_field:id.slice(-1)}],pagination:{frappe_total:101}};});assert.equal(found.definition_id,id);assert.equal(calls.length,2);assert.equal(calls[1].start,100);}
});

test('reconciliation apply remains local, carries exact JSON proof to save, and edits invalidate it',()=>{
 const raw='{"records_path":"before","extra":{"positive":9007199254740993}}';
 const draft={...fixture(),content:JSON.parse(raw),content_json:raw,reconciliation_revision:0};
 const state=new DraftState(draft);
 const proof='{"version":1,"candidate":{"extra":{"positive":9007199254740993}}}';
 const result=raw.replace('before','after');
 state.applyReconciliation(result,proof,9,0);
 const args=state.saveArgs();
 assert.equal(args.expected_active_revision,9);
 assert.equal(args.expected_reconciliation_revision,0);
 assert.equal(args.reconciliation_json,proof);
 assert.match(args.mapping_json,/"positive":9007199254740993/);
 state.set(['records_path'],'later');
 const changed=state.saveArgs();
 assert.equal(changed.reconciliation_json,'');
 assert.equal(changed.expected_active_revision,7);
});
