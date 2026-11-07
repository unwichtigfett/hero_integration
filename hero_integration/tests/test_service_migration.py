"""Service scalar contracts, heterogeneous calculation, taxes, conflicts and resume."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import service_migration as sm, direct_runtime, direct_operations


class TestServiceValues(unittest.TestCase):
    def setUp(self):
        self.read = direct_runtime.builtin_read('HeroServices')
        self.row = {'id': 123, 'company_id': 9, 'name': 'Synthetic service', 'nr': 'SERVICE-1', 'description': '<p>Text</p>',
                    'unit_type': 'Std', 'net_price_per_unit': 0, 'is_deleted': False, 'vat_percent': 0,
                    'quantity': None, 'time_minutes': 90, 'is_fixed_net_price': False, 'modified': 'now'}

    def test_zero_price_zero_tax_hour_unit_and_existing_item_fields(self):
        code, values, barcode, price = sm.service_values(self.row, self.read)
        self.assertEqual(code, 'SERVICE-1')
        self.assertEqual(values['stock_uom'], 'Hour')
        self.assertEqual(values['is_stock_item'], 0)
        self.assertEqual(values['item_group'], 'Services')
        self.assertEqual(values['custom_hero_service_vat_percent'], 0)
        self.assertEqual(price, 0)
        self.assertEqual(values['custom_hero_service_time_minutes'], 90)
        self.assertIsNone(barcode)

    def test_negative_nonfinite_and_boolean_prices_block(self):
        for invalid in [-1, float('nan'), float('inf'), True]:
            self.row['net_price_per_unit'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, sm.ServiceBlocked)):
                sm.service_values(self.row, self.read)

    def test_service_and_article_price_zero_semantics_are_separate(self):
        self.assertIsNone(direct_operations.resolve('HERO Price', 0))
        self.assertEqual(direct_operations.resolve('Service Price', 0), 0)

    def test_queries_validate_against_reference_schema_when_available(self):
        from pathlib import Path
        from graphql import build_client_schema, parse, validate
        from hero_integration.services.graphql_graph import normalize_schema
        from hero_integration.services.graphql_graph import analyze_snapshot
        from hero_integration.services.mapping_contract_v1 import SnapshotAdapter, StoredSnapshot
        import os
        reference = os.environ.get('HERO_REFERENCE_SCHEMA')
        if not reference: self.skipTest('Optional HERO_REFERENCE_SCHEMA unavailable')
        path = Path(reference)
        raw = json.loads(path.read_text()); raw = raw.get('data', raw)
        snapshot = SnapshotAdapter().adapt(StoredSnapshot('reference', raw, analyze_snapshot(raw).fingerprint, 'source'))
        schema = build_client_schema({'__schema': normalize_schema(raw)})
        self.assertEqual(validate(schema, parse(self.read.document)), [])
        request = sm._composition_request(snapshot, [1])
        self.assertEqual(validate(schema, parse(request.document)), [])


class TestCalculation(unittest.TestCase):
    def test_material_identity_never_creates_duplicate_items_and_labor_cost_is_preserved(self):
        positions = [{'__typename':'Documents_SupplyProduct','id':'product-a','name':'Material', 'quantity':2,
                      'unit_type':'Stk','net_price_per_unit':5,'base_price':3},
                     {'__typename':'WageGroup','id':'wage-a','name':'Labor','unit_type':'min','quantity':90,
                      'time_minutes':90,'net_price_per_unit':1.5,'wage_per_hour':90,'wage_cost_price':40,'profit_markup':20}]
        with patch.object(sm,'scoped_link',return_value=None), patch.object(sm,'activity',return_value='Labor'):
            result = sm.composition(positions,'source-a')
        self.assertEqual(result[0]['resolution'],'Unresolved')
        self.assertNotIn('item_code',result[0])
        self.assertEqual(result[1]['activity_type'],'Labor')
        self.assertEqual(result[1]['cost_price'],40)
        self.assertEqual(result[1]['quantity'],90)
        self.assertEqual(json.loads(result[0]['metadata']),{'base_price':3})
        self.assertNotIn('costing_rate',result[1])

    def test_material_only_links_to_a_synchronized_source_binding(self):
        row={'__typename':'Documents_SupplyProduct','id':'product-a','quantity':0}
        with patch.object(sm,'scoped_link',return_value=SimpleNamespace(target_name='ITEM-A',sync_status='Synchronized')):
            result=sm.composition([row],'source-a')
        self.assertEqual(result[0]['item_code'],'ITEM-A')
        self.assertEqual(result[0]['quantity'],0)

    def test_unknown_union_member_and_invalid_quantity_block(self):
        for row in [{'__typename':'Other','id':'x'}, {'__typename':'Documents_SupplyProduct','id':'x','quantity':-1}]:
            with self.assertRaises(sm.ServiceBlocked): sm.composition([row],'source-a')


class TestTaxAndBaseline(unittest.TestCase):
    def test_tax_requires_exactly_one_company_template_and_keeps_zero(self):
        fake=SimpleNamespace(get_all=lambda *a,**k:['zero'],get_doc=lambda *a:SimpleNamespace(taxes=[SimpleNamespace(tax_rate=0)]))
        with patch.object(sm,'frappe',fake):
            self.assertEqual(sm.tax_template('Company',0),'zero')
            with self.assertRaises(sm.ServiceBlocked): sm.tax_template('Company',19)
        fake.get_all=lambda *a,**k:['zero','other-zero']
        with patch.object(sm,'frappe',fake),self.assertRaises(sm.ServiceBlocked):sm.tax_template('Company',0)

    def test_tax_template_supports_multiple_accounts_and_zero_overrides(self):
        fake=SimpleNamespace(get_all=lambda *a,**k:['standard'],get_doc=lambda *a:SimpleNamespace(
            taxes=[SimpleNamespace(tax_rate=19),SimpleNamespace(tax_rate=0),SimpleNamespace(tax_rate=19)]))
        with patch.object(sm,'frappe',fake):
            self.assertEqual(sm.tax_template('Company',19),'standard')
            with self.assertRaises(sm.ServiceBlocked):sm.tax_template('Company',7)

    def test_local_edit_blocks_even_when_source_is_unchanged(self):
        baseline={'item_name':'Original'}
        link=SimpleNamespace(target_field_hashes=json.dumps({'values':baseline}),target_payload_hash=sm.products.digest(baseline))
        with self.assertRaises(sm.ServiceBlocked):sm.baseline_check(link,{'item_name':'Local edit'})

    def test_child_metadata_does_not_create_false_conflicts(self):
        a={'rows':[{'quantity':2,'name':'child-a','parent':'item','idx':1,'doctype':'HERO Service Calculation'}]}
        self.assertEqual(sm.normalized(a,['rows']),{'rows':[{'quantity':2}]})


class TestServiceRun(unittest.TestCase):
    def setUp(self):
        self.saved={}
        self.read=SimpleNamespace(page_size=2,mapping_version='mapping-v1')
        self.rows=[{'id':1,'company_id':9,'modified':'now'},{'id':2,'company_id':9,'modified':'now'}]
        self.fake=SimpleNamespace(only_for=Mock(),log_error=Mock(),db=SimpleNamespace(
            get_default=lambda k:None,get_value=lambda dt,filters,field:self.saved.get(filters['defkey']),set_default=lambda k,v:self.saved.__setitem__(k,v),commit=Mock(),rollback=Mock()))
        self.client=SimpleNamespace(query=lambda document,variables:{'supply_services':[row for row in self.rows if row['id'] in variables['ids']]})
        self.addCleanup(patch.stopall)
        patch.object(sm,'frappe',self.fake).start()
        patch('hero_integration.services.deferred_references.require_storage').start()
        patch.object(sm, 'remember_relationships').start()
        patch.object(sm,'preflight',return_value=('Company','schema-v1')).start()
        patch.object(sm.runtime,'validated_snapshot',return_value=SimpleNamespace(fingerprint='schema-v1')).start()
        patch.object(sm,'_composition_request',side_effect=lambda snapshot, ids: SimpleNamespace(
            document='materialized composition', variables={'ids': ids, 'first': len(ids)})).start()
        self.fetch=patch.object(sm.runtime,'fetch_page',side_effect=lambda client,read,offset:self.rows[offset:]).start()

    def run_sync(self,**kwargs):
        return sm.sync_services(self.client,source_instance='source-a',run_name='run-1',downloader=None,read=self.read,**kwargs)

    def test_atomic_checkpoint_and_failed_identity_is_retried(self):
        with patch.object(sm,'upsert',side_effect=['added',sm.ServiceBlocked('Review')]):
            state=self.run_sync()
        self.assertEqual(state['offset'],1)
        self.assertEqual(state['last_error'],'Review')
        self.fake.db.rollback.assert_called_once()
        with patch.object(sm,'upsert',return_value='added') as write:
            state=self.run_sync()
        self.assertTrue(state['done'])
        self.assertEqual(write.call_count,1)
        self.assertEqual(state['counts']['added'],2)

    def test_cancelled_run_writes_nothing(self):
        with patch.object(sm,'upsert') as write:
            state=self.run_sync(should_stop=lambda:True)
        self.assertTrue(state['stopped']);write.assert_not_called()

    def test_changed_company_or_mapping_blocks_resume(self):
        with patch.object(sm,'upsert',side_effect=sm.ServiceBlocked('Review')): self.run_sync()
        self.read.mapping_version='mapping-v2'
        with self.assertRaises(sm.ServiceBlocked):self.run_sync()

    def test_changed_detail_record_blocks_before_writes(self):
        self.client.query=lambda *a:{'supply_services':[{**row,'modified':'changed'} for row in self.rows]}
        with patch.object(sm,'upsert') as write,self.assertRaises(sm.ServiceBlocked):self.run_sync()
        write.assert_not_called()


class _Document(dict):
    def __getattr__(self, key):return self.get(key)
    def insert(self, **kwargs):
        self['name']=self.get('name') or self.get('item_code') or 'synthetic'
        return self
    def save(self, **kwargs):return self
    def reload(self):return self


class TestServiceUpsert(unittest.TestCase):
    def setUp(self):
        self.row={'id':123,'company_id':9,'nr':'SERVICE-1','name':'Synthetic service','unit_type':'Std',
                  'is_deleted':False,'net_price_per_unit':0,'vat_percent':0,'modified':'now'}
        self.detail={'id':123,'company_id':9,'modified':'now','positions':[],'file_upload':None}
        self.created=[]
        self.fake=SimpleNamespace(db=SimpleNamespace(exists=lambda *a:False),
                                  get_doc=self.get_doc,utils=SimpleNamespace(now_datetime=lambda:'now'))
        self.read=direct_runtime.builtin_read('HeroServices')
        self.addCleanup(patch.stopall)
        patch.object(sm,'frappe',self.fake).start()
        self.get_link=patch.object(sm,'scoped_link',return_value=None).start()
        self.tax=patch.object(sm,'tax_template',return_value='Tax Zero').start()
        self.price=patch.object(sm.products,'plan_price',return_value={'name':None,'rate':0,'uom':'Hour'}).start()
        patch.object(sm.products,'apply_price',return_value={'name':'price','rate':0,'uom':'Hour'}).start()
        self.write=patch.object(sm,'write_link').start()

    def get_doc(self, value, *args, **kwargs):
        if isinstance(value,dict):
            doc=_Document(value);self.created.append(doc);return doc
        raise AssertionError('Unexpected existing document')

    def test_new_service_writes_nonstock_item_tax_price_and_baseline(self):
        self.assertEqual(sm.upsert(self.row,self.detail,self.read,'source-a','Company',None),'added')
        item=next(doc for doc in self.created if doc.doctype=='Item')
        self.assertEqual(item.item_code,'SERVICE-1')
        self.assertEqual(item.is_stock_item,0)
        self.assertEqual(item.stock_uom,'Hour')
        self.assertEqual(item.taxes,[{'item_tax_template':'Tax Zero'}])
        self.assertEqual(item.custom_hero_service_calculation,[])
        self.price.assert_called_once_with('SERVICE-1','Standard Selling',0,'Hour',None)
        self.write.assert_called_once()

    def test_initial_service_import_keeps_component_ids_without_realizing_relationships(self):
        self.detail['positions'] = [{'__typename':'WageGroup', 'id':'labor-a', 'name':'Labor', 'wage_per_hour':60}]
        self.detail['file_upload'] = {'id':99, 'url_download':'https://private.invalid/signed'}
        with patch.object(sm, 'activity', side_effect=AssertionError('Premature activity binding')), \
             patch.object(sm, 'attach_file', side_effect=AssertionError('Premature attachment')):
            self.assertEqual(sm.upsert(self.row,self.detail,self.read,'source-a','Company',None),'added')
        item = next(doc for doc in self.created if doc.doctype=='Item')
        self.assertEqual(item[sm.CALCULATION][0]['source_id'], 'labor-a')
        self.assertEqual(item[sm.CALCULATION][0]['resolution'], 'Unresolved')
        self.assertNotIn('activity_type', item[sm.CALCULATION][0])

    def test_occupied_article_code_never_creates_or_changes_a_master(self):
        self.fake.db.exists=lambda dt,name:dt=='Item'
        with self.assertRaisesRegex(sm.ServiceBlocked,'already used'):
            sm.upsert(self.row,self.detail,self.read,'source-a','Company',None)
        self.assertEqual(self.created,[]);self.tax.assert_not_called();self.write.assert_not_called()

    def test_upsert_checks_local_baseline_before_source_hash_skip(self):
        baseline={'item_name':'Original'}
        link=SimpleNamespace(target_name='SERVICE-1',target_field_hashes=json.dumps({'values':baseline}),
                             target_payload_hash=sm.products.digest(baseline),source_payload_hash='unchanged',sync_status='Synchronized')
        item=_Document(doctype='Item',name='SERVICE-1',item_name='Local edit')
        self.get_link.return_value=link
        self.fake.get_doc=lambda *args,**kwargs:item
        with self.assertRaisesRegex(sm.ServiceBlocked,'edited locally'):
            sm.upsert(self.row,self.detail,self.read,'source-a','Company',None)
        self.tax.assert_not_called();self.write.assert_not_called()

    def mapping_extension(self, local, incoming, field='description'):
        code, fields, barcode, selling = sm.service_values(self.row, self.read)
        fields[field] = incoming
        baseline = {name: value for name, value in fields.items() if name != field}
        item = _Document(doctype='Item', name=code, **baseline, **{field: local})
        link = SimpleNamespace(target_name=code, target_field_hashes=json.dumps({'values': baseline}),
            target_payload_hash=sm.products.digest(baseline), source_payload_hash='previous-mapping', sync_status='Synchronized')
        self.get_link.return_value = link
        self.fake.get_doc = lambda value, *args, **kwargs: item if value == 'Item' else self.get_doc(value, *args, **kwargs)
        return item, patch.object(sm, 'service_values', return_value=(code, fields, barcode, selling))

    def test_mapping_extension_blocks_occupied_fields_before_related_writes(self):
        for field, local, incoming in (('description', 'Local text', 'HERO text'),
                                       ('custom_hero_service_quantity', 0, 3),
                                       ('custom_hero_service_is_fixed_net_price', False, True)):
            with self.subTest(field=field):
                item, mapped = self.mapping_extension(local, incoming, field)
                before = dict(item)
                with mapped, patch.object(sm, 'composition') as composition, \
                     patch.object(sm.products, 'apply_price') as apply_price, patch.object(sm, 'attach_file') as attachment, \
                     self.assertRaisesRegex(sm.ServiceBlocked, 'edited locally'):
                    sm.upsert(self.row, self.detail, self.read, 'source-a', 'Company', None)
                self.assertEqual(item, before)
                self.assertEqual(self.created, [])
                self.tax.assert_not_called(); self.price.assert_not_called(); self.write.assert_not_called()
                composition.assert_not_called(); apply_price.assert_not_called(); attachment.assert_not_called()

    def test_mapping_extension_allows_empty_or_equal_native_values(self):
        for local, incoming in ((None, 'HERO text'), ('', 'HERO text'), ('Same text', 'Same text'), (False, 0)):
            with self.subTest(local=local):
                item, mapped = self.mapping_extension(local, incoming)
                with mapped:
                    result = sm.upsert(self.row, self.detail, self.read, 'source-a', 'Company', None)
                self.assertEqual(result, 'updated')
                self.assertEqual(item.description, incoming)
                self.assertIn('description', self.write.call_args.args[6])

    def test_new_tax_mapping_preserves_local_table_before_related_work(self):
        item, mapped = self.mapping_extension(None, 0, 'custom_hero_service_vat_percent')
        item['taxes'] = [{'item_tax_template': 'Local tax', 'minimum_net_rate': 25}]
        before = dict(item)
        with mapped, patch.object(sm, 'composition') as composition, \
                patch.object(sm, 'activity') as activity, self.assertRaisesRegex(sm.ServiceBlocked, 'edited locally'):
            sm.upsert(self.row, self.detail, self.read, 'source-a', 'Company', None)
        self.assertEqual(item, before)
        self.assertEqual(self.created, [])
        self.tax.assert_not_called(); self.price.assert_not_called(); self.write.assert_not_called()
        composition.assert_not_called(); activity.assert_not_called()

    def test_new_tax_mapping_can_fill_an_empty_native_table(self):
        item, mapped = self.mapping_extension(None, 0, 'custom_hero_service_vat_percent')
        item['taxes'] = []
        with mapped:
            self.assertEqual(sm.upsert(self.row, self.detail, self.read, 'source-a', 'Company', None), 'updated')
        self.assertEqual(item.taxes, [{'item_tax_template': 'Tax Zero'}])
        self.assertIn('taxes', self.write.call_args.args[6])

    def test_zero_or_ambiguous_tax_resolution_blocks_before_item_creation(self):
        self.tax.side_effect=sm.ServiceBlocked('Tax review')
        with self.assertRaises(sm.ServiceBlocked):
            sm.upsert(self.row,self.detail,self.read,'source-a','Company',None)
        self.assertEqual(self.created,[]);self.write.assert_not_called()


class TestServiceAttachments(unittest.TestCase):
    def test_attachment_refresh_materializes_only_the_owning_service_and_file_metadata(self):
        from graphql import build_schema,introspection_from_schema,parse,validate
        from hero_integration.services.graphql_graph import analyze_snapshot
        from hero_integration.services.mapping_contract_v1 import SnapshotAdapter,StoredSnapshot
        schema=build_schema('''
            type Query { supply_services(first:Int,service_ids:[Int]):[Service] }
            type Service { id:Int company_id:Int file_upload:Upload }
            type Upload { id:Int uuid:String filename:String type:String size:Int
                          is_deleted:Boolean modified:String url_download:String }
        ''')
        raw=introspection_from_schema(schema)
        snapshot=SnapshotAdapter().adapt(StoredSnapshot('file-only',raw,analyze_snapshot(raw).fingerprint,'source'))
        request=sm._attachment_request(snapshot,[42])
        self.assertEqual(validate(schema,parse(request.document)),[])
        self.assertNotIn('positions',request.document)
        self.assertIn('url_download',request.document)
        self.assertIn([42],request.variables.values())
        self.assertEqual(len(request.result_bindings),10)

    def test_unchanged_private_file_is_reused_and_local_ownership_edit_blocks(self):
        detail={'id':5,'filename':'synthetic.pdf','modified':'now','size':12,'url_download':'https://example.invalid/files/5'}
        fingerprint=sm.products.digest({k:v for k,v in detail.items() if k!='url_download'})
        prior={'name':'file-1','hash':'synthetic-hash','source':fingerprint}
        file=SimpleNamespace(is_private=1,attached_to_doctype='Item',attached_to_name='SERVICE-1',content_hash='synthetic-hash')
        fake=SimpleNamespace(get_doc=lambda *a,**k:file)
        downloader=SimpleNamespace(fetch=Mock())
        with patch.object(sm,'frappe',fake):
            self.assertEqual(sm.attach_file(detail,SimpleNamespace(name='SERVICE-1'),'source',downloader,prior),prior)
            downloader.fetch.assert_not_called()
            file.attached_to_name='other-item'
            with self.assertRaises(sm.ServiceBlocked):
                sm.attach_file(detail,SimpleNamespace(name='SERVICE-1'),'source',downloader,prior)
            downloader.fetch.assert_not_called()

    def test_start_rejects_missing_schema_before_creating_a_run(self):
        from hero_integration.services import file_migration as fm
        fake=SimpleNamespace(get_doc=Mock())
        with patch.object(fm,'frappe',fake), patch.object(fm,'confirmed_configuration',return_value=SimpleNamespace(source_instance='source')), \
                patch.object(sm,'preflight',side_effect=sm.ServiceBlocked('Schema missing')), \
                patch.object(fm.direct_runtime, 'resolve_read'):
            with self.assertRaises(sm.ServiceBlocked):fm.start(fm.SERVICE_ACTION)
        fake.get_doc.assert_not_called()
