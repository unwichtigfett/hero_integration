"""Opt-in real Frappe WSGI request tests in the disposable API-05 database."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import get_test_client

ROOT = '/srv/hero-native/implementation-results/HERO_MAPPING_DRAFT_API_IMPLEMENT_05_20261025'
SITES = ROOT + '/sites'
SITE = 'draftapi05.localhost'
DB = 'herodraftapi05'
PREFIX = '/api/method/hero_integration.api.'


def connect():
    assert os.environ.get('HERO_DRAFT_API_TEST') == '1'
    frappe.init(SITE, sites_path=SITES)
    assert frappe.conf.db_name == DB and int(frappe.conf.db_port) == 13328
    frappe.connect()
    frappe.set_user('Administrator')
    frappe.flags.mute_emails = True


def session_for(user):
    from frappe.auth import CookieManager, LoginManager
    from frappe.utils import set_request
    from frappe.sessions import get_csrf_token
    set_request(path='/')
    frappe.local.form_dict = frappe._dict()
    frappe.local.cookie_manager = CookieManager()
    frappe.local.login_manager = LoginManager()
    frappe.local.login_manager.login_as(user)
    token = get_csrf_token()
    frappe.db.commit()
    return frappe.session.sid, token


@unittest.skipUnless(os.environ.get('HERO_DRAFT_API_TEST') == '1', 'Dedicated API-05 site required')
class TestMappingDraftAPIRequest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect()
        frappe.clear_cache()
        cls.request_count = 0
        cls.users = {key:'draft05-'+key+'@example.invalid' for key in ('a','b','employee')}
        if not frappe.db.exists('Role','Employee'):
            frappe.get_doc({'doctype':'Role','role_name':'Employee'}).insert()
        for key, user in cls.users.items():
            if not frappe.db.exists('User', user):
                frappe.get_doc({'doctype':'User','email':user,'first_name':'Synthetic API 05',
                    'send_welcome_email':0,'roles':[{'role':'Employee' if key=='employee' else 'System Manager'}]}).insert()
        frappe.db.commit()
        cls.auth = {}
        # Same real persisted-session fixture used by FrappeAPITestCase.sid.
        # Only the site-directory binding is overridden, never auth or roles.
        for key, user in cls.users.items():
            cls.auth[key] = session_for(user)
        print('API_DB_EVIDENCE', frappe.__version__, frappe.db.sql('select database(),version(),@@tx_isolation,@@port')[0], flush=True)
        frappe.destroy()

    @classmethod
    def tearDownClass(cls):
        print('REAL_WSGI_REQUESTS', cls.request_count, flush=True)

    def setUp(self):
        connect()
        self.definition = 'draft05-' + uuid4().hex
        schema = {'__schema': {'queryType':{'name':'Query'},'types':[
            {'kind':'OBJECT','name':'Query','description':self.definition,'fields':[
                {'name':'readThings','args':[],'type':{'kind':'SCALAR','name':'String'}}]},
            {'kind':'SCALAR','name':'String'}]}}
        snapshot = frappe.get_doc({'doctype':'HERO GraphQL Schema Snapshot', 'source_instance':self.definition,
            'schema_fingerprint':'a'*64,'analysis_status':'Validated','snapshot_json':json.dumps(schema)})
        for field in frappe.get_meta(snapshot.doctype).fields:
            if field.reqd and not snapshot.get(field.fieldname):
                snapshot.set(field.fieldname, 1 if field.fieldtype in ('Int','Check') else
                             (field.options or '').splitlines()[0] if field.fieldtype=='Select' else 'synthetic-api05')
        snapshot.insert()
        self.snapshot = snapshot.name
        self.rows = {}
        for label, status in (('active','Active'),('active2','Active'),('draft','Draft'),('disabled','Disabled')):
            row = frappe.get_doc({'doctype':'HERO GraphQL Mapping','definition_id':self.definition+'-'+label,
                'schema_snapshot':snapshot.name,'operation_kind':'QUERY','operation_name':'readThings',
                'direction':'QUERY_TO_FRAPPE','mapping_revision':1,'status':status,
                'mapping_json':json.dumps({'record_path':'readThings','variables':{'value':'SAMPLE_NOT_VISIBLE'},
                                          'source':{'field':'customer_name','value':'SAMPLE_NOT_VISIBLE'}})})
            row.insert()
            self.rows[label] = row.name
        self.definition += '-active'
        # Existing synthetic field-rule rows, without ERPNext target effects.
        for label,status in (('fieldactive','Active'),('fielddraft','Draft'),('fielddisabled','Disabled')):
            row = frappe.get_doc({'doctype':'HERO Frappe Mapping','name':'draft05-'+uuid4().hex,
                'definition_id':'field05-'+uuid4().hex,'schema_snapshot':snapshot.name,
                'schema_fingerprint':snapshot.schema_fingerprint,'operation':'HeroCustomers',
                'source_path':'id','source_type':'string','target_doctype':'Customer','target_field':'customer_name',
                'purpose':'Field','transformation':'Identity','status':status,'mapping_revision':1})
            row.db_insert()
            self.rows[label] = row.name
        frappe.db.commit()
        frappe.destroy()

    def request(self, user, route, method='GET', payload=None, csrf=True):
        self.__class__.request_count += 1
        client = get_test_client()
        headers = {}
        if user:
            sid, token = self.auth[user]
            client.set_cookie('sid',sid,domain=SITE)
            if csrf: headers['X-Frappe-CSRF-Token'] = token
        with patch('frappe.app._sites_path', SITES):
            response = client.open(route,method=method,json=payload,headers=headers,
                                   base_url='http://'+SITE,buffered=True)
            data = response.get_json(silent=True)
            status = response.status_code
            response.close()
        return status, data

    def rpc(self, user, method, payload=None, verb='GET', **kwargs):
        return self.request(user,PREFIX+method,verb,payload,**kwargs)

    def revisions(self, user):
        """Inspect retained audit records through native Frappe permissions."""
        from hero_integration.services.mapping_draft_service import draft_key
        key = draft_key(SITE, self.definition, self.users[user])
        status, data = self.request(user, '/api/resource/HERO Mapping Revision', payload={
            'fields':json.dumps(['name','actor','content_json','content_hash','generation','revision']),
            'filters':json.dumps({'draft_key':key}), 'order_by':'generation desc, revision desc',
            'limit_page_length':100})
        self.assertEqual(status, 200, data)
        rows = data['data']
        return {'items':[{**row, 'content':json.loads(row['content_json'])} for row in rows], 'total':len(rows)}

    def create(self, user='a'):
        status,data=self.rpc(user,'mapping_draft_create',{'definition_id':self.definition},'POST')
        self.assertEqual(200,status,data)
        return data['message']['draft']

    def save_args(self, revision=0, content=None):
        return {'definition_id':self.definition,'expected_generation':1,'expected_draft_revision':revision,
                'expected_active_revision':1,'mapping_json':json.dumps(self.retained_content(content))}

    def retained_content(self, content):
        # Existing non-editable fixture parameters must be sent unchanged.
        value = dict(content if content is not None else {'source': {}})
        value['variables'] = {'value': 'SAMPLE_NOT_VISIBLE'}
        if isinstance(value.get('source'), dict):
            value['source'] = dict(value['source'], value='SAMPLE_NOT_VISIBLE')
        return value

    def state(self):
        connect()
        try:
            return (frappe.db.count('HERO Mapping Draft',{'definition_id':self.definition}),
                    frappe.db.sql('select name,content_revision,content_hash,owner_user,state from `tabHERO Mapping Draft` where definition_id=%s order by name',(self.definition,)),
                    frappe.db.count('HERO Mapping Revision'))
        finally: frappe.destroy()

    def test_explicit_create_read_save_noop_history_discard(self):
        status,data=self.rpc('a','mapping_draft_read',{'definition_id':self.definition})
        self.assertEqual((200,{'draft':None}),(status,data['message']))
        self.assertEqual(0,self.state()[0])
        draft=self.create()
        self.assertEqual(draft['key'],self.create()['key'])
        self.assertEqual(1,self.state()[0])
        status,data=self.rpc('a','mapping_draft_save',self.save_args(),'POST')
        self.assertEqual(200,status,data)
        self.assertEqual(1,data['message']['draft']['content_revision'])
        before=self.state()
        status,data=self.rpc('a','mapping_draft_save',self.save_args(1),'POST')
        self.assertEqual(200,status,data)
        self.assertTrue(data['message']['unchanged'])
        self.assertEqual(before,self.state())

    def test_native_version_history_cannot_leak_foreign_draft_content(self):
        draft = self.create()
        self.assertEqual(200,self.rpc('a','mapping_draft_save',self.save_args(),'POST')[0])
        connect()
        versions = frappe.get_all('Version',filters={'ref_doctype':'HERO Mapping Draft','docname':draft['key']},pluck='name')
        self.assertTrue(versions, 'fixture must actually produce Frappe Version history')
        frappe.destroy()
        for name in versions:
            self.assertEqual(403,self.request('b','/api/resource/Version/'+name)[0])
            status,data=self.request('b','/api/resource/Version',payload={'fields':'["name","data"]','filters':json.dumps({'name':name})})
            self.assertEqual(200,status,data)
            self.assertEqual([],data['data'])
        history = self.revisions('a')
        self.assertEqual(1, history['total'])
        self.assertEqual(self.users['a'], history['items'][0]['actor'])

    def test_cross_admin_ids_and_native_list_detail_are_private(self):
        a=self.create('a')
        self.rpc('a','mapping_draft_save',self.save_args(),'POST')
        b=self.create('b')
        self.assertNotEqual(a['key'],b['key'])
        self.assertEqual(self.users['a'],a['owner'])
        before=self.state()
        for doctype,name in (('HERO Mapping Draft',a['key']),('HERO Mapping Revision',a['key']+'-1-1')):
            self.assertEqual(403,self.request('b','/api/resource/'+doctype+'/'+name)[0])
            status,data=self.request('b','/api/resource/'+doctype,payload={'fields':'["name"]','limit_page_length':100})
            self.assertEqual(200,status,data)
            self.assertNotIn(name,repr(data))
        for field in ('actor','owner','owner_user','draft_key','ignore_permissions','flags'):
            args=self.save_args();args[field]=self.users['a']
            self.assertEqual(417,self.rpc('b','mapping_draft_save',args,'POST')[0])
        self.assertEqual(before,self.state())

    def test_stale_save_and_rejected_writes_preserve_state(self):
        self.create()
        self.rpc('a','mapping_draft_save',self.save_args(),'POST')
        before=self.state()
        for method,args in (('mapping_draft_save',self.save_args()),):
            status,data=self.rpc('a',method,args,'POST')
            self.assertEqual(417,status,data)
            self.assertEqual('TimestampMismatchError',data['exc_type'])
            self.assertIn('MAPPING_DRAFT_CONFLICT',repr(data))
            self.assertEqual(before,self.state())


    def test_employee_guest_and_alternative_rpc_native_denials(self):
        draft=self.create()
        self.rpc('a','mapping_draft_save',self.save_args(),'POST')
        before=self.state()
        for user in ('employee',None):
            for method,verb,args in (
                ('mapping_draft_create','POST',{'definition_id':self.definition}),
                ('mapping_draft_read','GET',{'definition_id':self.definition})):
                self.assertIn(self.rpc(user,method,args,verb)[0],(403,401))
            for dt,name in (('HERO GraphQL Mapping',self.rows['active']),('HERO Mapping Draft',draft['key']),('HERO Mapping Revision',draft['key']+'-1-1')):
                self.assertIn(self.request(user,'/api/resource/'+dt+'/'+name)[0],(403,401))
                self.assertIn(self.request(user,'/api/method/frappe.client.get',payload={'doctype':dt,'name':name})[0],(403,401))
                self.assertIn(self.request(user,'/api/method/frappe.client.get_value',payload={'doctype':dt,'filters':name,'fieldname':'name'})[0],(403,401))
                self.assertIn(self.request(user,'/api/method/frappe.client.get_count',payload={'doctype':dt})[0],(403,401))
        self.assertEqual(before,self.state())

    def test_role_revocation_denies_existing_own_draft(self):
        self.create()
        before=self.state()
        connect()
        user=frappe.get_doc('User',self.users['a'])
        user.set('roles',[])
        user.save()
        frappe.db.commit()
        frappe.destroy()
        try:
            self.assertIn(self.rpc('a','mapping_draft_read',{'definition_id':self.definition})[0],(401,403))
            self.assertIn(self.rpc('a','mapping_draft_save',self.save_args(),'POST')[0],(401,403))
            self.assertEqual(before,self.state())
        finally:
            connect()
            frappe.get_doc('User',self.users['a']).add_roles('System Manager')
            frappe.db.commit()
            self.auth['a'] = session_for(self.users['a'])
            frappe.destroy()

    def test_http_methods_csrf_and_invalid_inputs(self):
        self.create()
        before=self.state()
        for verb in ('GET','PUT','DELETE'):
            self.assertEqual(403,self.rpc('a','mapping_draft_save',self.save_args(),verb)[0])
            self.assertEqual(403,self.rpc('a','mapping_draft_create',{'definition_id':self.definition},verb)[0])
        self.assertEqual(400,self.rpc('a','mapping_draft_save',self.save_args(),'POST',csrf=False)[0])
        for raw in ('[]','{"source":{},"source":{}}','{"value":NaN}', 'x'*65537):
            args=self.save_args();args['mapping_json']=raw
            self.assertEqual(417,self.rpc('a','mapping_draft_save',args,'POST')[0])
        for invalid in (-1,True,1.5,'bad','9'*5000):
            args=self.save_args();args['expected_draft_revision']=invalid
            self.assertEqual(417,self.rpc('a','mapping_draft_save',args,'POST')[0])
        for name in ('mapping_draft_read',):
            self.assertEqual(403,self.rpc('a',name,{'definition_id':self.definition},'POST')[0])
        self.assertEqual(before,self.state())

    def test_legacy_and_native_write_cannot_change_managed_active_mapping(self):
        draft=self.create()
        before=self.state()
        args={'snapshot_name':self.snapshot,'operation_kind':'QUERY','operation_name':'readThings',
              'direction':'QUERY_TO_FRAPPE','mapping_json':'{"source":{}}','mapping_name':self.rows['active'],'expected_revision':1}
        self.assertEqual(403,self.rpc('b','graphql_mapping_save',args,'POST')[0])
        self.assertEqual(403,self.request('b','/api/resource/HERO GraphQL Mapping/'+self.rows['active'],'PUT',{'mapping_json':'{}'})[0])
        self.assertEqual(403,self.request('b','/api/resource/HERO Mapping Draft/'+draft['key'],'PUT',{'content_json':'{}'})[0])
        self.assertEqual(403,self.request('a','/api/resource/HERO Mapping Draft/'+draft['key'],'PUT',{'content_json':'{}'})[0])
        connect()
        self.assertEqual('Active',frappe.db.get_value('HERO GraphQL Mapping',self.rows['active'],'status'))
        frappe.destroy()
        self.assertEqual(before,self.state())

    def test_foreign_legacy_draft_write_does_not_transfer_last_editor(self):
        before=self.state()
        args={'snapshot_name':self.snapshot,'operation_kind':'QUERY','operation_name':'readThings',
              'direction':'QUERY_TO_FRAPPE','mapping_json':'{"source":{}}','mapping_name':self.rows['draft'],'expected_revision':1}
        self.assertEqual(403,self.rpc('b','graphql_mapping_save',args,'POST')[0])
        self.assertEqual(403,self.request('b','/api/resource/HERO GraphQL Mapping/'+self.rows['draft'],'PUT',{'mapping_json':'{}'})[0])
        connect()
        self.assertEqual('Administrator',frappe.db.get_value('HERO GraphQL Mapping',self.rows['draft'],'modified_by'))
        frappe.destroy()
        self.assertEqual(before,self.state())

    def test_known_definition_drives_personal_draft_roundtrip(self):
        before = self.state()
        identity = self.definition
        for other in (identity, self.definition + '2'):
            status, data = self.rpc('a', 'mapping_draft_read', {'definition_id': other})
            self.assertEqual((200, {'draft': None}), (status, data['message']))
        self.assertEqual(before, self.state(), 'reads must not create drafts/revisions')
        status, data = self.rpc('a', 'mapping_draft_create', {'definition_id': identity}, 'POST')
        self.assertEqual(200, status, data)
        self.assertEqual(identity, data['message']['draft']['definition_id'])
        self.assertEqual(self.rows['active'], data['message']['draft']['base_active_name'])
        args = self.save_args(content={'record_path': 'readThings', 'source': {'field': 'synthetic'}})
        args['definition_id'] = identity
        status, data = self.rpc('a', 'mapping_draft_save', args, 'POST')
        self.assertEqual(200, status, data)
        saved = data['message']['draft']
        self.assertEqual(identity, saved['definition_id'])
        status, data = self.rpc('a', 'mapping_draft_read', {'definition_id': identity})
        self.assertEqual(200, status, data)
        self.assertEqual(saved, data['message']['draft'])
        before = self.state()
        for method, verb, payload in (
            ('mapping_draft_read', 'GET', {'definition_id': identity}),
            ('mapping_draft_create', 'POST', {'definition_id': identity}),
            ('mapping_draft_save', 'POST', args),
        ):
            self.assertEqual(403, self.rpc('employee', method, payload, verb)[0])
        self.assertEqual(before, self.state())
        self.assertEqual(0, self.revisions('b')['total'])
        self.assertIsNone(self.rpc('a', 'mapping_draft_read', {'definition_id': self.rows['active2']})[1]['message']['draft'])
