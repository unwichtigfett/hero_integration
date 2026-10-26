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
from hero_integration.services.mapping_draft_service import DraftConflict


def configure():
    base.ROOT=os.environ['HERO_RULES_TEST_ROOT']
    base.SITES=base.ROOT+'/sites'; base.SITE='mapping06c.localhost'; base.DB='heromapping06c'


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
