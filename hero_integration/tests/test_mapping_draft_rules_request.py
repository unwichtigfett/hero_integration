"""Opt-in actual RPC/DB checks for personal rule snapshots and parameter retention."""
from copy import deepcopy
import json
import multiprocessing
import os
from unittest.mock import patch
from uuid import uuid4

import frappe
from hero_integration.tests import test_mapping_draft_api_request as base
from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import DraftConflict, reconciliation_proof_from_json


def configure():
    base.ROOT=os.environ['HERO_RULES_TEST_ROOT']
    base.SITES=base.ROOT+'/sites'; base.SITE=os.environ.get('HERO_DRAFT_TEST_SITE','mapping06c.localhost'); base.DB=os.environ.get('HERO_DRAFT_TEST_DB','heromapping06c')


def writer(definition, actor, content, barrier, result):
    configure(); base.connect(); frappe.set_user(actor)
    cid=frappe.db.sql('select connection_id()')[0][0]
    try:
        barrier.wait(10)
        FrappeMappingDraftStorage().service().save(definition,actor,0,1,content,expected_generation=1)
        frappe.db.commit(); result.put((cid,'SAVED'))
    except DraftConflict:
        frappe.db.rollback(); result.put((cid,'CONFLICT'))
    except Exception as error:
        frappe.db.rollback(); result.put((cid,'ERROR',repr(error)))
    finally: frappe.destroy()


def reconciliation_writer(definition, actor, content, proof_json, barrier, result):
    """A second real MariaDB connection racing the exact same proof."""
    configure(); base.connect(); frappe.set_user(actor)
    cid=frappe.db.sql('select connection_id()')[0][0]
    try:
        proof=reconciliation_proof_from_json(proof_json)
        barrier.wait(10)
        FrappeMappingDraftStorage().service().save(
            definition, actor, 0, 2, content, expected_generation=1,
            expected_reconciliation_revision=0, reconciliation=proof,
        )
        frappe.db.commit(); result.put((cid,'SAVED'))
    except DraftConflict:
        frappe.db.rollback(); result.put((cid,'CONFLICT'))
    except Exception as error:
        frappe.db.rollback(); result.put((cid,'ERROR',repr(error)))
    finally: frappe.destroy()


def observe_reconciliation(key, queue):
    configure(); base.connect()
    queue.put((frappe.db.sql('select connection_id()')[0][0],
        frappe.db.get_value('HERO Mapping Draft',key,['content_revision','reconciliation_revision']),
        frappe.db.count('HERO Mapping Revision',{'draft_key':key})))
    frappe.destroy()


def observe(key, queue):
    configure(); base.connect()
    queue.put((frappe.db.sql('select connection_id()')[0][0],
        frappe.db.get_value('HERO Mapping Draft',key,'content_revision'),
        frappe.db.count('HERO Mapping Revision',{'draft_key':key})))
    frappe.destroy()


class TestPersonalRulesRequest(base.TestMappingDraftAPIRequest):
    def prepare_rules(self):
        base.connect()
        self.rule_id='synthetic-rule-'+uuid4().hex
        self.rule_name='synthetic-rule-doc-'+uuid4().hex
        parent=frappe.get_doc('HERO GraphQL Mapping',self.rows['active'])
        self.foreign_rule_id=frappe.db.get_value('HERO Frappe Mapping',self.rows['fieldactive'],'definition_id')
        rule=frappe.get_doc({'doctype':'HERO Frappe Mapping','name':self.rule_name,
            'definition_id':self.rule_id,'schema_snapshot':self.snapshot,'schema_fingerprint':parent.schema_fingerprint,
            'operation':'readThings','source_path':'nested.source','source_type':'String','target_doctype':'Synthetic Record',
            'target_field':'original_target','purpose':'Field','transformation':'Identity','priority':0,'relation':'','status':'Active','mapping_revision':1})
        rule.db_insert()
        content={'records_path':'readThings','identity_path':'id','mapping_definition_ids':[self.rule_id],
                 'additional_parameter':{'flag':False,'zero':0,'empty':'','list':[],'nested':{'keep':[False,0,'']}}}
        frappe.db.set_value('HERO GraphQL Mapping',parent.name,'mapping_json',json.dumps(content))
        frappe.db.commit(); frappe.destroy()
        return content

    def args_for(self,draft,content):
        return {'definition_id':draft['definition_id'],'expected_generation':draft['generation'],
                'expected_draft_revision':draft['content_revision'],'expected_active_revision':draft['base_active_revision'],
                'mapping_json':json.dumps(content)}

    def test_private_rules_revision_frozen_template_change_and_discard(self):
        active=self.prepare_rules(); a=self.create('a'); b=self.create('b')
        self.assertEqual('original_target',a['content']['field_rules'][self.rule_id]['target_field'])
        content=deepcopy(a['content']);content['field_rules'][self.rule_id]['target_field']='personal_target'
        status,data=self.rpc('a','mapping_draft_save',self.args_for(a,content),'POST');self.assertEqual(200,status,data)
        saved=data['message']['draft'];self.assertEqual(content,saved['content'])
        self.assertEqual(content,self.rpc('a','mapping_draft_read',{'definition_id':self.definition})[1]['message']['draft']['content'])
        self.assertEqual(b,self.rpc('b','mapping_draft_read',{'definition_id':self.definition})[1]['message']['draft'])
        base.connect()
        self.assertEqual(active,json.loads(frappe.db.get_value('HERO GraphQL Mapping',self.rows['active'],'mapping_json')))
        self.assertEqual('original_target',frappe.db.get_value('HERO Frappe Mapping',self.rule_name,'target_field'))
        # Fixture-only template change; never a personal-write side effect.
        frappe.db.set_value('HERO Frappe Mapping',self.rule_name,{'target_field':'later_template','mapping_revision':2,'operation':'later_template_operation'})
        frappe.db.commit();frappe.destroy()
        history=self.rpc('a','mapping_draft_revisions',{'definition_id':self.definition})[1]['message']
        self.assertEqual(content,history['items'][0]['content'])
        self.assertEqual('readThings',history['items'][0]['content']['field_rule_bases'][self.rule_id]['operation'])
        status,data=self.rpc('a','mapping_draft_save',self.args_for(saved,content),'POST')
        self.assertEqual(200,status,data);self.assertTrue(data['message']['unchanged'])
        self.assertEqual(saved['content_revision'],data['message']['draft']['content_revision'])
        status,data=self.rpc('a','mapping_draft_save',self.args_for(a,content),'POST')
        self.assertEqual(417,status,data);self.assertEqual('TimestampMismatchError',data['exc_type'])
        status,data=self.rpc('a','mapping_draft_discard',{'definition_id':self.definition,'expected_generation':1,'expected_draft_revision':1,'confirmed':True},'POST')
        self.assertEqual(200,status,data)
        self.assertEqual(content,self.rpc('a','mapping_draft_revisions',{'definition_id':self.definition})[1]['message']['items'][0]['content'])

    def test_rule_provenance_extra_parameters_and_privilege_injection_rejected_atomically(self):
        self.prepare_rules();draft=self.create();before=self.state()
        variants=[]
        content=deepcopy(draft['content']);content['field_rule_bases'][self.rule_id]['revision']=True;variants.append(content)
        content=deepcopy(draft['content']);content['field_rule_bases'][self.rule_id]['revision']=99;variants.append(content)
        content=deepcopy(draft['content']);content['field_rules'][self.rule_id]['owner']='other';variants.append(content)
        content=deepcopy(draft['content']);content['field_rules'][self.rule_id]['priority']=True;variants.append(content)
        content=deepcopy(draft['content']);content['additional_parameter']['zero']=1;variants.append(content)
        content=deepcopy(draft['content']);del content['additional_parameter'];variants.append(content)
        content=deepcopy(draft['content']);content['owner']='Administrator';variants.append(content)
        content=deepcopy(draft['content']);content['mapping_definition_ids']=[self.foreign_rule_id];variants.append(content)
        for content in variants:
            status,data=self.rpc('a','mapping_draft_save',self.args_for(draft,content),'POST')
            self.assertEqual(417,status,data);self.assertEqual(before,self.state())
        for method,verb in [('mapping_draft_read','GET'),('mapping_draft_revisions','GET'),('mapping_draft_create','POST')]:
            self.assertEqual(403,self.rpc('employee',method,{'definition_id':self.definition},verb)[0])
        self.assertEqual(403,self.rpc('employee','mapping_draft_save',self.args_for(draft,draft['content']),'POST')[0])
        self.assertEqual(before,self.state())

    def test_writeback_additional_nested_parameters_survive_real_save(self):
        base.connect()
        snapshot=frappe.get_doc('HERO GraphQL Schema Snapshot',self.snapshot)
        schema={'__schema':{'queryType':{'name':'Query'},'mutationType':{'name':'Mutation'},'types':[
            {'kind':'OBJECT','name':'Query','fields':[{'name':'readThings','args':[],'type':{'kind':'SCALAR','name':'String'}}]},
            {'kind':'OBJECT','name':'Mutation','fields':[{'name':'editSynthetic','args':[{'name':'input','type':{'kind':'INPUT_OBJECT','name':'SyntheticInput'}}],'type':{'kind':'SCALAR','name':'String'}}]},
            {'kind':'INPUT_OBJECT','name':'SyntheticInput','inputFields':[{'name':'field','type':{'kind':'SCALAR','name':'String'}}]}, {'kind':'SCALAR','name':'String'}]}}
        content={'version':1,'source':{'doctype':'Synthetic Record','field':'source','additional_source':{'keep':False,'whole_float':1.0}},
            'target':{'operation':'editSynthetic','argument':'input','input_type':'SyntheticInput','field':'field','field_path':'field','graphql_type':'String'},
            'transformation':'Identity','draft_only':True,'additional_parameter':{'false':False,'zero':0,'empty':'','list':[],'whole_float':1.0,'negative_zero':-0.0}}
        frappe.db.set_value(snapshot.doctype,snapshot.name,'snapshot_json',json.dumps(schema))
        frappe.db.set_value('HERO GraphQL Mapping',self.rows['active'],{'mapping_json':json.dumps(content),'direction':'FRAPPE_TO_MUTATION','operation_kind':'MUTATION','operation_name':'editSynthetic'})
        frappe.db.commit();frappe.destroy()
        draft=self.create();changed=deepcopy(draft['content']);changed['transformation']='Trim'
        submitted=deepcopy(changed);submitted['additional_parameter']['whole_float']=1;submitted['additional_parameter']['negative_zero']=0;submitted['source']['additional_source']['whole_float']=1
        status,data=self.rpc('a','mapping_draft_save',self.args_for(draft,submitted),'POST');self.assertEqual(200,status,data)
        self.assertEqual(json.dumps(changed,sort_keys=True),json.dumps(data['message']['draft']['content'],sort_keys=True))
        saved=data['message']['draft'];status,noop=self.rpc('a','mapping_draft_save',self.args_for(saved,submitted),'POST');self.assertEqual(200,status,noop);self.assertTrue(noop['message']['unchanged'])
        self.assertEqual(changed,self.rpc('a','mapping_draft_read',{'definition_id':self.definition})[1]['message']['draft']['content'])
        incomplete=deepcopy(changed);incomplete['target']['field']=''
        saved=data['message']['draft'];status,data=self.rpc('a','mapping_draft_save',self.args_for(saved,incomplete),'POST')
        self.assertEqual(200,status,data);self.assertEqual(incomplete,data['message']['draft']['content'])

    def test_rule_writers_real_connections_one_cas_winner(self):
        self.prepare_rules();draft=self.create()
        ctx=multiprocessing.get_context('spawn'); barrier=ctx.Barrier(2);queue=ctx.Queue();processes=[]
        for target in ('writer-a','writer-b'):
            content=deepcopy(draft['content']);content['field_rules'][self.rule_id]['target_field']=target
            processes.append(ctx.Process(target=writer,args=(self.definition,self.users['a'],content,barrier,queue)))
        try:
            for process in processes:process.start()
            outcomes=[queue.get(timeout=20),queue.get(timeout=20)]
            self.assertEqual(2,len({v[0] for v in outcomes}));self.assertCountEqual(['SAVED','CONFLICT'],[v[1] for v in outcomes])
            self.assertEqual(1,self.rpc('a','mapping_draft_read',{'definition_id':self.definition})[1]['message']['draft']['content_revision'])
            self.assertEqual(1,self.rpc('a','mapping_draft_revisions',{'definition_id':self.definition})[1]['message']['total'])
            print('RULE_CAS_REAL_CONNECTIONS',outcomes,flush=True)
        finally:
            for process in processes:
                process.join(10)
                if process.is_alive():process.terminate();process.join(5)
            queue.close();queue.join_thread()

    def test_rule_revision_failure_rolls_back_seen_by_independent_connection(self):
        self.prepare_rules();draft=self.create();content=deepcopy(draft['content']);content['field_rules'][self.rule_id]['target_field']='must-rollback'
        base.connect();frappe.set_user(self.users['a']);cid=frappe.db.sql('select connection_id()')[0][0]
        storage=FrappeMappingDraftStorage();original=storage.append_revision
        def failure(value):original(value);raise RuntimeError('synthetic after-revision-insert failure')
        with patch.object(storage,'append_revision',side_effect=failure):
            with self.assertRaises(RuntimeError):storage.service().save(self.definition,self.users['a'],0,1,content,expected_generation=1)
        frappe.db.commit();frappe.destroy()
        ctx=multiprocessing.get_context('spawn');queue=ctx.Queue();process=ctx.Process(target=observe,args=(draft['key'],queue));process.start()
        try:
            other,revision,count=queue.get(timeout=20);self.assertNotEqual(cid,other);self.assertEqual((0,0),(revision,count))
            print('RULE_ROLLBACK_REAL_CONNECTIONS',cid,other,revision,count,flush=True)
        finally:
            process.join(10)
            if process.is_alive():process.terminate();process.join(5)
            queue.close();queue.join_thread()


    def test_exact_integer_text_transport_noop_edit_history_and_rejected_manipulation(self):
        content=self.prepare_rules()
        content['additional_parameter']['integers']=[9007199254740991,9007199254740992,9007199254740993,-9007199254740993,{'nested':9007199254740993}]
        base.connect();frappe.db.set_value('HERO GraphQL Mapping',self.rows['active'],'mapping_json',json.dumps(content));frappe.db.commit();frappe.destroy()
        draft=self.create();exact=json.loads(draft['content_json']);self.assertEqual(content['additional_parameter'],exact['additional_parameter'])
        for number in exact['additional_parameter']['integers'][:4]:self.assertIs(type(number),int)
        args=self.args_for(draft,exact);args['mapping_json']=draft['content_json']
        status,result=self.rpc('a','mapping_draft_save',args,'POST');self.assertEqual(200,status,result);self.assertTrue(result['message']['unchanged']);self.assertEqual(draft['content_hash'],result['message']['draft']['content_hash'])
        exact['field_rules'][self.rule_id]['target_field']='integer-safe-other-edit'
        status,result=self.rpc('a','mapping_draft_save',self.args_for(draft,exact),'POST');self.assertEqual(200,status,result)
        saved=result['message']['draft'];self.assertEqual(1,saved['content_revision']);self.assertEqual(exact,json.loads(saved['content_json']))
        read=self.rpc('a','mapping_draft_read',{'definition_id':self.definition})[1]['message']['draft'];self.assertEqual(saved,read)
        for number in json.loads(read['content_json'])['additional_parameter']['integers'][:4]:self.assertIs(type(number),int)
        history=self.rpc('a','mapping_draft_revisions',{'definition_id':self.definition})[1]['message']['items'][0];self.assertEqual(saved['content_json'],history['content_json']);self.assertEqual(saved['content_hash'],history['content_hash'])
        before=self.state();bad=deepcopy(exact);bad['additional_parameter']['integers'][2]=9007199254740992
        self.assertEqual(417,self.rpc('a','mapping_draft_save',self.args_for(saved,bad),'POST')[0])
        injected=self.args_for(saved,exact);injected['content_json']='{}'
        self.assertEqual(417,self.rpc('a','mapping_draft_save',injected,'POST')[0]);self.assertEqual(before,self.state())
        status,result=self.rpc('a','mapping_draft_save',self.args_for(saved,exact),'POST');self.assertEqual(200,status,result);self.assertTrue(result['message']['unchanged'])

    def test_long_reference_identities_are_exact_across_active_pages_and_draft_roundtrip(self):
        content=self.prepare_rules();prefix=('r'+uuid4().hex).ljust(120,'r');ids=[prefix,prefix+'a',prefix+'a'*19+'A',prefix+'a'*19+'B']
        base.connect();template=frappe.get_doc('HERO Frappe Mapping',self.rule_name).as_dict()
        for i,identity in enumerate([prefix+'c'+str(n).zfill(19) for n in range(101)]+ids):
            row=dict(template,name='SYNTHETIC-LONG-'+self.definition+'-'+str(i).zfill(4),definition_id=identity);frappe.get_doc(row).db_insert()
        content['mapping_definition_ids']=ids;frappe.db.set_value('HERO GraphQL Mapping',self.rows['active'],'mapping_json',json.dumps(content));frappe.db.commit();frappe.destroy()
        found=[]
        for start in (0,100):
            status,result=self.rpc('employee','frontend_mapping_list',{'search':prefix,'start':start,'page_size':100});self.assertEqual(200,status,result);self.assertEqual(105,result['message']['pagination']['frappe_total']);found+=result['message']['frappe_mappings']
        self.assertEqual(ids,[row['definition_id'] for row in found if row['definition_id'] in ids])
        detail=self.rpc('employee','frontend_mapping_detail',{'mapping_type':'frappe','name':found[-1]['name']})[1]['message'];self.assertEqual(ids[-1],detail['definition_id'])
        draft=self.create();self.assertEqual(ids,draft['content']['mapping_definition_ids']);self.assertEqual(set(ids),set(draft['content']['field_rules']))
        edited=json.loads(draft['content_json']);edited['field_rules'][ids[-1]]['target_field']='last-exact-id'
        status,result=self.rpc('a','mapping_draft_save',self.args_for(draft,edited),'POST');self.assertEqual(200,status,result);self.assertEqual(edited,json.loads(result['message']['draft']['content_json']))
        self.assertEqual(403,self.rpc('employee','mapping_draft_read',{'definition_id':self.definition})[0])

    def test_reconciliation_preview_apply_and_save_are_bound_and_nonpersistent_until_save(self):
        active = self.prepare_rules()
        draft = self.create('a')
        candidate = json.loads(draft['content_json'])
        candidate['records_path'] = 'local-draft-path'
        changed_active = deepcopy(active)
        changed_active['records_path'] = 'current-active-path'
        base.connect()
        frappe.db.set_value('HERO GraphQL Mapping', self.rows['active'], {
            'mapping_json': json.dumps(changed_active), 'mapping_revision': 2,
        })
        frappe.db.commit(); frappe.destroy()
        preview_args = {
            'definition_id': draft['definition_id'], 'expected_generation': draft['generation'],
            'expected_draft_revision': draft['content_revision'], 'expected_active_revision': 2,
            'expected_reconciliation_revision': 0, 'candidate_json': json.dumps(candidate),
        }
        before = self.state()
        status, preview = self.rpc('a', 'mapping_draft_reconcile_preview', preview_args, 'POST')
        self.assertEqual(200, status, preview)
        self.assertEqual(before, self.state(), 'preview must not persist draft content or evidence')
        conflict = next(item for item in preview['message']['items'] if item['identifier'] == 'root:records_path')
        self.assertEqual('CONFLICT', conflict['state'])
        apply_args = dict(preview_args, accepted_suggestions_json=json.dumps({'items': []}),
                          conflict_choices_json=json.dumps({'root:records_path': 'DRAFT'}))
        status, applied = self.rpc('a', 'mapping_draft_reconcile_apply', apply_args, 'POST')
        self.assertEqual(200, status, applied)
        self.assertEqual(before, self.state(), 'local apply must not persist draft content or evidence')
        self.assertEqual('local-draft-path', json.loads(applied['message']['result_content_json'])['records_path'])
        save_args = self.args_for(draft, json.loads(applied['message']['result_content_json']))
        save_args.update({'expected_active_revision': 2, 'expected_reconciliation_revision': 0,
                          'reconciliation_json': applied['message']['reconciliation_json']})
        status, saved = self.rpc('a', 'mapping_draft_save', save_args, 'POST')
        self.assertEqual(200, status, saved)
        self.assertFalse(saved['message']['unchanged'])
        self.assertTrue(saved['message']['reconciliation_updated'])
        self.assertEqual(1, saved['message']['draft']['content_revision'])
        self.assertEqual(1, saved['message']['draft']['reconciliation_revision'])
        self.assertEqual(saved['message']['draft'], self.rpc('a', 'mapping_draft_read', {
            'definition_id': draft['definition_id'],
        })[1]['message']['draft'])
        self.assertEqual(403, self.rpc('employee', 'mapping_draft_reconcile_preview', preview_args, 'POST')[0])
        self.assertIn(self.rpc(None, 'mapping_draft_reconcile_apply', apply_args, 'POST')[0], (401, 403))
        stale = dict(save_args, expected_reconciliation_revision=0)
        self.assertEqual(417, self.rpc('a', 'mapping_draft_save', stale, 'POST')[0])
        later=json.loads(saved['message']['draft']['content_json'])
        later['field_rules'][self.rule_id]['target_field']='post-reconciliation-private-edit'
        later_args=self.args_for(saved['message']['draft'],later)
        later_args.update({'expected_active_revision':2,'expected_reconciliation_revision':1})
        status,later_saved=self.rpc('a','mapping_draft_save',later_args,'POST')
        self.assertEqual(200,status,later_saved)
        self.assertEqual((2,1),(later_saved['message']['draft']['content_revision'],later_saved['message']['draft']['reconciliation_revision']))

    def _reconciliation_fixture(self):
        active=self.prepare_rules(); draft=self.create('a'); candidate=json.loads(draft['content_json'])
        candidate['records_path']='local-race-path'; changed=deepcopy(active);changed['records_path']='active-race-path'
        base.connect();frappe.db.set_value('HERO GraphQL Mapping',self.rows['active'],{'mapping_json':json.dumps(changed),'mapping_revision':2});frappe.db.commit();frappe.destroy()
        args={'definition_id':draft['definition_id'],'expected_generation':1,'expected_draft_revision':0,
              'expected_active_revision':2,'expected_reconciliation_revision':0,'candidate_json':json.dumps(candidate)}
        status,applied=self.rpc('a','mapping_draft_reconcile_apply',dict(args,
            accepted_suggestions_json=json.dumps({'items':[]}),conflict_choices_json=json.dumps({'root:records_path':'DRAFT'})),'POST')
        self.assertEqual(200,status,applied)
        return draft,json.loads(applied['message']['result_content_json']),applied['message']['reconciliation_json']

    def test_reconciliation_evidence_real_connections_has_one_cas_winner(self):
        draft,content,proof=self._reconciliation_fixture()
        ctx=multiprocessing.get_context('spawn');barrier=ctx.Barrier(2);queue=ctx.Queue();processes=[]
        try:
            for _ in range(2):
                processes.append(ctx.Process(target=reconciliation_writer,args=(self.definition,self.users['a'],content,proof,barrier,queue)))
            for process in processes: process.start()
            outcomes=[queue.get(timeout=20),queue.get(timeout=20)]
            self.assertEqual(2,len({item[0] for item in outcomes}));self.assertCountEqual(['SAVED','CONFLICT'],[item[1] for item in outcomes])
            saved=self.rpc('a','mapping_draft_read',{'definition_id':self.definition})[1]['message']['draft']
            self.assertEqual((1,1),(saved['content_revision'],saved['reconciliation_revision']))
            self.assertEqual(1,self.rpc('a','mapping_draft_revisions',{'definition_id':self.definition})[1]['message']['total'])
            print('RECONCILIATION_CAS_REAL_CONNECTIONS',outcomes,flush=True)
        finally:
            for process in processes:
                process.join(10)
                if process.is_alive(): process.terminate();process.join(5)
            queue.close();queue.join_thread()

    def test_reconciliation_evidence_write_failure_rolls_back_real_connection(self):
        draft,content,proof_json=self._reconciliation_fixture();proof=reconciliation_proof_from_json(proof_json)
        base.connect();frappe.set_user(self.users['a']);cid=frappe.db.sql('select connection_id()')[0][0]
        storage=FrappeMappingDraftStorage();original=storage.save_draft
        def failure(value): original(value);raise RuntimeError('synthetic reconciliation evidence failure')
        with patch.object(storage,'save_draft',side_effect=failure):
            with self.assertRaises(RuntimeError): storage.service().save(self.definition,self.users['a'],0,2,content,
                expected_generation=1,expected_reconciliation_revision=0,reconciliation=proof)
        frappe.db.commit();frappe.destroy()
        ctx=multiprocessing.get_context('spawn');queue=ctx.Queue();process=ctx.Process(target=observe_reconciliation,args=(draft['key'],queue));process.start()
        try:
            other,revisions,count=queue.get(timeout=20);self.assertNotEqual(cid,other);self.assertEqual(((0,0),0),(revisions,count))
            print('RECONCILIATION_ROLLBACK_REAL_CONNECTIONS',cid,other,revisions,count,flush=True)
        finally:
            process.join(10)
            if process.is_alive():process.terminate();process.join(5)
            queue.close();queue.join_thread()
