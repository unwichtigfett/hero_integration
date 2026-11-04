"""The single Project confirmation binds data and every displayed activity."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from graphql import build_schema, introspection_from_schema
from hero_integration.services import project_delivery as delivery
from hero_integration.services import project_logbook_writeback as logbook
from hero_integration.services.project_identity_read import ProjectIdentityReader
from hero_integration.services import mapped_update_v2, mapped_send


class TestProjectDelivery(TestCase):
    def setUp(self):
        self.data = {'project':'P-H','fingerprint':'data-token','changes':[{'label':'Notes','new':'changed'}],
                     'conflicts':[],'sending_enabled':True,'can_send':True,'phases':[],'hero':{'phase_id':None}}
        self.activities = [{'name':'C1','activity_doctype':'Comment','creation':'1'},
                           {'name':'V1','activity_doctype':'Version','creation':'2'}]
        self.calls = []
        self.baseline = 0
        self.activity_token = 'activity-token'
        def preview(name, **kwargs):
            return {'comment':name,'activity_doctype':kwargs['activity_doctype'],'project':'P-H',
                    'fingerprint':name+'-'+str(self.baseline), 'delivery_fingerprint':name+self.activity_token,
                    'changes':[{'label':'Activity','new':'safe text'}], 'can_send':True,'sending_enabled':True}
        def send_data(*args,**kwargs):
            self.calls.append('data');self.baseline += 1
            return {'status':'SENT','changes':['Notes']}
        def send_activity(name,**kwargs):
            self.assertEqual(kwargs['fingerprint'],name+'-'+str(self.baseline))
            self.calls.append(name);return {'hero_id':len(self.calls)}
        self.fake=NS(log_error=Mock())
        patches=[patch.object(delivery,'_access',return_value=NS(modified='project-original')),patch.object(delivery,'frappe',self.fake),
            patch.object(delivery,'_sign',side_effect=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()),
            patch.object(mapped_send,'preview',side_effect=lambda *args,**kw:deepcopy(self.data)),
            patch.object(mapped_send,'send',side_effect=send_data),patch.object(logbook,'candidates',side_effect=lambda *a:{'comments':deepcopy(self.activities)}),
            patch.object(logbook,'preview',side_effect=preview),patch.object(logbook,'send',side_effect=send_activity),
            patch('hero_integration.api._assert_mutation_execution_released')]
        for item in patches:item.start();self.addCleanup(item.stop)

    def preview(self):return delivery.preview('P-H',client=Mock())

    def send(self,preview,**kwargs):
        return delivery.send('P-H',fingerprint=preview['fingerprint'],delivery=preview['delivery'],confirmed=True,client=Mock(),**kwargs)

    def test_one_confirmation_sends_data_then_only_displayed_activities_with_fresh_native_tokens(self):
        preview=self.preview();self.assertEqual(len(preview['changes']),3)
        self.assertEqual(self.calls,[])
        result=self.send(preview)
        self.assertEqual(result['status'],'SENT');self.assertEqual(self.calls,['data','C1','V1'])

    def test_activity_only_and_data_only_are_supported(self):
        self.data['changes']=[]
        self.assertTrue(self.preview()['can_send']);self.send(self.preview());self.assertEqual(self.calls,['C1','V1'])
        self.calls.clear();self.activities=[];self.data['changes']=[{'label':'Notes'}]
        self.send(self.preview());self.assertEqual(self.calls,['data'])

    def test_changed_data_new_activity_and_tampered_confirmation_stop_before_transport(self):
        for change in ('data','new','tamper'):
            with self.subTest(change=change):
                preview=self.preview()
                if change=='data':self.data['fingerprint']+='changed'
                if change=='new':self.activities.append({'name':'C2','activity_doctype':'Comment','creation':'3'})
                if change=='tamper':preview['delivery']['activities']=[]
                with self.assertRaises(delivery.ProjectDeliveryError):self.send(preview)
                self.assertEqual(self.calls,[])

    def test_conflicts_global_disable_and_empty_changes_block(self):
        for change in ('conflict','disabled','empty'):
            with self.subTest(change=change):
                self.data['conflicts']=[{'label':'Notes'}] if change=='conflict' else []
                self.data['sending_enabled']=change!='disabled'
                if change=='empty':self.data['changes']=[];self.activities=[]
                preview=self.preview();self.assertFalse(preview['can_send'])
                with self.assertRaises(delivery.ProjectDeliveryError):self.send(preview)
                self.assertEqual(self.calls,[])

    def test_partial_send_stops_and_reports_confirmed_result_without_retry(self):
        preview=self.preview()
        with patch.object(logbook,'send',side_effect=RuntimeError('unknown transport')) as sender:
            result=self.send(preview)
        self.assertEqual(result['status'],'STOPPED');self.assertEqual(result['changes'],['Notes'])
        self.assertEqual(result['activities'],[]);self.assertEqual(result['failed_step'],'Comment:C1')
        self.assertEqual(sender.call_count,1)

    def test_activity_authority_change_after_data_does_not_get_new_confirmation(self):
        preview=self.preview()
        def data(*args,**kwargs):self.activity_token='changed';return {'changes':['Notes']}
        with patch.object(mapped_send,'send',side_effect=data):result=self.send(preview)
        self.assertEqual(result['status'],'STOPPED');self.assertEqual(self.calls,[])

    def test_request_cache_does_not_leak_mutations_or_survive_next_preview(self):
        client=Mock();client.query.return_value={'project':{'id':1}}
        reader=delivery.PreviewReader(client)
        reader.query('query',{'id':1})['project']['id']=2
        self.assertEqual(reader.query('query',{'id':1})['project']['id'],1)
        self.assertEqual(client.query.call_count,1)
        delivery.PreviewReader(client).query('query',{'id':1})
        self.assertEqual(client.query.call_count,2)


class TestProjectIdentityRead(TestCase):
    def schema(self,extra=''):
        return introspection_from_schema(build_schema('''
          type Project {id:Int notes:String histories(first:Int,offset:Int):[History]}
          type History {id:Int}
          type Query {project_matches(first:Int,offset:Int,orderBy:String):[Project]
                      project_match(project_match_id:Int):Project '''+extra+'''}
        '''))

    def test_same_approved_fields_target_only_bound_project_and_keep_nested_pagination(self):
        client=Mock();client.query.return_value={'project_match':{'id':7,'notes':'n','histories':[{'id':2}]}}
        reader=ProjectIdentityReader(client,self.schema(),'7')
        query='query Read($first:Int,$offset:Int,$orderBy:String,$history:Int){project_matches(first:$first,offset:$offset,orderBy:$orderBy){id notes histories(first:$first,offset:$history){id}}}'
        result=reader.query(query,{'first':50,'offset':20000,'orderBy':'id','history':100})
        document,variables=client.query.call_args.args
        self.assertIn('project_match(project_match_id: 7)',document)
        self.assertNotIn('$offset',document);self.assertNotIn('$orderBy',document)
        self.assertEqual(variables,{'first':50,'history':100})
        self.assertEqual(result,{'project_matches':[{'id':7,'notes':'n','histories':[{'id':2}]}]})

    def test_wrong_identity_and_extra_roots_fail_closed(self):
        client=Mock();client.query.return_value={'project_match':{'id':8}}
        reader=ProjectIdentityReader(client,self.schema('other:String'),'7')
        with self.assertRaises(ValueError):reader.query('{project_matches{id}}',{})
        client.reset_mock()
        with self.assertRaises(ValueError):reader.query('{project_matches{id} other}',{})
        client.query.assert_not_called()


class TestProjectNamingProtection(TestCase):
    def test_mixed_version_keeps_status_but_never_local_naming(self):
        fields={name:NS(label=name,fieldtype='Data',hidden=False) for name in ('project_name','custom_hero_project_number','status')}
        version=NS(data=json.dumps({'changed':[['project_name','Old','Title-H'],
            ['custom_hero_project_number','1','PV-1-H'],['status','Open','Completed']]}))
        with patch.object(logbook,'frappe',NS(get_meta=lambda dt:NS(get_field=fields.get),_=lambda value:value)):
            self.assertEqual(logbook._version_text(version),'status: Open → Completed')
        self.assertEqual(logbook._activity_text(NS(doctype='Comment',comment_type='Edit',content='renamed from Old to PV-1-H'),{'comment_types':['Edit']}),'')

    def test_update_mapping_rejects_project_local_name_even_when_schema_has_name_input(self):
        from hero_integration.tests.test_mapped_update_v2 import mapping,SCHEMA
        value=mapping('HeroProjectMatches');item=value['assignments'][0]
        item['source']['field']='project_name';item['input_path']='project_match.name';item['read_path']='name'
        with self.assertRaisesRegex(ValueError,'local to ERPNext'):mapped_update_v2.normalise_mapping(value,SCHEMA)

    def test_delivery_activity_token_ignores_only_own_data_baseline_advancement(self):
        from hero_integration.tests.test_project_logbook_writeback import TestLogbookPreparation
        ctx=TestLogbookPreparation().context()
        ctx.link['source_field_hashes']='before';ctx.link['target_field_hashes']='before'
        token=logbook._fingerprint(ctx,delivery=True);native=logbook._fingerprint(ctx)
        ctx.link['source_field_hashes']='after';ctx.link['target_field_hashes']='after'
        self.assertEqual(token,logbook._fingerprint(ctx,delivery=True))
        self.assertNotEqual(native,logbook._fingerprint(ctx))
        ctx.project.modified='changed'
        self.assertNotEqual(token,logbook._fingerprint(ctx,delivery=True))

    def test_identity_root_is_a_used_schema_dependency(self):
        from hero_integration.services.mapping_schema_compatibility import check_compatibility
        sdl='type Project{id:Int} type Query{project_matches(first:Int,offset:Int):[Project] project_match(project_match_id:Int):Project}'
        old=introspection_from_schema(build_schema(sdl))
        new=introspection_from_schema(build_schema(sdl.replace('project_match_id:Int','project_match_id:String')))
        with self.assertRaises(ValueError):
            check_compatibility(old,new,read_queries=['{project_matches(first:1,offset:0){id}}'],mutation_profiles=[],mutation_queries=[])

    def test_project_public_wrapper_uses_delivery_instead_of_field_only_runtime(self):
        from hero_integration.services import project_send
        with patch.object(delivery,'preview',return_value={'combined':True}) as preview:
            self.assertEqual(project_send.preview('P',client='reader'),{'combined':True})
            preview.assert_called_once_with('P',phase_id=None,client='reader')
        with patch.object(delivery,'send',return_value={'status':'SENT'}) as sender:
            project_send.send('P',fingerprint='fp',confirmed=1,delivery={'plan':True})
            self.assertEqual(sender.call_args.kwargs['delivery'],{'plan':True})

    def test_read_only_comments_do_not_block_other_eligible_activities(self):
        from frappe import _dict
        from hero_integration.tests.test_project_logbook_writeback import content
        row=_dict(name='C1',content='Read only',comment_type='Comment',creation='now')
        def get_all(dt,**kwargs):
            return [NS(mapping_json=json.dumps(content()))] if dt=='HERO GraphQL Mapping' else []
        fake=NS(get_doc=lambda *a:NS(),get_all=get_all,get_list=lambda *a,**kw:[row],local=NS(site='site'),
                has_permission=lambda dt,ptype,**kw:not(dt=='Comment' and ptype=='write'))
        with patch.object(logbook,'frappe',fake),patch.object(mapped_send,'_roles',return_value=True), \
             patch('hero_integration.client.settings.get_configuration',return_value=NS(source_instance='source')):
            self.assertEqual(logbook.candidates('P')['comments'],[])

    def test_targeted_reader_never_removes_business_filters(self):
        schema=introspection_from_schema(build_schema('''type Project{id:Int}
            type Query{project_matches(first:Int,status:String):[Project] project_match(project_match_id:Int):Project}'''))
        client=Mock();reader=ProjectIdentityReader(client,schema,'7')
        with self.assertRaises(ValueError):reader.query('{project_matches(first:1,status:"open"){id}}',{})
        client.query.assert_not_called()

    def test_native_readback_reads_one_project_and_completes_its_nested_history(self):
        from hero_integration.services.read_contract import ReadContract
        schema=TestProjectIdentityRead().schema()
        document='query Read($first:Int,$offset:Int,$history:Int){project_matches(first:$first,offset:$offset){id notes histories(first:$first,offset:$history){id}}}'
        contract=ReadContract('service',1,'mapping',1,'HeroProjectMatches','project_matches','id',(),
            {'first':'$page_size','offset':'$offset','history':0},1,adapter_options={'logbook':{
                'path':'histories','identity_path':'id','offset_variable':'history','max_pages':10}})
        ctx=NS(raw_schema=schema,hero_id='7',content={'area':'HeroProjectMatches','identity':{'read_path':'id'}},
               read=NS(service=NS(name='service',query_document=document),mapping=NS(name='mapping'),contract=contract),
               snapshot=NS(),proof=NS(source_instance='source'))
        client=Mock()
        client.query.side_effect=lambda doc,args:{'project_match':{'id':7,'notes':'n',
            'histories':[{'id':1}] if args['history']==0 else [{'id':2}] if args['history']==1 else []}}
        fake=NS(get_doc=lambda dt,name:ctx.read.service if dt=='HERO GraphQL Service' else ctx.read.mapping)
        with patch.object(mapped_update_v2,'frappe',fake), \
             patch('hero_integration.services.read_contract.from_documents',return_value=contract), \
             patch('hero_integration.services.writeback_source.validate_source'):
            result=mapped_update_v2._read_record(ctx,client)
        self.assertEqual([row['id'] for row in result['histories']],[1,2])
        self.assertEqual(client.query.call_count,3)
        self.assertTrue(all('project_match(project_match_id: 7)' in call.args[0] for call in client.query.call_args_list))

    def test_legacy_grouped_mapping_rejects_local_title_source_and_target(self):
        from hero_integration.tests.test_mapped_send import content, SCHEMA
        for field,target in (('project_name','remarks'),('notes','name')):
            value=content();value['source']={'doctype':'Project','field':field};value['target']['field']=target
            with self.assertRaisesRegex(ValueError,'local to ERPNext'):
                mapped_send.normalise_send_mapping(value,SCHEMA)


class TestConfirmedPhaseStamp(TestCase):
    def test_exact_phase_save_receipt_can_rebase_delivery_token_but_not_native_confirmation(self):
        from hero_integration.tests.test_project_logbook_writeback import TestLogbookPreparation
        ctx=TestLogbookPreparation().context()
        original=logbook._fingerprint(ctx,delivery=True)
        before=str(ctx.project.modified);ctx.project.modified='own-verified-phase-save'
        self.assertNotEqual(original,logbook._fingerprint(ctx,delivery=True))
        self.assertEqual(original,logbook._fingerprint(ctx,delivery=True,project_modified=before))
        config=NS(endpoint='endpoint',api_token='synthetic',timeout_seconds=1)
        prepared=NS(text='New\nactivity',fingerprint=logbook._fingerprint(ctx))
        with patch.object(logbook,'_load_context',return_value=ctx),patch.object(logbook,'_prepare',return_value=prepared), \
             patch('hero_integration.services.writeback_source.validate_source',return_value=config), \
             patch('hero_integration.api._mutation_execution_state',return_value={'mutation_execution_enabled':True}):
            result=logbook.preview('C1',client=Mock(),delivery_project_stamp={'before':before,'after':'own-verified-phase-save'})
            self.assertEqual(result['fingerprint'],prepared.fingerprint)
            self.assertEqual(result['delivery_fingerprint'],original)
            ctx.project.modified='concurrent-change'
            with self.assertRaises(ValueError):
                logbook.preview('C1',client=Mock(),delivery_project_stamp={'before':before,'after':'own-verified-phase-save'})

class TestProjectRelationshipPreview(TestCase):
    def test_unresolved_related_binding_is_visible_in_preview_and_still_blocks_actual_send(self):
        from hero_integration.tests.test_mapped_update_v2 import TestMappedUpdatePrepare
        ctx=TestMappedUpdatePrepare().context('HeroTasks')
        # Same native update machinery, a Project relationship assignment.
        ctx.content['area']='HeroProjectMatches'
        remote={'id':7,'name':'old'}
        def source(_ctx,item,_remote):
            ctx.source_documents[item['id']]=ctx.document;ctx.source_links[item['id']]=ctx.link
            return ctx.document,'new'
        with patch.object(mapped_update_v2,'_read_record',return_value=remote), \
             patch.object(mapped_update_v2,'_source',side_effect=source), \
             patch.object(mapped_update_v2,'_wire',side_effect=mapped_update_v2.MappedUpdateBindingError('The native binding requires synchronization or conflict review.')), \
             patch.object(mapped_update_v2.frappe,'get_meta',return_value=NS(get_field=lambda _:NS(label='Customer'))):
            prepared=mapped_update_v2._prepare(ctx,Mock(),preview_only=True)
            self.assertEqual(prepared.changes,[]);self.assertEqual(len(prepared.conflicts),1)
            self.assertEqual(prepared.conflicts[0]['label'],'Customer')
            self.assertIn('synchronization',prepared.conflicts[0]['blocked_reason'])
            self.assertNotIn('name',prepared.variables['input'])
            with self.assertRaises(mapped_update_v2.MappedUpdateBindingError):mapped_update_v2._prepare(ctx,Mock())
