"""Legacy Notes provenance and identity-only Project Customer references stay safe."""
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import mapped_update_v2 as update, project_logbook_writeback as logbook
from hero_integration.services.direct_mapping import MappingSpec, plain_text_html


class TestProjectNotesBaselines(TestCase):
    def setup_case(self, remote='Original', source=None, target=None):
        item={'id':'notes-rule','source':{'doctype':'Project','field':'notes'},'input_path':'project_match.partner_notes',
              'read_path':'partner_notes','query_rule_id':'','resolver':'PLAIN_TEXT','options':{}}
        legacy={'custom_hero_partner_notes':update.send_helpers._hash('Original')}
        link=NS(name='link',source_field_hashes=json.dumps(source if source is not None else legacy),
                target_field_hashes=json.dumps(target if target is not None else legacy))
        rule=MappingSpec('HeroProjectMatches','partner_notes','Project','notes','string|null','Field','Plain Text To HTML')
        ctx=NS(content={'area':'HeroProjectMatches','assignments':[item]},document=NS(doctype='Project'),
               source_links={item['id']:link},query_plan=NS(definitions=[rule]))
        return ctx,item,{'partner_notes':remote},plain_text_html(remote)

    def check(self, *args, **kwargs):
        ctx,item,remote,canonical=self.setup_case(*args,**kwargs)
        return update._baseline_matches(ctx,item,remote,canonical)

    def test_legacy_trim_baseline_accepts_unchanged_remote_with_native_html_canonical_value(self):
        self.assertTrue(self.check())
        self.assertTrue(self.check('  Original  '))
        self.assertFalse(self.check('Changed in HERO'))

    def test_approved_safe_html_query_uses_its_own_canonical_value_and_legacy_trim_proof(self):
        ctx,item,remote,canonical=self.setup_case()
        ctx.query_plan.definitions=[MappingSpec('HeroProjectMatches','partner_notes','Project','notes',
            'string|null','Field','Safe HTML')]
        self.assertTrue(update._baseline_matches(ctx,item,remote,'Original'))
        self.assertFalse(update._baseline_matches(ctx,item,{'partner_notes':'Changed in HERO'},'Changed in HERO'))

    def test_partial_native_baseline_cannot_be_overridden_by_valid_legacy_baseline(self):
        legacy={'custom_hero_partner_notes':update.send_helpers._hash('Original')}
        self.assertFalse(self.check(source={**legacy,'notes':update.send_helpers._hash(plain_text_html('Original'))},target=legacy))
        self.assertFalse(self.check(source=legacy,target={}))
        self.assertFalse(self.check(source={},target=legacy))

    def test_conflicting_aliases_and_html_text_changes_are_not_blindly_accepted(self):
        values={'custom_hero_partner_notes':update.send_helpers._hash('Original'),
                'notes':update.send_helpers._hash(plain_text_html('Another value'))}
        self.assertFalse(self.check(source=values,target=values))
        # The native baseline for plain A equals the literal text <p>A</p>;
        # switching hash encodings would incorrectly hide this real HERO edit.
        native={'notes':update.send_helpers._hash(plain_text_html('A'))}
        self.assertFalse(self.check('<p>A</p>',source=native,target=native))
        old={'custom_hero_partner_notes':update.send_helpers._hash('A')}
        self.assertFalse(self.check('<p>A</p>',source=old,target=old))

    def test_no_baseline_or_unapproved_field_pair_does_not_gain_alias_authority(self):
        self.assertFalse(self.check(source={},target={}))
        ctx,item,remote,canonical=self.setup_case()
        item['source']['field']='custom_notes'
        self.assertFalse(update._baseline_matches(ctx,item,remote,canonical))

    def test_verified_acknowledgement_moves_both_hash_maps_to_native_notes_only(self):
        ctx,item,remote,canonical=self.setup_case()
        new='<p>Locally edited</p>'
        prepared=NS(changes=[{'id':item['id'],'local':new}])
        writer=Mock()
        with patch.object(update,'frappe',NS(db=NS(set_value=writer))),patch.object(update,'_canonical',return_value=new):
            update._acknowledge(ctx,prepared,{'partner_notes':'Locally edited'})
        values=writer.call_args.args[2]
        for key in ('source_field_hashes','target_field_hashes'):
            self.assertEqual(json.loads(values[key]),{'notes':update.send_helpers._hash(new)})


class TestUnchangedProjectCustomer(TestCase):
    def setUp(self):
        self.item={'source':{'doctype':'Project','field':'customer'},'input_path':'project_match.customer_id',
                   'read_path':'customer_id','resolver':'LINK','options':{}}
        self.ctx=NS(content={'area':'HeroProjectMatches'},proof=NS(source_instance='source'))
        self.link=NS(source_id='7',sync_status='Synchronized',source_payload_hash='prior',observed_source_hash='observed')
        self.customer=NS(name='Customer-1')
        def get_all(dt,**kwargs):
            return ['link'] if kwargs['filters']['source_instance']=='source' and kwargs['filters']['target_name']=='Customer-1' else []
        self.fake=NS(get_all=get_all,get_doc=lambda dt,name:self.link if dt=='HERO Integration Link' else self.customer,
                     has_permission=Mock(),get_meta=lambda dt:NS(get_field=lambda name:NS(options='Customer')))
        self.patch=patch.object(update,'frappe',self.fake);self.patch.start();self.addCleanup(self.patch.stop)

    def test_unchanged_relation_uses_fresh_project_identity_without_overwriting_customer(self):
        self.assertEqual(update._wire(self.ctx,self.item,NS(doctype='Project'),'Customer-1',{'customer_id':7}),7)
        with self.assertRaises(update.MappedUpdateBindingError):update._link('Customer','Customer-1','source','customer')

    def test_changed_relation_other_source_bad_identity_or_status_remain_blocked(self):
        for remote in ({'customer_id':8},{'customer_id':'7'},{'customer_id':None}):
            with self.subTest(remote=remote),self.assertRaises(update.MappedUpdateBindingError):
                update._wire(self.ctx,self.item,NS(doctype='Project'),'Customer-1',remote)
        self.ctx.proof.source_instance='other'
        with self.assertRaises(update.MappedUpdateBindingError):update._wire(self.ctx,self.item,NS(doctype='Project'),'Customer-1',{'customer_id':7})
        self.ctx.proof.source_instance='source';self.link.sync_status='Conflict'
        with self.assertRaises(update.MappedUpdateBindingError):update._wire(self.ctx,self.item,NS(doctype='Project'),'Customer-1',{'customer_id':7})

    def test_another_mapping_cannot_use_project_relation_exception(self):
        self.ctx.content['area']='HeroCustomers'
        with self.assertRaises(update.MappedUpdateBindingError):update._wire(self.ctx,self.item,NS(doctype='Project'),'Customer-1',{'customer_id':7})


class TestMeaningfulProjectActivity(TestCase):
    def test_derived_totals_notes_and_privileged_fields_are_omitted_but_status_survives(self):
        fields={name:NS(label=name,fieldtype='Float' if name.startswith('total') else 'Data',read_only=name.startswith('total'),
                        hidden=False,permlevel=1 if name=='private' else 0)
                for name in ('total_costing_amount','total_billable_amount','notes','status','private')}
        version=NS(data=json.dumps({'changed':[['total_costing_amount',0.0,None],['total_billable_amount',0.0,None],
                    ['notes','old','new'],['status','Open','Completed'],['private','x','y']]}))
        with patch.object(logbook,'frappe',NS(get_meta=lambda _:NS(get_field=fields.get),_=lambda text:text)):
            result=logbook._version_text(version)
        self.assertEqual(result,'status: Open → Completed')
