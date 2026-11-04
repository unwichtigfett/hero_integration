"""Update-only mappings preserve identity and safety across all native areas."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from graphql import build_schema, introspection_from_schema
from hero_integration.services import mapped_update_v2 as update

SDL='''
scalar DateTime
scalar Date
input ContactInput {id:Int name:String address:AddressInput}
input AddressInput {id:Int line_2:String full_address:String}
input ProjectInput {id:Int name:String customer_id:Int}
input EventInput {id:Int name:String start:DateTime end:DateTime all_day:Boolean}
input TaskInput {id:Int name:String}
input BaseInput {id:Int product_id:String name:String}
input VersionInput {id:Int product_id:String base_data:BaseInput! name:String}
input ServiceInput {id:Int name:String}
input PartnerInput {id:Int first_name:String! last_name:String! email:String! name:String birth_date:Date signature:String address:AddressInput}
type Address {id:Int line_2:String full_address:String}
type Base {id:Int product_id:String name:String}
type Record {id:Int product_id:String name:String filename:String first_name:String last_name:String email:String birth_date:Date signature:String address:Address base_data:Base}
type Edge {node:Record} type FilePage {edges:[Edge]}
type Query {contacts:[Record] project_matches:[Record] calendar_events:[Record] tasks:[Record] supply_product_versions:[Record] supply_services:[Record] FileUpload_FileUploads:FilePage partners:[Record]}
type Mutation {update_contact(contact:ContactInput):Record update_project_match(project_match:ProjectInput):Record update_calendar_event(calendar_event:EventInput):Record update_task(task:TaskInput):Record update_supply_product_version(supply_product_version:VersionInput):Record update_supply_service(supply_service:ServiceInput):Record update_file_upload(id:Int filename:String):Record update_partner(partner:PartnerInput):Record}
'''
SCHEMA=introspection_from_schema(build_schema(SDL))


def mapping(area='HeroTasks'):
    dt,_,_,op,identity=update.AREAS[area]
    argument=identity.rsplit('.',1)[0] if '.' in identity else ''
    data={'usage':update.USAGE,'version':2,'area':area,'read_service':'approved-reader','source':{'doctype':dt},
      'target':{'operation':op},'identity':{'read_path':'product_id' if area=='HeroProducts' else 'id',
      'input_path':identity,'mutation_read_path':'id','response_path':'id'},'assignments':[{'id':str(uuid4()),
       'source':{'doctype':dt,'field':'file_name' if dt=='File' else 'subject'},
       'input_path':argument+'.name' if argument else 'filename','read_path':'filename' if dt=='File' else 'name','query_rule_id':'',
       'resolver':'FIELD','options':{}}],'required_readback':[]}
    if area=='HeroProducts': data['required_readback']=[{'input_path':argument+'.'+p,'read_path':p} for p in ('base_data.id','base_data.product_id','product_id')]
    if area=='HeroEmployees':data['required_readback']=[{'input_path':'partner.'+p,'read_path':p} for p in ('first_name','last_name','email')]
    return data


class TestMappedUpdateSchema(TestCase):
    def test_all_nine_are_schema_bound(self):
        for area in update.AREAS:
            with self.subTest(area=area):self.assertEqual(update.normalise_mapping(mapping(area),SCHEMA)['area'],area)

    def test_product_version_identity_cannot_be_replaced_by_product_id(self):
        data=mapping('HeroProducts');data['identity']['mutation_read_path']='product_id'
        with self.assertRaisesRegex(ValueError,'identity'):update.normalise_mapping(data,SCHEMA)

    def test_mandatory_copies_cannot_be_missing_or_grant_extra_inputs(self):
        for area in ('HeroEmployees','HeroProducts'):
            data=mapping(area);data['required_readback']=[]
            with self.assertRaisesRegex(ValueError,'Required unchanged'):update.normalise_mapping(data,SCHEMA)
        data=mapping();data['required_readback']=[{'input_path':'task.name','read_path':'name'}]
        with self.assertRaisesRegex(ValueError,'arbitrary'):update.normalise_mapping(data,SCHEMA)

    def test_no_identity_duplicate_or_delete_assignments(self):
        for change in ('identity','duplicate','delete'):
            data=mapping()
            if change=='identity':data['assignments'][0]['input_path']='task.id'
            elif change=='delete':data['assignments'][0]['input_path']='task.delete'
            else:
                extra=deepcopy(data['assignments'][0]);extra['id']=str(uuid4());data['assignments'].append(extra)
            with self.assertRaises(ValueError):update.normalise_mapping(data,SCHEMA)

    def test_employee_input_status_does_not_invent_unavailable_enums(self):
        data=mapping('HeroEmployees');data['assignments'][0]['input_path']='partner.status'
        with self.assertRaises(ValueError):update.normalise_mapping(data,SCHEMA)

    def test_scalar_file_arguments_stay_scalar(self):
        data=update.normalise_mapping(mapping('HeroDocuments'),SCHEMA)
        self.assertEqual(data['identity']['input_path'],'id');self.assertEqual(data['assignments'][0]['input_path'],'filename')

    def test_protected_native_metadata_blocks(self):
        field=SimpleNamespace(fieldtype='Data',get=lambda key:1 if key=='read_only' else 0)
        with patch.object(update.frappe,'get_meta',return_value=SimpleNamespace(get_field=lambda _:field)):
            with self.assertRaisesRegex(ValueError,'protected'):update.validate_sources(mapping())

    def test_event_readonly_and_recurring_are_not_ordinary_updates(self):
        ctx=SimpleNamespace(content={'area':'HeroCalendarEvents'})
        for record in ({'readonly':True,'is_recurring':False},{'readonly':False,'is_recurring':True},{}):
            with self.assertRaises(ValueError):update._guard_record(ctx,record)
        update._guard_record(ctx,{'readonly':False,'is_recurring':False})

    def test_separate_product_and_employee_identity_proofs(self):
        ctx=SimpleNamespace(content={'area':'HeroProducts'})
        record={'id':12,'product_id':'product-a','base_data':{'id':7,'product_id':'product-a'}}
        update._guard_record(ctx,record);record['base_data']['product_id']='other'
        with self.assertRaises(ValueError):update._guard_record(ctx,record)
        ctx.content['area']='HeroEmployees'
        with self.assertRaises(ValueError):update._guard_record(ctx,{'user_id':1,'user':{'id':2}})

    def test_plain_text_inverse_preserves_native_semantics(self):
        item={'resolver':'PLAIN_TEXT','options':{}}
        self.assertEqual(update._wire(None,item,None,'<p>A &lt; B<br>C</p>',{}),'A < B\nC')

    def test_native_check_is_equivalent_to_graphql_boolean(self):
        self.assertTrue(update._same(1,True));self.assertTrue(update._same(0,False));self.assertFalse(update._same(2,True))

    def test_null_does_not_erase_without_explicit_policy(self):
        self.assertIs(update._wire(None,{'resolver':'FIELD','options':{}},None,None,{}),update.MISSING)
        self.assertIsNone(update._wire(None,{'resolver':'FIELD','options':{'null':'EXPLICIT_NULL'}},None,None,{}))


class TestMappedUpdateExecution(TestCase):
    def test_global_gate_blocks_before_mapping_or_remote_access(self):
        import hero_integration.api as api
        with patch.object(api,'_assert_mutation_execution_released',side_effect=ValueError('disabled')),patch.object(update,'_load_context') as load:
            with self.assertRaisesRegex(ValueError,'disabled'):update.send('m','Task','t',confirmed=True,fingerprint='x')
            load.assert_not_called()

    def test_changed_preview_never_reserves_or_sends(self):
        import hero_integration.api as api
        ctx=SimpleNamespace(proof=object())
        prepared=SimpleNamespace(fingerprint='new',changes=[{}],conflicts=[])
        with patch.object(api,'_assert_mutation_execution_released'),patch.object(update,'_load_context',return_value=ctx), \
             patch('hero_integration.services.writeback_source.validate_source'),patch.object(update,'_prepare',return_value=prepared), \
             patch.object(api,'_reserve_mutation') as reserve:
            with self.assertRaisesRegex(ValueError,'preview changed'):update.send('m','Task','t',client=Mock(),confirmed=True,fingerprint='old')
            reserve.assert_not_called()

    def test_remote_conflict_never_reserves(self):
        import hero_integration.api as api
        with patch.object(api,'_assert_mutation_execution_released'),patch.object(update,'_load_context',return_value=SimpleNamespace(proof=object())), \
             patch('hero_integration.services.writeback_source.validate_source'),patch.object(update,'_prepare',return_value=SimpleNamespace(fingerprint='x',changes=[{}],conflicts=[{}])), \
             patch.object(api,'_reserve_mutation') as reserve:
            with self.assertRaisesRegex(ValueError,'baseline conflicts'):update.send('m','Task','t',client=Mock(),confirmed=True,fingerprint='x')
            reserve.assert_not_called()


class TestMappedUpdatePrepare(TestCase):
    def context(self,area):
        from hero_integration.services.graphql_contract import parse_mutation
        content=mapping(area);op=content['target']['operation'];owner=content['source']['doctype']
        types={'HeroTasks':'TaskInput','HeroProducts':'VersionInput','HeroEmployees':'PartnerInput'}
        if owner=='File': document='mutation($key:Int,$value:String){update_file_upload(id:$key,filename:$value){id}}'
        else:
            argument=content['identity']['input_path'].split('.')[0]
            document=f'mutation($input:{types[area]}){{{op}({argument}:$input){{id}}}}'
        local=SimpleNamespace(doctype=owner,name='native-id',modified='v1',get=lambda field:'new')
        link=SimpleNamespace(name='bound-link',source_id='product-key' if area=='HeroProducts' else '7')
        ctx=SimpleNamespace(content=content,document=local,mapping=SimpleNamespace(name='map',mapping_revision=2,operation_name=op),
          link=link,source_documents={},source_links={},raw_schema=SCHEMA,parsed=parse_mutation(document),
          service=SimpleNamespace(query_document=document),read=SimpleNamespace(contract=SimpleNamespace(revision=1),
          service=SimpleNamespace(query_document='approved query')),contract=SimpleNamespace(name='contract',contract_revision=1,contract_fingerprint='hash'),
          proof=SimpleNamespace(fingerprint='proof'))
        return ctx

    def prepare(self,area,remote):
        ctx=self.context(area)
        def source(_ctx,item,_remote):
            ctx.source_documents[item['id']]=ctx.document;ctx.source_links[item['id']]=ctx.link
            return ctx.document,'new'
        with patch.object(update,'_read_record',return_value=remote),patch.object(update,'_source',side_effect=source), \
             patch.object(update,'_canonical',return_value='old'),patch.object(update,'_baseline_matches',return_value=True), \
             patch.object(update.frappe,'get_meta',return_value=SimpleNamespace(get_field=lambda _:SimpleNamespace(label='Name'))):
            return update._prepare(ctx,Mock())

    def test_product_uses_version_id_and_retains_base_identity(self):
        result=self.prepare('HeroProducts',{'id':7,'product_id':'product-key','base_data':{'id':9,'product_id':'product-key'},'name':'old'})
        self.assertEqual(result.variables,{'input':{'id':7,'product_id':'product-key','base_data':{'id':9,'product_id':'product-key'},'name':'new'}})
        self.assertEqual(result.mutation_id,7)

    def test_employee_retains_unchanged_required_members(self):
        result=self.prepare('HeroEmployees',{'id':7,'first_name':'First','last_name':'Last','email':'person@example.org','name':'old'})
        self.assertEqual(result.variables['input']['email'],'person@example.org')
        self.assertEqual(result.variables['input']['first_name'],'First')

    def test_file_binds_actual_scalar_variable_names(self):
        result=self.prepare('HeroDocuments',{'id':7,'name':'old'})
        self.assertEqual(result.variables,{'key':7,'value':'new'})

    def test_missing_required_member_blocks_before_transport(self):
        with self.assertRaisesRegex(ValueError,'required unchanged'):
            self.prepare('HeroEmployees',{'id':7,'first_name':'First','last_name':'Last','name':'old'})

    def test_identity_must_be_positive_not_document_name(self):
        with self.assertRaisesRegex(ValueError,'mutation record identity'):
            self.prepare('HeroTasks',{'id':'ERP-TASK-1','name':'old'})

    def test_failed_readback_never_retries_remote_write(self):
        import hero_integration.api as api
        ctx=self.context('HeroTasks');ctx.snapshot=SimpleNamespace(schema_fingerprint='schema');ctx.hero_id='7'
        prepared=SimpleNamespace(fingerprint='x',changes=[{'label':'name'}],conflicts=[],variables={'input':{'id':7}},mutation_id=7)
        transport=Mock();transport.execute_once.return_value={'update_task':{'id':7}}
        audit=SimpleNamespace(correlation_id='audit')
        with patch.object(api,'_assert_mutation_execution_released'),patch.object(update,'_load_context',return_value=ctx), \
             patch('hero_integration.services.writeback_source.validate_source'),patch.object(update,'_prepare',return_value=prepared), \
             patch.object(api,'_reserve_mutation',return_value=audit),patch.object(api,'_finalize_mutation') as finalize, \
             patch.object(update,'_read_record',side_effect=ValueError('unverified')),patch.object(update.frappe,'db',Mock()), \
             patch.object(update,'_acknowledge') as acknowledge:
            with self.assertRaisesRegex(ValueError,'no automatic retry'):
                update.send('m','Task','t',client=Mock(),mutation_client=transport,confirmed=True,fingerprint='x')
            self.assertEqual(transport.execute_once.call_count,1);acknowledge.assert_not_called()
            self.assertEqual(finalize.call_args.args[1],'Failed')


class TestMappedCollectionPriceProof(TestCase):
    def context(self):
        item={'id':'price','resolver':'PRICE','read_path':'sales_prices','options':{'price_list':'Standard Selling'}}
        selected={'source_id':'500','document':SimpleNamespace(currency='EUR'),'config':{
          'collection_path':'sales_prices','identity_path':'id','price_list_identity_path':'supply_sales_price_id',
          'fields':{'price_list_rate':'net_price_per_unit'},'price_lists':{'30':{'name':'Standard Selling','currency':'EUR'}}}}
        ctx=SimpleNamespace(price_collections={'price':selected})
        remote={'sales_prices':[{'id':500,'supply_sales_price_id':30,'net_price_per_unit':9.5,'supply_sales_price':{'is_default':True}}]}
        return ctx,item,remote

    def test_only_explicit_owned_default_row_is_eligible(self):
        ctx,item,remote=self.context();self.assertEqual(update._collection_remote(ctx,item,remote),9.5)
        remote['sales_prices'][0]['supply_sales_price']['is_default']=False
        with self.assertRaisesRegex(ValueError,'proven default'):update._collection_remote(ctx,item,remote)

    def test_foreign_price_list_and_duplicate_row_block(self):
        for mode in ('list','duplicate','identity'):
            ctx,item,remote=self.context()
            if mode=='list':remote['sales_prices'][0]['supply_sales_price_id']=31
            elif mode=='identity':remote['sales_prices'][0]['id']=501
            else:remote['sales_prices'].append(deepcopy(remote['sales_prices'][0]))
            with self.assertRaises(ValueError):update._collection_remote(ctx,item,remote)

    def test_local_rate_change_requires_old_remote_rate_and_all_dimensions(self):
        from hero_integration.services.mapped_collection_relations import _hash
        ctx,item,_remote=self.context();item['source']={'doctype':'Item Price','field':'price_list_rate'}
        baseline={'price_list_rate':9.5,'currency':'EUR','uom':'Nos'}
        selected=ctx.price_collections['price'];selected.update(observed={**baseline,'price_list_rate':10},target_payload_hash=_hash(baseline))
        ctx.source_links={'price':SimpleNamespace(target_field_hashes='{}')};ctx.content={'area':'HeroProducts'}
        self.assertTrue(update._baseline_matches(ctx,item,{},9.5))
        self.assertFalse(update._baseline_matches(ctx,item,{},8.5))
        selected['observed']['currency']='USD'
        self.assertFalse(update._baseline_matches(ctx,item,{},9.5))

    def test_negative_or_nonfinite_price_cannot_be_sent(self):
        for value in (-1,float('inf'),float('nan'),True):
            with self.assertRaises(ValueError):update._wire(None,{'resolver':'PRICE','options':{}},None,value,{})

    def test_different_mutation_and_readback_semantics_are_rejected(self):
        data=mapping('HeroProducts');data['assignments'][0]['read_path']='base_data.name'
        with self.assertRaisesRegex(ValueError,'same HERO field'):update.normalise_mapping(data,SCHEMA)


class TestAddedNativeSources(TestCase):
    def test_birth_date_preserves_date_without_inventing_time(self):
        from datetime import date,datetime
        self.assertEqual(update._wire(None,{'resolver':'DATE','options':{}},None,date(1990,4,3),{}),'1990-04-03')
        self.assertEqual(update._wire(None,{'resolver':'DATE','options':{}},None,'1990-04-03',{}),'1990-04-03')
        with self.assertRaises(ValueError):update._wire(None,{'resolver':'DATE','options':{}},None,datetime(1990,4,3),{})
        data=mapping('HeroEmployees');item=data['assignments'][0]
        item.update(source={'doctype':'Employee','field':'date_of_birth'},input_path='partner.birth_date',read_path='birth_date',resolver='DATE')
        update.normalise_mapping(data,SCHEMA)

    def test_visible_email_signature_is_not_an_account_privilege(self):
        data=mapping('HeroEmployees');item=data['assignments'][0]
        item.update(source={'doctype':'User','field':'email_signature'},input_path='partner.signature',read_path='signature',resolver='HTML')
        update.normalise_mapping(data,SCHEMA)
        item['source']['field']='roles'
        with self.assertRaises(ValueError):update.normalise_mapping(data,SCHEMA)

    def test_employee_address_requires_unchanged_proven_address_id(self):
        data=mapping('HeroEmployees');item=data['assignments'][0]
        item.update(source={'doctype':'Employee','field':'current_address'},input_path='partner.address.full_address',read_path='address.full_address')
        with self.assertRaisesRegex(ValueError,'address identity'):update.normalise_mapping(data,SCHEMA)
        data['required_readback'].append({'input_path':'partner.address.id','read_path':'address.id'})
        update.normalise_mapping(data,SCHEMA)

    def test_phone_baseline_key_follows_approved_query_contract(self):
        ctx=SimpleNamespace();item={'source':{'doctype':'Contact','field':'phone'}}
        with patch.object(update,'_rule',return_value=SimpleNamespace(target_doctype='Contact')):
            self.assertEqual(update._baseline_key(ctx,item),'phone')
        with patch.object(update,'_rule',return_value=SimpleNamespace(target_doctype='Contact Phone')):
            self.assertEqual(update._baseline_key(ctx,item),'hero_phone_home')
        self.assertEqual(update._baseline_key(ctx,{'source':{'doctype':'ToDo','field':'allocated_to'}}),'ToDo.allocated_to')

    def test_task_datetime_obeys_explicit_timezone_and_dst_gap(self):
        item={'resolver':'DATETIME','options':{'timezone':'Europe/Berlin'}}
        self.assertEqual(update._wire(None,item,None,'2026-01-03 14:00:00',{}),'2026-01-03T14:00:00+01:00')
        with self.assertRaisesRegex(ValueError,'Ambiguous or nonexistent'):
            update._wire(None,item,None,'2026-03-29 02:30:00',{})


class TestExistingSendDispatcher(TestCase):
    def context(self,doctype):
        return SimpleNamespace(content={'usage':update.USAGE},mapping=SimpleNamespace(name='version-two'))

    def test_update_profile_is_selected_only_for_native_update(self):
        from hero_integration.services import mapped_send
        row=SimpleNamespace(name='v2',mapping_json=json.dumps({'usage':update.USAGE,'source':{'doctype':'Task'}}))
        self.assertEqual(mapped_send._profile_name([row],'Task','UPDATE'),'v2')
        for doctype,mode in [('Customer','UPDATE'),('Task','CREATE')]:
            with self.assertRaises(ValueError):mapped_send._profile_name([row],doctype,mode)
        with self.assertRaises(ValueError):mapped_send._profile_name([row,row],'Task','UPDATE')

    def test_existing_preview_buttons_receive_compatible_shape(self):
        from hero_integration.services import mapped_send
        for doctype in ('Customer','Project','Task'):
            with patch.object(mapped_send,'_load_context',return_value=self.context(doctype)), \
                 patch.object(update,'preview',return_value={'hero_id':'5','create':False,'changes':[],'conflicts':[],'fingerprint':'proof','can_send':False}) as preview:
                result=mapped_send.preview(doctype,'DOC',client='client')
                self.assertEqual(result[doctype.lower()],'DOC');self.assertEqual(result['phases'],[])
                self.assertEqual(result['hero'],{'phase_id':None})
                preview.assert_called_once_with('version-two',doctype,'DOC',client='client')

    def test_existing_send_buttons_pass_confirmation_without_bypass(self):
        from hero_integration.services import mapped_send
        import hero_integration.api as api
        for doctype in ('Customer','Project','Task'):
            with patch.object(api,'_assert_mutation_execution_released') as gate, \
                 patch.object(mapped_send,'_load_context',return_value=self.context(doctype)),patch.object(update,'send',return_value={'status':'SENT'}) as send:
                result=mapped_send.send(doctype,'DOC',fingerprint='proof',confirmed=1,client='read',mutation_client='write')
                gate.assert_called_once();self.assertEqual(result['status'],'SENT')
                send.assert_called_once_with('version-two',doctype,'DOC',fingerprint='proof',confirmed=1,client='read',mutation_client='write')

    def test_v2_does_not_silently_accept_legacy_phase_input(self):
        from hero_integration.services import mapped_send
        with patch.object(mapped_send,'_load_context',return_value=self.context('Project')),patch.object(update,'preview') as preview:
            with self.assertRaisesRegex(ValueError,'phase identity'):mapped_send.preview('Project','DOC',phase_id=7)
            preview.assert_not_called()


class TestMappedUpdateContractScope(TestCase):
    def fixture(self, area='HeroTasks'):
        identity = str(uuid4())
        document = SimpleNamespace(operation_name=update.AREAS[area][3], portable_definition_id=identity,
                                   definition_id=uuid4().hex)
        content = {'area': area}
        scope = {'mode': 'VALIDATED_RESOLVER', 'resolver': update.USAGE,
                 'areas': [area], 'mapping_definition_ids': [identity]}
        return document, content, scope

    def check(self, document, content, scope):
        return update._validate_contract_scope(document, content, SimpleNamespace(mapping_json=json.dumps(scope)))

    def test_portable_identity_is_preferred_and_native_hex_fallback_is_canonical(self):
        document, content, scope = self.fixture()
        self.check(document, content, scope)
        document.definition_id = document.portable_definition_id.replace('-', '')
        document.portable_definition_id = ''
        self.check(document, content, scope)
        document.definition_id = scope['mapping_definition_ids'][0]
        self.check(document, content, scope)

    def test_invalid_explicit_portable_identity_never_falls_back(self):
        document, content, scope = self.fixture()
        document.definition_id = scope['mapping_definition_ids'][0].replace('-', '')
        for invalid in ('invalid', str(uuid4()), scope['mapping_definition_ids'][0].replace('-', ''), 0):
            document.portable_definition_id = invalid
            with self.subTest(identity=invalid), self.assertRaisesRegex(ValueError, 'does not authorize'):
                self.check(document, content, scope)

    def test_legacy_admin_input_unknown_keys_and_foreign_identity_are_denied(self):
        document, content, scope = self.fixture()
        for change in ('admin', 'resolver', 'foreign', 'extra', 'invalid_uuid', 'area'):
            candidate = deepcopy(scope)
            if change == 'admin': candidate['mode'] = 'ADMIN_INPUT'
            elif change == 'resolver': candidate['resolver'] = 'UNREVIEWED'
            elif change == 'foreign': candidate['mapping_definition_ids'] = [str(uuid4())]
            elif change == 'extra': candidate['all_mappings'] = True
            elif change == 'invalid_uuid': candidate['mapping_definition_ids'] = ['not-an-identity']
            else: candidate['areas'] = ['HeroCustomers']
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'does not authorize'):
                self.check(document, content, candidate)

    def test_shared_file_operation_does_not_grant_the_other_area_implicitly(self):
        document, content, scope = self.fixture('HeroDocuments')
        picture_id = str(uuid4())
        scope['areas'] = ['HeroPictures']
        with self.assertRaisesRegex(ValueError, 'does not authorize'):
            self.check(document, content, scope)
        scope['areas'] = ['HeroDocuments', 'HeroPictures']
        scope['mapping_definition_ids'].append(picture_id)
        self.check(document, content, scope)
        document.portable_definition_id = picture_id
        self.check(document, {'area': 'HeroPictures'}, scope)
        document.portable_definition_id = str(uuid4())
        with self.assertRaisesRegex(ValueError, 'does not authorize'):
            self.check(document, content, scope)

    def test_duplicate_json_scope_keys_are_not_silently_accepted(self):
        document, content, scope = self.fixture()
        raw = json.dumps(scope)[:-1] + ',"mode":"ADMIN_INPUT"}'
        with self.assertRaisesRegex(ValueError, 'does not authorize'):
            update._validate_contract_scope(document, content, SimpleNamespace(mapping_json=raw))

    def test_scope_rejection_in_load_precedes_service_plan_and_remote_access(self):
        content = mapping();document, _, scope = self.fixture()
        document.name = 'map';document.schema_snapshot = 'snapshot';document.schema_fingerprint = 'fingerprint'
        document.source_instance = 'source';document.status = 'Active';document.direction = 'FRAPPE_TO_MUTATION'
        document.operation_kind = 'MUTATION';document.mapping_json = json.dumps(content);document.check_permission = Mock()
        snapshot = SimpleNamespace(name='snapshot', schema_fingerprint='fingerprint', snapshot_json=json.dumps(SCHEMA),
                                   analysis_status='Validated', execution_contract_status='', check_permission=Mock())
        scope['mapping_definition_ids'] = [str(uuid4())]
        contract = SimpleNamespace(mapping_json=json.dumps(scope), graphql_service='must-not-load')
        native = SimpleNamespace(doctype='Task', name='task')
        docs = {('HERO GraphQL Mapping', 'map'): document, ('HERO GraphQL Schema Snapshot', 'snapshot'): snapshot,
                ('Task', 'task'): native, ('HERO Mutation Contract', 'contract'): contract}
        read = SimpleNamespace(contract=SimpleNamespace(target_operation='HeroTasks', identity_path='id'))
        with patch.object(update.frappe, 'get_doc', side_effect=lambda dt, name: docs[(dt, name)]) as get_doc, \
             patch.object(update.frappe, 'get_all', return_value=['contract']), patch.object(update.frappe, 'has_permission'), \
             patch.object(update.send_helpers, '_roles', return_value=True), patch.object(update, 'validate_sources'), \
             patch('hero_integration.client.settings.get_configuration', return_value=SimpleNamespace(source_instance='source')), \
             patch('hero_integration.services.writeback_source.capture_source', return_value=SimpleNamespace(source_instance='source')), \
             patch.object(update, '_link', return_value=SimpleNamespace(source_id='1')), \
             patch.object(update.send_helpers, '_load_read', return_value=read), \
             patch('hero_integration.services.migration._mapping_plan_for_run') as plan, \
             patch.object(update, '_read_record') as remote:
            with self.assertRaisesRegex(ValueError, 'does not authorize'):
                update._load_context('map', 'Task', 'task')
            plan.assert_not_called();remote.assert_not_called()
            self.assertNotIn(('HERO GraphQL Service', 'must-not-load'), [call.args for call in get_doc.call_args_list])
