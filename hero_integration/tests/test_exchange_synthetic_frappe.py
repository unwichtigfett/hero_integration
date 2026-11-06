"""Native portable exchange on an explicitly selected disposable Frappe site."""
import json
import os
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from hero_integration.services import mapping_draft_exchange as exchange
from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage


class TestSyntheticMappingExchange(TestCase):
    def setUp(self):
        if (not os.environ.get('HERO_TEST_PRIVATE_SITE') or
                os.environ['HERO_TEST_PRIVATE_SITE'] != frappe.local.site or
                os.environ.get('HERO_SYNTHETIC_BINDING_TEST') != '1'):
            self.skipTest('Requires explicitly selected synthetic Frappe site')
        for name in ('hero_integration.client.graphql.HeroGraphQLClient._post',
                     'hero_integration.client.graphql_mutation.HeroMutationClient.execute_once'):
            guard = patch(name,side_effect=AssertionError('Portable exchange must not contact HERO'))
            guard.start()
            self.addCleanup(guard.stop)
        frappe.set_user('Administrator')
        frappe.flags.mute_emails = True
        if not frappe.db.exists('User Type','Website User'):
            frappe.get_doc({'doctype':'User Type','name':'Website User','is_standard':1}).insert()
        self.actor = os.environ.get('HERO_SYNTHETIC_USER','synthetic@example.invalid')
        frappe.get_doc('User',self.actor).add_roles('System Manager','Sales Master Manager')
        frappe.clear_cache(user=self.actor)
        frappe.set_user(self.actor)
        from graphql import build_schema, introspection_from_schema
        self.schema = introspection_from_schema(build_schema('''
            type Query { customers:[Customer] }
            type Customer { id:ID! name:String full_name:String }
            input CustomerInput { name:String }
            type Mutation { updateCustomer(input:CustomerInput):Customer }
        '''))
        self.source = self._snapshot()

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache(user=getattr(self,'actor','Administrator'))
        frappe.set_user('Administrator')
        frappe.clear_messages()

    def _snapshot(self):
        snapshot = frappe.get_doc({'doctype':'HERO GraphQL Schema Snapshot',
            'source_instance':'synthetic-exchange-'+uuid4().hex,'snapshot_json':json.dumps(self.schema)})
        for field in frappe.get_meta(snapshot.doctype).fields:
            if field.reqd and not snapshot.get(field.fieldname):
                snapshot.set(field.fieldname,1 if field.fieldtype in ('Int','Check') else
                    (field.options or '').splitlines()[0] if field.fieldtype == 'Select' else 'synthetic-exchange')
        return snapshot.insert()

    def _roundtrip(self, direction, operation, content):
        identity = str(uuid4())
        definition = {'site_scope':frappe.local.site,'definition_id':identity,
            'source_instance':self.source.source_instance,'snapshot_name':self.source.name,
            'schema_fingerprint':self.source.schema_fingerprint,'direction':direction,
            'operation_kind':'QUERY' if direction == 'QUERY_TO_FRAPPE' else 'MUTATION',
            'operation_name':operation}
        service = FrappeMappingDraftStorage().service()
        source_draft = service.open_new_draft(definition,self.actor,content)
        raw = exchange.export_package(self.source.source_instance,self.source.name,direction,
                                      format_version=exchange.PORTABLE_FORMAT_VERSION)
        self.assertEqual(len(json.loads(raw)['mappings']),1)
        # Remove only this test's source definition through the retained
        # replacement lifecycle, leaving a fresh target identity context.
        discarded = service.discard(identity,self.actor,source_draft.generation,
                                    source_draft.content_revision,confirmed=True)
        exchange._delete_replaced_draft(discarded)
        self.assertFalse(frappe.db.exists('HERO Mapping Draft',source_draft.key))
        # A site permits one snapshot for each fingerprint. Transition only
        # this test's empty source fixture to a fresh target source context.
        frappe.db.delete('HERO GraphQL Schema Snapshot',{'name':self.source.name})
        self.target = self._snapshot()
        preview = exchange.preview_import(raw,self.target.source_instance,self.target.name,direction)
        self.assertTrue(preview['can_save'], preview)
        expected = [dict(definition_id=item['definition_id'],**item['expected']) for item in preview['items']]
        result = exchange.save_import(raw,self.target.source_instance,self.target.name,direction,expected,
                                     confirmed=True,preview_fingerprint=preview['preview_fingerprint'])
        self.assertEqual(result['status'],'SAVED_DRAFTS_ONLY')
        imported = FrappeMappingDraftStorage().service().open_draft(identity,self.actor)
        self.assertEqual(imported.content,content)
        self.assertEqual(imported.new_definition['source_instance'],self.target.source_instance)
        self.assertEqual(imported.new_definition['snapshot_name'],self.target.name)
        self.assertEqual(raw,exchange.export_package(self.target.source_instance,self.target.name,direction,
                                                    format_version=exchange.PORTABLE_FORMAT_VERSION))
        self.assertTrue(frappe.db.exists('HERO Mapping Revision',{'draft_key':imported.key,'actor':self.actor}))
        self.assertFalse(frappe.db.exists('HERO GraphQL Mapping',{'definition_id':identity}))

    def test_query_portable_roundtrip_uses_native_storage_and_retains_ids(self):
        rule_id = str(uuid4())
        self._roundtrip('QUERY_TO_FRAPPE','customers',{'records_path':'customers','identity_path':'id',
            'target_operation':'HeroCustomers','mapping_definition_ids':[rule_id],
            'field_rules':{rule_id:{'source_path':'full_name','source_type':'string|null',
                'target_doctype':'Customer','target_field':'customer_name',
                'purpose':'Record Name','transformation':'Trim','relation':'','priority':1}}})

    def test_mutation_portable_roundtrip_uses_native_storage_without_execution(self):
        self._roundtrip('FRAPPE_TO_MUTATION','updateCustomer',{
            'source':{'doctype':'Customer','field':'customer_name'},
            'target':{'operation':'updateCustomer','argument':'input','input_type':'CustomerInput',
                      'field':'name','graphql_type':'String'},'transformation':'Identity'})
