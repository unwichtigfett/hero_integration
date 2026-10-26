import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';
const window = {};
vm.runInNewContext(readFileSync(new URL('../public/js/hero_mapping_editor.js', import.meta.url), 'utf8'), {window});
const {DraftState, stable} = window.hero_integration_mapping_editor;
test('only the exact in-progress editor download is exempt from navigation interception', () => {
 const {isOwnedExportLink}=window.hero_integration_mapping_editor;
 const owned={href:'blob:https://site/owned',download:'query.json'};
 const other={href:owned.href,download:owned.download};
 assert.equal(isOwnedExportLink(owned,owned),true);
 assert.equal(isOwnedExportLink(other,owned),false);
 assert.equal(isOwnedExportLink(owned,null),false);
 assert.equal(isOwnedExportLink(null,null),false);
});
test('directional adoption requires the current valid preview and idle matching context', () => {
 const {exchangeCanAdopt}=window.hero_integration_mapping_editor;
 const entry={context:{key:'query-context'},preview:{can_save:true}};
 assert.equal(exchangeCanAdopt(entry,entry,'query-context',false),true);
 for (const can_save of [false,undefined,null,1]) {
  const blocked={...entry,preview:{can_save}};
  assert.equal(exchangeCanAdopt(blocked,blocked,'query-context',false),false);
 }
 assert.equal(exchangeCanAdopt(entry,{...entry},'query-context',false),false);
 assert.equal(exchangeCanAdopt(entry,entry,'mutation-context',false),false);
 assert.equal(exchangeCanAdopt(entry,entry,'query-context',true),false);
});
const fixture = () => ({definition_id:'canonical-distinct', generation:2, content_revision:3, base_active_revision:7, state:'OPEN',
 content:{mapping_definition_ids:['a','b','c'],variables:{hidden:['keep',1]},records_path:'things',complex:{other:true}}});
test('choosing a mutation source updates its app, DocType and field in the local draft', () => {
 const state = new DraftState({...fixture(), content:{source:{app:'old-app',doctype:'Old',field:'old', extra:'keep'},target:{operation:'mutate'},additional:{value:0}}});
 state.setFrappeSource('erpnext','Task','subject');
 assert.equal(stable(state.content.source),stable({app:'erpnext',doctype:'Task',field:'subject',extra:'keep'}));
 assert.equal(stable(state.content.target),stable({operation:'mutate'}));
 assert.equal(stable(state.content.additional),stable({value:0}));
 assert.equal(state.dirty,true);
 assert.equal(state.saved.content.source.app,'old-app');
});
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

test('reopened reconciliation uses its verified active revision for a later save',()=>{
 const draft={...fixture(),reconciliation_revision:1,reconciliation_json:'{"version":1,"active_revision":9}'};
 const state=new DraftState(draft);state.set(['records_path'],'later');
 const args=state.saveArgs();
 assert.equal(args.expected_active_revision,9);
 assert.equal(args.expected_reconciliation_revision,1);
 assert.equal(args.reconciliation_json,'');
});

test('directional import save serializes only the server preview CAS states',()=>{
 const preview={items:[
  {definition_id:'query-a',state:'NEW',expected:{generation:0,draft_revision:0,active_revision:9,reconciliation_revision:0}},
  {definition_id:'query-b',state:'REMOVE',expected:{generation:4,draft_revision:7,active_revision:10,reconciliation_revision:2}},
 ]};
 const states=window.hero_integration_mapping_editor.exchangeExpectedStates(preview);
 assert.equal(stable(states),stable([
  {definition_id:'query-a',generation:0,draft_revision:0,active_revision:9,reconciliation_revision:0},
  {definition_id:'query-b',generation:4,draft_revision:7,active_revision:10,reconciliation_revision:2},
 ]));
 assert.equal(stable(window.hero_integration_mapping_editor.exchangeCounts(preview)),stable({NEW:1,REPLACE:0,UNCHANGED:0,REMOVE:1}));
});

test('directional import rejects a malformed preview before any save request can be constructed',()=>{
 assert.throws(()=>window.hero_integration_mapping_editor.exchangeExpectedStates({items:[
  {definition_id:'query-a',expected:{generation:0,draft_revision:0,active_revision:'9',reconciliation_revision:0}},
 ]}),/Invalid directional import preview/);
});

test('tab state storage retains distinct drafts and supplied view state', () => {
 const {MappingTabStates,directionForTab,mappingTabs}=window.hero_integration_mapping_editor;
 const tabs=new MappingTabStates();
 const query=new DraftState({...fixture(),definition_id:'query',content:{records_path:'before'}});
 const mutation=new DraftState({...fixture(),definition_id:'mutation',content:{source:{app:'erpnext',doctype:'Task',field:'subject'}}});
 query.set(['records_path'],'unsaved query'); mutation.setFrappeSource('erpnext','Task','description');
 const queryView={model:query,selected:2,validation:'required',revision:query.saved.content_revision,input:'unsaved query'};
 const mutationView={model:mutation,selected:0,validation:'invalid field',revision:mutation.saved.content_revision,input:'description'};
 tabs.set('Query',queryView); tabs.set('Mutation',mutationView); tabs.set('Binding',{model:null,readOnly:true});
 assert.equal(stable(mappingTabs),stable(['Query','Mutation','Binding']));
 assert.equal(directionForTab('Query'),'QUERY_TO_FRAPPE'); assert.equal(directionForTab('Mutation'),'FRAPPE_TO_MUTATION'); assert.equal(directionForTab('Binding'),null);
 assert.strictEqual(tabs.get('Query'),queryView); assert.strictEqual(tabs.get('Mutation'),mutationView);
 assert.equal(tabs.get('Query').model.content.records_path,'unsaved query');
 assert.equal(tabs.get('Mutation').model.content.source.field,'description');
 assert.equal(tabs.get('Query').selected,2); assert.equal(tabs.get('Mutation').validation,'invalid field');
 assert.equal(tabs.get('Query').revision,3); assert.equal(tabs.get('Binding').readOnly,true);
 assert.equal(tabs.otherDirty('Binding'),true); assert.equal(tabs.otherDirty('Query'),true);
 assert.equal(query.saveArgs().definition_id,'query'); assert.equal(mutation.saveArgs().definition_id,'mutation');
});
test('tab keyboard navigation wraps and supports Home and End', () => {
 const target=window.hero_integration_mapping_editor.tabKeyTarget;
 assert.equal(target('Query','ArrowLeft'),'Binding'); assert.equal(target('Binding','ArrowRight'),'Query');
 assert.equal(target('Mutation','Home'),'Query'); assert.equal(target('Mutation','End'),'Binding');
 assert.equal(target('Query','Tab'),null); assert.equal(target('unknown','ArrowRight'),null);
});

test('binding tab restricts the read view to administrators with a definition', () => {
 const access=window.hero_integration_mapping_editor.bindingAccess;
 assert.equal(access(false,{definition_id:'query'}),'ADMIN_ONLY');
 assert.equal(access(true,null),'SELECT_DEFINITION');
 assert.equal(access(true,{name:'not-a-definition-id'}),'SELECT_DEFINITION');
 assert.equal(access(true,{definition_id:'query'}),'READ_ONLY');
});
