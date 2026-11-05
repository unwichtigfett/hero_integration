"""Regression proofs for complete Query coverage and native contact values."""
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch
import json

from graphql import build_schema, introspection_from_schema
from hero_integration.services import query_mapping as qm
from hero_integration.services import query_mapping_extensions as ext
from hero_integration.services.direct_mapping import MappingSpec, MISSING
from hero_integration.services.mapping_runtime import plan_from_definitions
from hero_integration.services.graphql_graph import analyze_snapshot


class TestCompleteQueryExtensions(TestCase):
    def test_customer_name_priority_and_person_fallback(self):
        spec=MappingSpec('HeroCustomers','full_name','Customer','customer_name','string|null',
                         'Record Name','Customer Display Name',definition_id='name')
        opts={'source_paths':['company_name','full_name','first_name','last_name']}
        for record,expected in [({'company_name':' Firma ','full_name':'Person'},'Firma'),
                                ({'company_name':'','full_name':' Max Muster '},'Max Muster'),
                                ({'company_name':None,'full_name':None,'first_name':'Max','last_name':'Muster'},'Max Muster')]:
            self.assertEqual(ext.source_value(spec,record,opts),expected)
        self.assertIs(ext.source_value(spec,{},opts),MISSING)
        with self.assertRaises(ValueError):ext.source_value(spec,{'full_name':22},opts)

    def test_coalesce_preserves_zero_and_reaches_direct_runtime(self):
        from hero_integration.services.direct_runtime import record_values
        spec=MappingSpec('HeroProducts','quantity_min','Item','min_order_qty','float|null',
                         transformation='Coalesce',definition_id='quantity')
        options={'version':2,'transform_options':{'quantity':{'source_paths':['quantity_min','base_data.quantity_min'],'target_type':'Float'}}}
        plan=NS(definitions=(spec,),adapter_options=options)
        self.assertEqual(record_values(plan,{'quantity_min':None,'base_data':{'quantity_min':2}},'Item'),{'min_order_qty':2.0})
        self.assertEqual(record_values(plan,{'quantity_min':0,'base_data':{'quantity_min':2}},'Item'),{'min_order_qty':0.0})
        self.assertEqual(record_values(plan,{},'Item'),{})

    def test_json_cannot_carry_nonfinite_values_or_skip_serialization(self):
        spec=MappingSpec('HeroServices','positions','Item','custom_hero_positions','json|null',transformation='JSON Value')
        with self.assertRaises(ValueError):qm._check_value(spec,{'value':float('nan')})
        with self.assertRaises(ValueError):qm._validate_output_compatibility(replace(spec,transformation='Identity'),{},'JSON')
        result=qm.transform(spec,[{'__typename':'Product','amount':2}],{'version':2,'transform_options':{}})
        self.assertEqual(json.loads(result)[0]['amount'],2)

    def test_composite_union_query_is_schema_sealed(self):
        schema=build_schema('''type Query {supply_services(first:Int!,offset:Int!):[Service!]!}
            type Service {id:Int! name:String company_id:Int is_deleted:Boolean modified:String positions:[Position!]!}
            union Position = Product | Wage
            type Product {id:Int! amount:Float} type Wage {id:Int! name:String}''')
        raw=introspection_from_schema(schema)
        snapshot=NS(name='snapshot',snapshot_json=raw,source_instance='source-a',analysis_status='Validated',schema_fingerprint=analyze_snapshot(raw).fingerprint)
        specs=[MappingSpec('HeroServices','id','HERO Integration Link','source_id','int','Source Identity','Positive Int To Text',definition_id='id'),
               MappingSpec('HeroServices','name','Item','item_name','string|null','Record Name','Trim',definition_id='name'),
               MappingSpec('HeroServices','positions','Item','custom_hero_positions','json','Field','JSON Value',definition_id='positions')]
        plan=plan_from_definitions('HeroServices',specs,target_operation='HeroServices',adapter_options={'version':2})
        config={'records_path':'supply_services','identity_path':'id','page_size':20,'variables':{'first':'$page_size','offset':'$offset'}}
        document='query services($first:Int!,$offset:Int!){supply_services(first:$first,offset:$offset){id name company_id is_deleted modified positions{__typename ... on Product{id amount} ... on Wage{id name}}}}'
        service=NS(operation_name='services',query_document=document)
        self.assertTrue(qm.seal_query(service,snapshot,config,plan))
        with self.assertRaises(ValueError):qm.seal_query(NS(operation_name='services',query_document=document.replace('... on Wage{id name}','... on Wage @skip(if:true){id name}')),snapshot,config,plan)

    def test_user_resolver_is_source_scoped_and_never_guesses_by_email(self):
        calls=[]
        fake=NS(local=NS(site='site-a'),db=NS(sql=lambda query,args,**kw:(calls.append(args) or [NS(target_name='employee@example.invalid')]),get_value=lambda *a:1),
                get_doc=lambda *a:NS(name=a[-1]),has_permission=lambda *a,**kw:True)
        with patch.dict('sys.modules',{'frappe':fake}):
            self.assertEqual(ext.bound_user(17,'source-b'),'employee@example.invalid')
        self.assertEqual(calls[0][:4],('site-a','source-b','User','17'))

    def test_native_contact_phone_and_designation_use_child_baselines(self):
        from hero_integration.tests.test_contact_persistence import _Document,_fake_frappe
        from hero_integration.integrations.erpnext.contact import synchronize_primary_contact,primary_contact_values
        customer=_Document(name='C1',customer_primary_contact='CONTACT')
        contact=_Document(name='CONTACT',links=[_Document(link_doctype='Customer',link_name='C1')],phone_nos=[],email_ids=[])
        values={'phone':'+49555123','designation':'Leitung'}
        with _fake_frappe(contact):
            synchronize_primary_contact(customer,values);synchronize_primary_contact(customer,values)
            self.assertEqual(primary_contact_values(customer,set(values)),values)
        self.assertEqual(len(contact['phone_nos']),1)
        self.assertEqual(contact.save_calls,1)
