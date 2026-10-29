import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';
const window = {};
vm.runInNewContext(readFileSync(new URL('../public/js/hero_mapping_editor.js', import.meta.url), 'utf8'), {window});
const {DraftState, stable} = window.hero_integration_mapping_editor;
test('new mapping preserves the existing assignment and supports undo', () => {
 window.crypto = {randomUUID: () => '8a6c58ba-08fb-4da4-87e6-684bb1b16e19'};
 const initial = {source: {doctype: 'Customer', field: 'customer_name'}, target: {field: 'full_name'}};
 const state = new DraftState({content: initial});
 state.creation = {source: {doctype:'Customer',field:'alias'}, target:{field:'nr'}};
 assert.equal(state.dirty,true); state.createAdditional();
 assert.equal(state.content.source.field,'customer_name'); assert.equal(state.content.additional_mappings[0].source.field,'alias');
 state.removeAdditional(0); assert.equal(state.content.source.field,'customer_name'); state.restore();
 assert.equal(state.content.additional_mappings[0].target.field,'nr');
});
test('removing and restoring a rule preserves its matching object context', () => {
 const state = new DraftState({content:{mapping_definition_ids:['rule-1'],field_contexts:{'rule-1':{object_type:'Task',schema_context:'context'}}}});
 state.remove(0);
 assert.equal(state.content.mapping_definition_ids.length,0);
 assert.equal(state.content.field_contexts['rule-1'],undefined);
 state.restore();
 assert.equal(state.content.field_contexts['rule-1'].object_type,'Task');
 assert.equal(state.content.field_contexts['rule-1'].schema_context,'context');
 assert.equal(state.dirty,false);
});
test('selecting a HERO field preserves and updates existing source path aliases', () => {
 const state = new DraftState({content:{source:{field:'old',path:'old',source_path:'old'}}});
 state.setHeroSource({path:'project.customer.id',object_type:'Task',schema_context:'schema'});
 assert.equal(state.content.source.field,'project.customer.id');
 assert.equal(state.content.source.path,'project.customer.id');
 assert.equal(state.content.source.source_path,'project.customer.id');
 assert.equal(state.content.source.object_type,'Task');
 assert.equal(state.content.source.schema_context,'schema');
 const plain = new DraftState({content:{source:{field:'old'}}});
 plain.setHeroSource({path:'title',object_type:'Task',schema_context:'schema'});
 assert.equal(Object.hasOwn(plain.content.source,'source_path'),false);
 assert.equal(Object.hasOwn(plain.content.source,'path'),false);
});
test('same operation names retain distinct definition identity and context', () => {
 const {definitionChoice}=window.hero_integration_mapping_editor;
 const first={operation_name:'contacts',name:'HERO-GQL-MAP-1',definition_id:'id-1',kind:'graphql',schema_snapshot:'snapshot-a',revision:2};
 const second={...first,name:'HERO-GQL-MAP-2',definition_id:'id-2'};
 const a=definitionChoice(first,0), b=definitionChoice(second,1);
 assert.equal(a.label,'contacts'); assert.equal(b.label,'contacts');
 assert.equal(a.name,first.name); assert.equal(b.name,second.name);
 assert.equal(a.definition_id,'id-1'); assert.equal(b.definition_id,'id-2');
 assert.match(a.context,/Definition 1.*snapshot-a.*Revision 2/);
 assert.match(b.context,/Definition 2.*snapshot-a.*Revision 2/);
 assert.doesNotMatch(a.context,/id-1/); assert.doesNotMatch(b.context,/id-2/);
 assert.equal(definitionChoice({operation:'project_matches'}).label,'project_matches');
});
test('object field context displays relationship role, list cardinality and arguments', () => {
 const {objectFieldContext}=window.hero_integration_mapping_editor;
 const value=objectFieldContext({fieldtype:'String',relations:[{owner_type:'Project',name:'addresses',target_type:'Address',is_list:true,arguments:[{name:'role',type:'String!'}]}],arguments:[{name:'locale',type:'String'}]});
 assert.match(value,/Project\.addresses → Address \[list\] \(role: String!\)/);
 assert.match(value,/Arguments: locale: String/);
});
test('mutation input type comes from the argument, including GraphQL wrappers', () => {
 const {actionInputType}=window.hero_integration_mapping_editor;
 const operation={arguments:[{name:'contact',type:'CustomerInput!'}, {name:'batch',type:'[CustomerInput!]!'}],input_fields:{contact:[{path:'full_name',input_type:''}]}};
 assert.equal(actionInputType(operation,'contact'),'CustomerInput');
 assert.equal(actionInputType(operation,'batch'),'CustomerInput');
 assert.equal(actionInputType(operation,'missing'),'');
});
test('snapshot pagination reaches later pages and never returns partial results after failure or context change', async () => {
 const {readSnapshotPages}=window.hero_integration_mapping_editor;
 const calls=[]; const data=Array.from({length:205},(_,i)=>({name:`snapshot-${i}`}));
 const all=await readSnapshotPages(async ({start,page_size})=>{calls.push(start);return data.slice(start,start+page_size)},()=>true);
 assert.equal(all.length,205); assert.equal(all[204].name,'snapshot-204'); assert.equal(calls.join(','),'0,100,200');
 await assert.rejects(readSnapshotPages(async ({start})=>{if(start)throw Error('offline');return data.slice(0,100)},()=>true),/offline/);
 await assert.rejects(readSnapshotPages(async ({start})=>start ? [data[0]] : data.slice(0,100),()=>true),/changed or contains invalid identities/);
 let current=true;
 const stopped=await readSnapshotPages(async ()=>{current=false;return data.slice(0,100)},()=>current);
 assert.equal(stopped,null);
});
test('returning to a pending view repeats only its read and retains its exact identity', () => {
 const {resumePendingView, MappingTabStates} = window.hero_integration_mapping_editor;
 const calls=[];
 const handlers={list:()=>calls.push(['list']), detail:(item,kind)=>calls.push(['detail',item,kind])};
 const tabs=new MappingTabStates();
 const item={name:'definition-1',definition_id:'identity-1'};
 tabs.set('Query',{pendingView:{kind:'list'},search:'needle',start:20});
 tabs.set('Mutation',{pendingView:{kind:'detail',item,mappingType:'graphql'}});
 resumePendingView(tabs.get('Query').pendingView,handlers);
 resumePendingView(tabs.get('Mutation').pendingView,handlers);
 assert.deepEqual(calls,[['list'],['detail',item,'graphql']]);
 assert.equal(tabs.get('Query').search,'needle');
 assert.equal(tabs.get('Query').start,20);
 assert.equal(resumePendingView({kind:'binding'},handlers),false);
 assert.equal(resumePendingView(null,handlers),false);
 assert.equal(calls.length,2);
});
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
test('choosing a mutation source keeps app as UI metadata and preserves saved JSON parameters', () => {
 const state = new DraftState({...fixture(), content:{source:{app:'old-app',doctype:'Old',field:'old', extra:'keep'},target:{operation:'mutate'},additional:{value:0}}});
 state.setFrappeSource('erpnext','Task','subject');
 assert.equal(stable(state.content.source),stable({app:'old-app',doctype:'Task',field:'subject',extra:'keep'}));
 assert.equal(stable(state.frappeSelection),stable({side:'source',app:'erpnext',doctype:'Task'}));
 assert.equal(stable(state.content.target),stable({operation:'mutate'}));
 assert.equal(stable(state.content.additional),stable({value:0}));
 assert.equal(state.dirty,true);
 assert.equal(state.saved.content.source.app,'old-app');
});
test('picker app is never introduced into a new mutation save payload', () => {
 const state = new DraftState({...fixture(),content:{source:{doctype:'Customer',field:'name'},target:{operation:'mutate'},complex:{other:true}}});
 state.setFrappeSource('erpnext','Customer','customer_name');
 const content=JSON.parse(state.saveArgs().mapping_json);
 assert.equal(Object.hasOwn(content.source,'app'),false);
 assert.equal(content.source.field,'customer_name');
 assert.equal(stable(content.complex),stable({other:true}));
});
test('reopened admin picker resolves app from current DocType rather than immutable historical app', () => {
 const state=new DraftState({...fixture(),content:{source:{app:'old-app',doctype:'Customer',field:'name'}}});
 const app=window.hero_integration_mapping_editor.selectedPickerApp;
 assert.equal(app(state,'source',state.content.source,true),'');
 state.frappeSelection={side:'source',app:'new-app',doctype:'Customer'};
 assert.equal(app(state,'source',state.content.source,true),'new-app');
 assert.equal(JSON.parse(state.saveArgs().mapping_json).source.app,'old-app');
 assert.equal(app(state,'source',{doctype:'Other',app:'old-app'},true),'');
});
test('draft save remains fail-closed with stale detail or a newer active revision after a saved-draft read', () => {
 const state=new DraftState(fixture());
 const allowed=window.hero_integration_mapping_editor.canSaveDraft;
 const active={definition_id:'canonical-distinct',name:'mapping',revision:7};
 assert.equal(allowed(state,active),true);
 state.detailStale=true;
 assert.equal(state.resolve(fixture(),state.content),true);
 assert.equal(allowed(state,active),false);
 state.detailStale=false;
 assert.equal(state.resolve(fixture(),state.content),true);
 assert.equal(allowed(state,{...active,revision:8}),false);
 state.applyReconciliation(JSON.stringify(state.content),'{}',8,0);
 assert.equal(allowed(state,{...active,revision:8}),true);
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

test('reviewed writeback information is descriptive and fails closed for missing or ambiguous contracts', () => {
 const {reviewedTargetLabel, writebackContractLabel} = window.hero_integration_mapping_editor;
 assert.equal(reviewedTargetLabel({target:{operation:'update_contact',argument:'contact',field:'email'}}),'update_contact / contact.email');
 assert.equal(reviewedTargetLabel({target:{operation:'update_contact',argument:'contact'}}),'');
 assert.match(writebackContractLabel({contracts:[{operation:'update_contact',activation_ready:true}]},'update_contact'),/separate execution gates/);
 assert.equal(writebackContractLabel({contracts:[{operation:'update_contact',activation_ready:false,blocker:'Review required'}]},'update_contact'),'Review required');
 assert.match(writebackContractLabel({contracts:[]},'update_contact'),/unavailable/);
 assert.match(writebackContractLabel({contracts:[{operation:'update_contact',activation_ready:true},{operation:'update_contact',activation_ready:true}]},'update_contact'),/unavailable/);
 assert.match(writebackContractLabel({contracts:[{operation:'update_contact',activation_ready:1}]},'update_contact'),/unavailable/);
});

test('shared writeback inventory preserves draft review metadata and filters direction', () => {
 const {legacyWritebackRows}=window.hero_integration_mapping_editor;
 const draft={name:'legacy-draft',operation_kind:'MUTATION',direction:'FRAPPE_TO_MUTATION',operation_name:'update_contact',status:'Draft',mapping_revision:3,suggestion_reason:'review me',confidence:0};
 const other={...draft,name:'other',direction:'QUERY_TO_FRAPPE'};
 const result=legacyWritebackRows([draft,other]);
 assert.equal(result.length,1); assert.equal(result[0].name,'legacy-draft');
 assert.equal(result[0].suggestion_reason,'review me'); assert.equal(result[0].confidence,0); assert.equal(result[0].mapping_revision,3);
 assert.throws(()=>legacyWritebackRows([{...draft,mapping_revision:'3'}]),/Invalid shared mapping list/);
 assert.throws(()=>legacyWritebackRows(null),/Invalid shared mapping list/);
});
test('shared writeback responses only render for the authorized, connected current snapshot and tab', () => {
 const {legacyWritebackCurrent}=window.hero_integration_mapping_editor;
 const args=[true,'Mutation','Mutation',3,3,7,7,'snapshot-a','snapshot-a',true,false];
 assert.equal(legacyWritebackCurrent(...args),true);
 for (const [index, value] of [[0,false],[2,'Query'],[4,4],[6,8],[8,'snapshot-b'],[9,false],[10,true]]) {
  const changed=[...args];changed[index]=value; assert.equal(legacyWritebackCurrent(...changed),false);
 }
});

test('shared detail rejects changed snapshot, status, revision and identity before displaying content', () => {
 const {legacyWritebackDetailMatches}=window.hero_integration_mapping_editor;
 const row={name:'draft',operation_name:'update_contact',status:'Draft',mapping_revision:3};
 const doc={doctype:'HERO GraphQL Mapping',name:'draft',schema_snapshot:'snap',direction:'FRAPPE_TO_MUTATION',operation_kind:'MUTATION',operation_name:'update_contact',status:'Draft',mapping_revision:3,mapping_json:'{}'};
 assert.equal(legacyWritebackDetailMatches(doc,row,'snap'),true);
 for (const [key,value] of Object.entries({name:'other',schema_snapshot:'other',direction:'QUERY_TO_FRAPPE',operation_kind:'QUERY',operation_name:'other',status:'Active',mapping_revision:4,mapping_json:null})) {
  assert.equal(legacyWritebackDetailMatches({...doc,[key]:value},row,'snap'),false);
 }
});

test('shared inventory retains entries beyond twenty and rejects an invalid response', () => {
 const {legacyWritebackRows}=window.hero_integration_mapping_editor;
 const rows=Array.from({length:45},(_,i)=>({name:`mapping-${i}`,operation_name:'update_contact',direction:'FRAPPE_TO_MUTATION',operation_kind:'MUTATION',status:'Draft',mapping_revision:1}));
 assert.equal(legacyWritebackRows(rows).length,45);
 assert.equal(legacyWritebackRows(rows)[44].name,'mapping-44');
 assert.throws(()=>legacyWritebackRows({items:rows}),/Invalid shared mapping list/);
});

test('legacy candidate preview requires explicit inputs and preserves exact synthetic JSON', () => {
 const {legacyCandidateArgs,legacyCandidateMatches}=window.hero_integration_mapping_editor;
 const raw='{ "nested": {"id": 9007199254740993} }';
 const args=legacyCandidateArgs('snap','HeroCustomers',' id ','Customer',' custom_field ',raw);
 assert.equal(args.source_record_json,raw); assert.equal(args.source_path,'id'); assert.equal(args.target_field,'custom_field');
 const valid={operation:'HeroCustomers',source_path:'id',target_doctype:'Customer',target_field:'custom_field',writes:false,values:{custom_field:1}};
 assert.equal(legacyCandidateMatches(valid,args),true);
 for (const [key,value] of Object.entries({writes:true,operation:'other',source_path:'other',target_doctype:'Other',target_field:'other',values:[]}))
  assert.equal(legacyCandidateMatches({...valid,[key]:value},args),false);
 for (const sample of ['[]','null','42','invalid']) assert.throws(()=>legacyCandidateArgs('s','o','p','d','f',sample));
 assert.throws(()=>legacyCandidateArgs('s','o','p','','f','{}'));
});

test('candidate callback uses raw HTTP JSON tokens for integer and decimal output', async () => {
 const {legacyCandidateArgs,legacyCandidateRpc,legacyCandidatePreviewText}=window.hero_integration_mapping_editor;
 const args=legacyCandidateArgs('snap','HeroCustomers','id','Customer','custom_field','{"id":9007199254740993}');
 const raw='{"message":{"operation":"HeroCustomers","source_path":"id","target_doctype":"Customer","target_field":"custom_field","writes":false,"values":{"integer":9007199254740993,"decimal":9007199254740991.1,"exponent":1.234567890123456789e+30,"small":42}}}';
 const call=async (options)=>{assert.equal(options.method,'hero_integration.api.preview_mapping_candidate'); assert.equal(options.type,'POST'); options.callback(JSON.parse(raw),raw); return JSON.parse(raw);};
 const result=await legacyCandidateRpc(args,call);
 const displayed=legacyCandidatePreviewText(result,args);
 assert.match(displayed, /"integer": 9007199254740993/);
 assert.match(displayed, /"decimal": 9007199254740991\.1/);
 assert.match(displayed, /"exponent": 1\.234567890123456789e\+30/);
 assert.match(displayed, /"small": 42/);
 await assert.rejects(legacyCandidateRpc(args,async ()=>({message:{}})),/cannot preserve preview numbers/);
 const olderWindow={};
 vm.runInNewContext(readFileSync(new URL('../public/js/hero_mapping_editor.js',import.meta.url),'utf8'),{window:olderWindow,JSON:{parse:JSON.parse,stringify:JSON.stringify}});
 assert.throws(()=>olderWindow.hero_integration_mapping_editor.legacyCandidateRawMessage(raw),/cannot preserve preview numbers/);
 const noTokenWindow={};
 const oldParser={rawJSON:JSON.rawJSON,stringify:JSON.stringify,parse:(source,reviver)=>JSON.parse(source,(key,value)=>reviver ? reviver(key,value) : value)};
 vm.runInNewContext(readFileSync(new URL('../public/js/hero_mapping_editor.js',import.meta.url),'utf8'),{window:noTokenWindow,JSON:oldParser});
 assert.throws(()=>noTokenWindow.hero_integration_mapping_editor.legacyCandidateRawMessage(raw),/cannot preserve preview numbers/);
});

test('legacy package adoption requires exact checked schema choices and dependencies', () => {
 const {legacyPackageSelection}=window.hero_integration_mapping_editor;
 const preview={preview_revision:'a'.repeat(64),items:[
  {id:'doctype:a',kind:'schema',state:'New',selectable:true,dependencies:[]},
  {id:'field:a',kind:'schema',state:'New',selectable:true,dependencies:['doctype:a','already']},
  {id:'already',kind:'schema',state:'Unchanged',selectable:false,dependencies:[]},
  {id:'mapping:a',kind:'mapping',state:'New',selectable:true,dependencies:['field:a']},
  {id:'conflict',kind:'schema',state:'Conflict',selectable:false,dependencies:[]}]};
 assert.equal(legacyPackageSelection(preview,['doctype:a','field:a']).map(x=>x.id).join(','),'doctype:a,field:a');
 assert.throws(()=>legacyPackageSelection(preview,['field:a']),/dependencies/);
 assert.throws(()=>legacyPackageSelection(preview,['conflict']),/unavailable/);
 assert.throws(()=>legacyPackageSelection(preview,['doctype:a','doctype:a']));
 assert.throws(()=>legacyPackageSelection({...preview,preview_revision:''},['doctype:a']),/Invalid/);
 assert.throws(()=>legacyPackageSelection({...preview,items:[...preview.items,preview.items[0]]},['doctype:a']),/Invalid/);
});

test('schema-only confirmed adoption request binds exact preview and does not select mappings', () => {
 const {legacyPackageAdoptionArgs}=window.hero_integration_mapping_editor;
 const preview={preview_revision:'b'.repeat(64),items:[{id:'field:one',kind:'schema',state:'New',selectable:true,dependencies:[]},{id:'mapping:one',kind:'mapping',state:'New',selectable:true,dependencies:['field:one']}]};
 const args=legacyPackageAdoptionArgs('snapshot-a','{"schema":{}}',preview,['field:one']);
 assert.equal(args.snapshot_name,'snapshot-a'); assert.equal(args.payload_json,'{"schema":{}}');
 assert.equal(args.preview_revision,'b'.repeat(64)); assert.equal(args.confirmed,true);
 assert.equal(args.selected_ids_json,'["field:one"]');
 assert.throws(()=>legacyPackageAdoptionArgs('snapshot-a','{"schema":{}}',preview,['mapping:one']),/dependencies/);
});

test('only the current actor and exact shared Draft identity are eligible for editing', () => {
 const {sharedWritebackDraftAccess}=window.hero_integration_mapping_editor;
 const row={name:'draft',operation_name:'update_contact',status:'Draft',mapping_revision:3};
 const doc={doctype:'HERO GraphQL Mapping',name:'draft',schema_snapshot:'snap',direction:'FRAPPE_TO_MUTATION',operation_kind:'MUTATION',operation_name:'update_contact',status:'Draft',mapping_revision:3,mapping_json:'{}',definition_id:'def',modified_by:'alice'};
 assert.equal(sharedWritebackDraftAccess(doc,row,'snap','alice'),'EDITABLE');
 assert.equal(sharedWritebackDraftAccess(doc,row,'snap','bob'),'OWNER_CONFLICT');
 assert.equal(sharedWritebackDraftAccess(doc,row,'snap',''),'OWNER_CONFLICT');
 assert.equal(sharedWritebackDraftAccess({...doc,mapping_revision:4},row,'snap','alice'),'STALE');
 assert.equal(sharedWritebackDraftAccess({...doc,status:'Active'},{...row,status:'Active'},'snap','alice'),'NOT_DRAFT');
 assert.equal(sharedWritebackDraftAccess({...doc,definition_id:''},row,'snap','alice'),'INVALID');
});

test('shared reviewed edit is fail-closed against unknown fields and numerical token changes', () => {
 const {reviewedWritebackEditable}=window.hero_integration_mapping_editor;
 const mapping={version:1,source:{doctype:'Customer',field:'customer_name'},target:{operation:'update_contact',argument:'contact',input_type:'CustomerInput',field:'full_name'},transformation:'Trim'};
 const catalog={fields:[{doctype:'Customer',path:'customer_name',selectable:true,target:{...mapping.target,graphql_type:'String'}}]};
 const json=JSON.stringify(mapping);
 assert.equal(reviewedWritebackEditable(json,catalog),true);
 assert.equal(reviewedWritebackEditable(JSON.stringify({...mapping,unknown:9007199254740992}),catalog),false);
 assert.equal(reviewedWritebackEditable(json.replace('"version":1','"version":1.0'),catalog),false);
 assert.equal(reviewedWritebackEditable(json.replace('"version":1','"version":1,"version":1'),catalog),false);
 assert.equal(reviewedWritebackEditable(JSON.stringify({...mapping,target:{...mapping.target,field_path:'full_name'}}),catalog),false);
 assert.equal(reviewedWritebackEditable(json,{fields:[catalog.fields[0],catalog.fields[0]]}),false);
 assert.equal(reviewedWritebackEditable(json,{fields:[]}),false);
});

test('new reviewed shared mapping is projected only from one permitted catalog source target', () => {
 const {buildReviewedWriteback}=window.hero_integration_mapping_editor;
 const target={operation:'update_contact',argument:'contact',input_type:'CustomerInput',field:'full_name',graphql_type:'String'};
 const entry={doctype:'Customer',path:'customer_name',selectable:true,target};
 const result=buildReviewedWriteback({fields:[entry]},'Customer','customer_name','Trim');
 assert.equal(JSON.stringify(result),JSON.stringify({version:1,source:{doctype:'Customer',field:'customer_name'},target:{operation:'update_contact',argument:'contact',input_type:'CustomerInput',field:'full_name'},transformation:'Trim'}));
 assert.throws(()=>buildReviewedWriteback({fields:[entry,entry]},'Customer','customer_name','Trim'));
 assert.throws(()=>buildReviewedWriteback({fields:[{...entry,selectable:false}]},'Customer','customer_name','Trim'));
 assert.throws(()=>buildReviewedWriteback({fields:[entry]},'Customer','other','Trim'));
 assert.throws(()=>buildReviewedWriteback({fields:[entry]},'Customer','customer_name','Unknown'));
});

test('shared writeback requests enforce owner, revision, no-op and review-only edits without activation', () => {
 const {sharedWritebackRequest}=window.hero_integration_mapping_editor;
 const mapping={version:1,source:{doctype:'Customer',field:'customer_name'},target:{operation:'update_contact',argument:'contact',input_type:'CustomerInput',field:'full_name'},transformation:'Trim'};
 const catalog={fields:[{doctype:'Customer',path:'customer_name',selectable:true,target:{...mapping.target,graphql_type:'String'}}]};
 const row={name:'draft',operation_name:'update_contact',status:'Draft',mapping_revision:3,suggestion_reason:'old',confidence:0.7};
 const doc={...row,doctype:'HERO GraphQL Mapping',schema_snapshot:'snap',direction:'FRAPPE_TO_MUTATION',operation_kind:'MUTATION',mapping_json:JSON.stringify(mapping),definition_id:'def',modified_by:'alice'};
 const input={doc,row,actor:'alice',catalog,snapshot:'snap',transformation:'Trim',reason:'old',confidence:'0.7'};
 assert.equal(sharedWritebackRequest(input),null);
 const reasonOnly=sharedWritebackRequest({...input,reason:'reviewed'});
 assert.equal(reasonOnly.suggestion_reason,'reviewed');assert.equal(Object.hasOwn(reasonOnly,'confidence'),false);
 assert.equal(reasonOnly.mapping_name,'draft');assert.equal(reasonOnly.expected_revision,3);assert.equal(reasonOnly.activate,0);
 assert.throws(()=>sharedWritebackRequest({...input,reason:'edit',actor:'bob'}),/owner/);
 assert.throws(()=>sharedWritebackRequest({...input,reason:'edit',doc:{...doc,status:'Active'}}),/status/);
 assert.throws(()=>sharedWritebackRequest({...input,reason:'edit',doc:{...doc,mapping_json:JSON.stringify({...mapping,extra:'retain'})}}),/losing parameters/);
 const choice=catalog.fields[0]; const created=sharedWritebackRequest({...input,doc:null,row:null,choice,reason:'new'});
 assert.equal(created.mapping_name,'');assert.equal(created.expected_revision,0);assert.equal(created.activate,0);
 assert.equal(JSON.parse(created.mapping_json).target.field,'full_name');
});

test('shared save is accepted only for the expected identity and a changed revision', () => {
 const {sharedWritebackSaveAccepted}=window.hero_integration_mapping_editor;
 const doc={name:'draft-1',mapping_revision:3};
 const result={name:'draft-1',status:'Draft',revision:4,unchanged:false};
 assert.equal(sharedWritebackSaveAccepted(result,doc),true);
 assert.equal(sharedWritebackSaveAccepted({...result,name:'draft-2'},doc),false);
 assert.equal(sharedWritebackSaveAccepted({...result,revision:3},doc),false);
 assert.equal(sharedWritebackSaveAccepted({...result,unchanged:true},doc),false);
 assert.equal(sharedWritebackSaveAccepted({...result,status:'Active'},doc),false);
 assert.equal(sharedWritebackSaveAccepted({...result,name:''},null),false);
 assert.equal(sharedWritebackSaveAccepted(result,null),true);
});

test('shared save releases controls before reloading and skips stale or failed saves', async () => {
 const {finishSharedWritebackSave}=window.hero_integration_mapping_editor;
 for (const [saved,current,expected] of [[true,true,'release,refresh'],[false,true,'release'],[true,false,'release']]) {
  const events=[]; let disabled=true;
  await finishSharedWritebackSave(saved,()=>current,()=>{events.push('release');disabled=false},async()=>{
   assert.equal(disabled,false);events.push('refresh');
  });
  assert.equal(events.join(','),expected);
 }
});

test('resuming an interrupted definition browser does not replace it with an automatic selection', () => {
 const {resumePendingView}=window.hero_integration_mapping_editor;
 const reads=[];
 resumePendingView({kind:'list',selectFirst:false},{list:(selectFirst)=>reads.push(selectFirst)});
 resumePendingView({kind:'list',selectFirst:true},{list:(selectFirst)=>reads.push(selectFirst)});
 assert.deepEqual(reads,[false,true]);
});

test('resuming automatic directional entry preserves keyboard focus on its tab', () => {
 const {resumePendingView}=window.hero_integration_mapping_editor;
 const reads=[];
 resumePendingView({kind:'detail',item:{name:'query'},mappingType:'graphql',focus:false},{detail:(item,kind,focus)=>reads.push([item.name,kind,focus])});
 assert.deepEqual(reads,[['query','graphql',false]]);
});
