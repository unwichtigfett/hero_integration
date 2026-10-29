from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration import api
from hero_integration.services import mapping_draft_exchange as exchange
from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services import frappe_mapping_draft_storage
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping
from hero_integration.services import frappe_mapping_catalog, schema_provisioning


SCALAR = lambda name: {'kind': 'SCALAR', 'name': name}
SCHEMA = {'__schema': {'queryType': {'name': 'Query'}, 'mutationType': {'name': 'Mutation'}, 'types': [
    {'kind': 'OBJECT', 'name': 'Query', 'fields': [{'name': 'customers', 'args': [],
        'type': {'kind': 'LIST', 'ofType': {'kind': 'OBJECT', 'name': 'Customer'}}}]},
    {'kind': 'OBJECT', 'name': 'Customer', 'fields': [{'name': 'id', 'args': [], 'type': SCALAR('String')},
        {'name': 'email', 'args': [], 'type': SCALAR('String')},
        {'name': 'address', 'args': [], 'type': {'kind':'OBJECT','name':'Address'}}]},
    {'kind':'OBJECT', 'name':'Address', 'fields':[{'name':'line_2','args':[], 'type':SCALAR('String')}]},
    {'kind': 'OBJECT', 'name': 'Mutation', 'fields': [{'name': 'updateCustomer', 'args': [
        {'name': 'input', 'type': {'kind': 'INPUT_OBJECT', 'name': 'CustomerInput'}}],
        'type': {'kind': 'OBJECT', 'name': 'Customer'}}]},
    {'kind': 'INPUT_OBJECT', 'name': 'CustomerInput', 'inputFields': [
        {'name': 'name', 'type': SCALAR('String')},
        {'name': 'requiredLabel', 'type': {'kind': 'NON_NULL', 'ofType': SCALAR('String')}},
        {'name': 'contact', 'type': {'kind': 'INPUT_OBJECT', 'name': 'CustomerContactInput'}},
        {'name': 'tags', 'type': {'kind': 'LIST', 'ofType': SCALAR('String')}}]},
    {'kind': 'INPUT_OBJECT', 'name': 'CustomerContactInput', 'inputFields': [
        {'name': 'email', 'type': SCALAR('String')}]}, {'kind': 'SCALAR', 'name': 'String'}]}}
IDENTITY = 'a16b15fa-7ca8-4722-b63c-ac60cc46629a'


class Row(dict):
    __getattr__ = dict.__getitem__


class Document:
    def __init__(self, store, values):
        self.store, self.values = store, dict(values)
    def update(self, values): self.values.update(values)
    def __getattr__(self, key):
        try: return self.values[key]
        except KeyError: raise AttributeError(key) from None
    def get(self, key, default=None): return self.values.get(key, default)
    def check_permission(self, permission): pass
    def save(self):
        self.values['name'] = self.values['draft_key']
        self.store.drafts[self.values['draft_key']] = dict(self.values)
    def insert(self, **kwargs):
        key = tuple(self.values[k] for k in ('draft_key', 'generation', 'revision', 'kind'))
        self.store.revisions[key] = dict(self.values)


class Fake:
    def __init__(self, site, source, snapshot):
        self.local = SimpleNamespace(site=site, conf={'encryption_key': 'synthetic-key'})
        self.session = SimpleNamespace(user='owner@example.invalid')
        self.drafts, self.revisions, self.savepoints, self.active, self.rules = {}, {}, {}, [], []
        self.customer_name_type = 'Data'
        self.schema_fields = {}
        self.snapshot = SimpleNamespace(name=snapshot, source_instance=source, analysis_status='Validated',
            schema_fingerprint=analyze_snapshot(SCHEMA).fingerprint, snapshot_json=json.dumps(SCHEMA),
            check_permission=lambda *_: None)
        self.db = SimpleNamespace(sql=self.sql, savepoint=self.savepoint, rollback=self.rollback, exists=self.exists,
                                  get_value=lambda *args: ('', ''), has_column=lambda *args: True)
    def exists(self, doctype, filters):
        if doctype == 'DocType': return filters == 'Customer'
        if doctype == 'Custom Field': return (filters['dt'], filters['fieldname']) in self.schema_fields
        return False
    def get_meta(self, doctype):
        def get_field(field):
            value = self.schema_fields.get((doctype, field))
            if not value and doctype == 'Customer' and field in {'customer_name', 'email_id'}:
                value = {'fieldtype': self.customer_name_type if field == 'customer_name' else 'Data',
                         'options':'', 'reqd':0, 'read_only':0, 'hidden':0, 'permlevel':0, 'is_virtual':0}
            return Row(value) if value else None
        return SimpleNamespace(get_field=get_field, get=lambda key: {'module':'Selling', 'custom':0,
            'istable':0, 'issingle':0, 'is_virtual':0}.get(key))
    def get_roles(self): return ['System Manager']
    def only_for(self, role):
        if role not in self.get_roles(): raise PermissionError(role)
    def savepoint(self, name): self.savepoints[name] = (deepcopy(self.drafts), deepcopy(self.revisions))
    def rollback(self, save_point): self.drafts, self.revisions = deepcopy(self.savepoints[save_point])
    def sql(self, query, params=(), **kwargs):
        if query.startswith('show index'):
            return [Row(Column_name='definition_id', Seq_in_index=1, Sub_part=None),
                    Row(Column_name='portable_definition_id', Seq_in_index=1, Sub_part=None, Non_unique=0,
                        Key_name='portable_definition_id')]
        if 'tabHERO GraphQL Mapping' in query:
            if 'where name=%s' in query:
                return [Row(row) for row in self.active if row.get('name') == params[0]]
            if 'portable_definition_id=%s or definition_id=%s' in query:
                return [Row(row) for row in self.active if row.get('portable_definition_id') == params[0] or row['definition_id'] == params[1]]
            if 'portable_definition_id=%s' in query:
                return [Row(row) for row in self.active if row.get('portable_definition_id') == params[0]]
            return [Row(row) for row in self.active if row['definition_id'] == params[0]]
        if 'tabHERO Frappe Mapping' in query:
            if 'where portable_definition_id=%s' in query:
                return [Row(row) for row in self.rules if row.get('portable_definition_id') == params[0]]
            ids = set(params) if ' in (' in query else {params[0]}
            return [Row(row) for row in self.rules if row['definition_id'] in ids]
        if 'tabDocType' in query:
            return [dict(name=params[0], module='Selling', custom=0, istable=0, issingle=0, is_virtual=0)]
        if query.startswith('SELECT fieldtype') and 'tabDocField' in query:
            return []
        if query.startswith('SELECT fieldtype') and 'tabCustom Field' in query:
            value = self.schema_fields.get((params[0], params[1]))
            return [Row(value)] if value else []
        if 'tabDocField' in query:
            if params[0] == 'Address':
                return [dict(name='address-line2', fieldname='address_line2', fieldtype='Data',
                             options='', reqd=0, read_only=0, hidden=0, permlevel=0, is_virtual=0)]
            return [dict(name='customer-custom', fieldname='custom_import_email', fieldtype='Data',
                         options='', reqd=0, read_only=0, hidden=0, permlevel=0, is_virtual=0),
                    dict(name='customer-name', fieldname='customer_name', fieldtype=self.customer_name_type,
                         options='', reqd=0, read_only=0, hidden=0, permlevel=0, is_virtual=0),
                    dict(name='customer-email', fieldname='email_id', fieldtype='Data',
                         options='', reqd=0, read_only=0, hidden=0, permlevel=0, is_virtual=0)] + [
                    dict(name='field-'+name, fieldname=name, fieldtype=value['fieldtype'],
                         options=value.get('options', ''), reqd=0, read_only=0, hidden=0, permlevel=0, is_virtual=0)
                    for (doctype, name), value in self.schema_fields.items() if doctype == params[0]]
        if 'tabCustom Field' in query or 'tabProperty Setter' in query:
            return []
        if 'select owner_user from `tabHERO Mapping Draft`' in query:
            return [Row(owner_user=row['owner_user']) for row in self.drafts.values() if row['definition_id'] == params[0]]
        if query.startswith('select definition_id, owner_user, content_json'):
            needle = params[2].strip('%')
            alternate = params[3].strip('%') if len(params) > 3 else needle
            return [Row(row) for row in self.drafts.values() if row['site_scope'] == params[0]
                    and row['state'] == params[1] and (needle in row['content_json'] or alternate in row['content_json'])]
        if query.startswith('select definition_id from `tabHERO Mapping Draft`'):
            return [Row(row) for row in self.drafts.values() if row['definition_id'] == params[0]]
        if 'tabHERO Mapping Draft' in query:
            row = self.drafts.get(params[0]); return [Row(row)] if row else []
        if 'tabHERO Mapping Revision' in query:
            value = self.revisions.get(tuple(params))
            return [Row(value)] if value else []
        raise AssertionError(query)
    def new_doc(self, doctype): return Document(self, {'doctype': doctype})
    def get_doc(self, *args, **kwargs):
        if isinstance(args[0], dict): return Document(self, args[0])
        if args[0] == 'HERO GraphQL Schema Snapshot': return self.snapshot
        if args[0] == 'HERO Mapping Draft': return Document(self, self.drafts[args[1]])
        if args[0] == 'HERO Frappe Mapping': return Document(self, next(row for row in self.rules if row['name'] == args[1]))
        raise AssertionError(args)
    def get_all(self, doctype, filters, fields, **kwargs):
        if doctype == 'Custom Field':
            value = self.schema_fields.get((filters['dt'], filters['fieldname']))
            return [Row(value)] if value else []
        rows = self.drafts.values() if doctype == 'HERO Mapping Draft' else self.rules if doctype == 'HERO Frappe Mapping' else self.active
        return [Row(row) for row in rows if all(row.get(k) == v for k, v in filters.items())]


class FakePickerMetadata:
    def doctypes(self):
        return [{'name': 'Customer', 'module': 'Selling', 'app_name': 'erpnext', 'istable': 0,
                 'issingle': 0, 'is_virtual': 0}]
    def can_write(self, doctype): return doctype == 'Customer'
    def fields(self, doctype):
        return [SimpleNamespace(fieldname='customer_name', fieldtype='Data', label='Customer Name',
                                read_only=0, hidden=0, permlevel=0, is_virtual=0),
                SimpleNamespace(fieldname='email_id', fieldtype='Data', label='Email',
                                read_only=0, hidden=0, permlevel=0, is_virtual=0)]


class FakeRuleMetadata:
    def ensure_write(self, doctype):
        if doctype not in {'Customer', 'Address'}: raise PermissionError(doctype)
    def field(self, doctype, field):
        if (doctype, field) not in {('Customer', 'custom_import_email'), ('Customer', 'custom_hero_email'),
                                    ('Address', 'address_line2')}: return None
        return SimpleNamespace(fieldtype='Data', read_only=0, is_custom_field=True)


class TestPortableDraftExchange(unittest.TestCase):
    def context(self, fake):
        stack = __import__('contextlib').ExitStack()
        stack.enter_context(patch.object(exchange, 'frappe', fake))
        stack.enter_context(patch.object(api, 'frappe', fake))
        stack.enter_context(patch.object(schema_provisioning, 'frappe', fake))
        def candidates(f, identity):
            compact = identity.replace('-', '')
            return [Row(dict(row, draft_key=key, active_direction=next(
                    (parent['direction'] for parent in f.active if parent.get('name') == row.get('base_active_mapping')), None)))
                    for key, row in f.drafts.items()
                    if row['site_scope'] == f.local.site and row['state'] == 'OPEN'
                    and (identity in row['content_json'] or compact in row['content_json'])]
        stack.enter_context(patch.object(frappe_mapping_draft_storage, '_fresh_identity_candidates', side_effect=candidates))
        stack.enter_context(patch.object(frappe_mapping_draft_storage, '_fresh_definition_candidates',
            side_effect=lambda f, identity: [Row(draft_key=key) for key, row in f.drafts.items()
                                             if row['site_scope'] == f.local.site and row['definition_id'] == identity]))
        stack.enter_context(patch.object(exchange, 'FrappeMappingDraftStorage', side_effect=lambda: FrappeMappingDraftStorage(fake)))
        stack.enter_context(patch.object(exchange, '_lock_context'))
        stack.enter_context(patch.object(hero_frappe_mapping, '_lock_active_target'))
        stack.enter_context(patch.object(hero_frappe_mapping, '_FrappeMetadata', FakeRuleMetadata))
        stack.enter_context(patch.object(frappe_mapping_catalog, 'FrappePickerMetadata', FakePickerMetadata))
        return stack

    def test_identity_candidates_lock_only_exact_references_in_key_order(self):
        fake = Fake('target.test', 'source-B', 'snapshot-B')
        rule_id = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        matching = {'mapping_definition_ids':[rule_id], 'field_rules':{rule_id:{}}}
        unrelated = {'mapping_definition_ids':[], '_portable_exchange_schema':{'id':rule_id}}
        for key, value in [('z', matching), ('a', matching), ('unrelated', unrelated)]:
            fake.drafts[key] = {'draft_key':key, 'definition_id':key, 'owner_user':'owner@example.invalid',
                'site_scope':'target.test', 'state':'OPEN', 'content_json':json.dumps(value),
                'new_definition_json':json.dumps({'direction':'QUERY_TO_FRAPPE'})}
        calls = []
        original = fake.sql
        def sql(query, params=(), **kwargs):
            if 'where draft_key=%s for update' in query:
                calls.append(params[0])
            return original(query, params, **kwargs)
        fake.db.sql = sql
        with self.context(fake):
            rows = frappe_mapping_draft_storage.lock_identity_drafts(fake, [rule_id], 'rule')
        self.assertEqual([row.draft_key for row in rows], ['a', 'z'])
        self.assertEqual(calls, ['a', 'z'])

    def test_candidate_discovery_uses_fresh_nonlocking_connection(self):
        import frappe.database
        calls = []
        db = SimpleNamespace(sql=lambda query, values, **kwargs: calls.append((query, values)) or [],
                             close=lambda: calls.append(('close', ())))
        conf = SimpleNamespace(db_socket=None, db_host='synthetic', db_port=0, db_user='fixture',
                               db_name='fixture', db_password='synthetic')
        fake = SimpleNamespace(local=SimpleNamespace(site='target.test', conf=conf))
        with patch.object(frappe.database, 'get_db', return_value=db):
            self.assertEqual(frappe_mapping_draft_storage._fresh_identity_candidates(fake, IDENTITY), [])
            self.assertEqual(frappe_mapping_draft_storage._fresh_definition_candidates(fake, IDENTITY), [])
        self.assertEqual(len([call for call in calls if call[0] == 'close']), 2)
        self.assertTrue(all('for update' not in query.lower() for query, _ in calls))

    def test_missing_identity_index_fails_closed(self):
        fake = Fake('target.test', 'source-B', 'snapshot-B')
        fake.db.sql = lambda *args, **kwargs: []
        with self.assertRaisesRegex(ValueError, 'index is not installed'):
            frappe_mapping_draft_storage.assert_draft_identity_index(fake)

    def test_missing_unique_identity_index_fails_closed(self):
        fake = Fake('target.test', 'source-B', 'snapshot-B')
        fake.db.sql = lambda *args, **kwargs: [Row(Column_name='portable_definition_id',
            Seq_in_index=1, Sub_part=None, Non_unique=1)]
        with self.assertRaisesRegex(ValueError, 'unique index is not installed'):
            frappe_mapping_draft_storage.assert_portable_identity_schema(fake)

    def test_additional_ids_follow_direction_and_duplicate_rules_are_rejected(self):
        from hero_integration.services.personal_mapping_content import identity_sets
        child = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        assignment = {'id':child, 'source':{'field':'email'}, 'target':{'field':'email_id'},
                      'transformation':'Identity'}
        content = {'additional_mappings':[assignment]}
        self.assertEqual(identity_sets(content, 'QUERY_TO_FRAPPE'), ({child}, set()))
        self.assertEqual(identity_sets(content, 'FRAPPE_TO_MUTATION'), (set(), {child}))
        with self.assertRaisesRegex(ValueError, 'duplicates a selected'):
            identity_sets({**content, 'mapping_definition_ids':[child.replace('-', '')]}, 'QUERY_TO_FRAPPE')

    def test_additional_rule_candidates_obey_direction(self):
        fake = Fake('target.test', 'source-B', 'snapshot-B')
        identity = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        content = json.dumps({'additional_mappings':[{'id':identity, 'source':{'field':'email'},
            'target':{'field':'email_id'}, 'transformation':'Identity'}]})
        for key, direction in [('query', 'QUERY_TO_FRAPPE'), ('mutation', 'FRAPPE_TO_MUTATION')]:
            fake.drafts[key] = {'draft_key':key, 'definition_id':key, 'owner_user':'owner@example.invalid',
                'site_scope':'target.test', 'state':'OPEN', 'content_json':content,
                'new_definition_json':json.dumps({'direction':direction})}
        with self.context(fake):
            self.assertEqual([r.draft_key for r in frappe_mapping_draft_storage.lock_identity_drafts(fake, [identity], 'rule')], ['query'])
            self.assertEqual([r.draft_key for r in frappe_mapping_draft_storage.lock_identity_drafts(fake, [identity], 'child')], ['mutation'])

    def test_additional_identity_lock_plan_uses_direction_namespace(self):
        child = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        content = {'additional_mappings':[{'id':child, 'source':{'field':'email'},
            'target':{'field':'email_id'}, 'transformation':'Identity'}]}
        for direction, expected in [('QUERY_TO_FRAPPE', [('personal-new-definition', IDENTITY),
                                                          ('portable-field-rule', child)]),
                                    ('FRAPPE_TO_MUTATION', [('personal-new-definition', child),
                                                             ('personal-new-definition', IDENTITY)])]:
            fake = Fake('target.test', 'source-B', 'snapshot-B')
            calls = []
            with self.context(fake), patch.object(hero_frappe_mapping, '_lock_active_target',
                    side_effect=lambda namespace, site, identity: calls.append((namespace, identity))):
                FrappeMappingDraftStorage(fake).lock_draft_identities(fake.session.user, IDENTITY, content,
                                                                     direction=direction)
            self.assertEqual(calls, expected)

    def test_legacy_rule_rows_are_locked_in_stable_order(self):
        fake = Fake('target.test', 'source-B', 'snapshot-B')
        fake.rules = [{'definition_id':'z-legacy', 'portable_definition_id':''},
                      {'definition_id':'a-legacy', 'portable_definition_id':''}]
        calls = []
        original = fake.sql
        def sql(query, params=(), **kwargs):
            if query.startswith('select portable_definition_id from `tabHERO Frappe Mapping`'):
                calls.append(params[0])
            return original(query, params, **kwargs)
        fake.db.sql = sql
        with self.context(fake):
            FrappeMappingDraftStorage(fake).lock_draft_identities(fake.session.user, IDENTITY,
                {'mapping_definition_ids':['z-legacy', 'a-legacy']}, direction='QUERY_TO_FRAPPE')
        self.assertEqual(calls, ['a-legacy', 'z-legacy'])

    def test_existing_mapping_direction_is_immutable(self):
        from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping
        from hero_integration.services import mapping_draft_access
        previous = SimpleNamespace(direction='QUERY_TO_FRAPPE', status='Active')
        doc = SimpleNamespace(is_new=lambda: False, get_doc_before_save=lambda: previous,
                              direction='FRAPPE_TO_MUTATION')
        def fail(message): raise ValueError(message)
        fake = SimpleNamespace(only_for=lambda role: None, throw=fail)
        with patch.object(hero_graphql_mapping, 'frappe', fake), patch.object(mapping_draft_access, 'protect_managed_mapping'):
            with self.assertRaisesRegex(ValueError, 'direction is immutable'):
                hero_graphql_mapping.HEROGraphQLMapping.validate(doc)

    def test_existing_mapping_requires_preheld_context_without_acquiring_it(self):
        from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping
        def fail(message): raise ValueError(message)
        fake = SimpleNamespace(local=SimpleNamespace(site='target.test'), db=SimpleNamespace(), throw=fail)
        with patch.object(hero_graphql_mapping, 'frappe', fake), patch.object(hero_frappe_mapping, '_lock_active_target') as lock:
            with self.assertRaisesRegex(ValueError, 'must be locked before'):
                hero_graphql_mapping.require_exchange_context('source', 'snapshot', 'QUERY_TO_FRAPPE')
            lock.assert_not_called()

    def test_existing_rule_requires_preheld_target_without_acquiring_it(self):
        def fail(message): raise ValueError(message)
        fake = SimpleNamespace(local=SimpleNamespace(site='target.test'), db=SimpleNamespace(), throw=fail)
        with patch.object(hero_frappe_mapping, 'frappe', fake), patch.object(hero_frappe_mapping, '_lock_active_target') as lock:
            with self.assertRaisesRegex(ValueError, 'must be locked before'):
                hero_frappe_mapping._require_active_target('operation', 'Customer', 'email')
            lock.assert_not_called()

    def test_child_identity_rejects_existing_definition_and_foreign_draft(self):
        from hero_integration.services.mapping_draft_service import DraftConflict
        fake = Fake('target.test', 'source-B', 'snapshot-B')
        child = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        definition = {'definition_id': IDENTITY}
        additions = [{'id': child}]
        storage = FrappeMappingDraftStorage(fake)
        fake.active.append({'definition_id': 'other-local', 'portable_definition_id': child})
        with self.context(fake):
            with self.assertRaises(DraftConflict):
                storage._assert_parentless_child_identities(definition, additions)
            fake.active.clear()
            fake.drafts['other'] = {'draft_key':'other', 'definition_id':'other-local',
                'owner_user':'other@example.invalid', 'site_scope':'target.test', 'state':'OPEN',
                'content_json':json.dumps({'additional_mappings':[{'id':child, 'source':{'field':'email'},
                    'target':{'field':'email_id'}, 'transformation':'Identity'}]}),
                'new_definition_json':json.dumps({'direction':'FRAPPE_TO_MUTATION'})}
            with self.assertRaises(DraftConflict):
                storage._assert_parentless_child_identities(definition, additions)
            fake.drafts['other']['owner_user'] = fake.session.user
            fake.drafts['other']['definition_id'] = IDENTITY
            storage._assert_parentless_child_identities(definition, additions)

    def test_stale_saved_identity_set_is_rejected_after_locking(self):
        from hero_integration.services.mapping_draft_service import DraftConflict
        fake = Fake('target.test', 'source-B', 'snapshot-B')
        definition = {'site_scope':fake.local.site, 'definition_id':IDENTITY, 'source_instance':'source-B',
            'snapshot_name':'snapshot-B', 'schema_fingerprint':fake.snapshot.schema_fingerprint,
            'direction':'QUERY_TO_FRAPPE', 'operation_kind':'QUERY', 'operation_name':'customers'}
        content = {'records_path':'customers', 'identity_path':'id', 'mapping_definition_ids':[], 'field_rules':{}}
        with self.context(fake):
            storage = FrappeMappingDraftStorage(fake)
            draft = storage.service().open_new_draft(definition, fake.session.user, content)
            storage.lock_draft_identities(fake.session.user, IDENTITY, content)
            changed = {**content, 'mapping_definition_ids':['09a2bc52-4b21-46b7-9a11-3a62bf5a6348']}
            fake.drafts[draft.key]['content_json'] = json.dumps(changed)
            with storage.transaction(IDENTITY, fake.session.user):
                with self.assertRaises(DraftConflict):
                    storage.assert_locked_draft_identities(IDENTITY, fake.session.user)

    def roundtrip(self, direction, kind, operation, content):
        source, target = Fake('source.test', 'source-A', 'snapshot-A'), Fake('target.test', 'source-B', 'snapshot-B')
        definition = {'site_scope': source.local.site, 'definition_id': IDENTITY, 'source_instance': 'source-A',
            'snapshot_name': 'snapshot-A', 'schema_fingerprint': source.snapshot.schema_fingerprint,
            'direction': direction, 'operation_kind': kind, 'operation_name': operation}
        with self.context(source):
            FrappeMappingDraftStorage(source).service().open_new_draft(definition, source.session.user, content)
            payload = exchange.export_package('source-A', 'snapshot-A', direction, format_version=exchange.PORTABLE_FORMAT_VERSION)
        with self.context(target):
            preview = exchange.preview_import(payload, 'source-B', 'snapshot-B', direction)
            states = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            result = exchange.save_import(payload, 'source-B', 'snapshot-B', direction, states,
                                          confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            self.assertFalse(result['activation'])
            reopened = FrappeMappingDraftStorage(target).service().open_draft(IDENTITY, target.session.user)
            self.assertEqual(reopened.content, content)
            stored_plan = FrappeMappingDraftStorage(target).materialize_portable_draft(
                IDENTITY, target.session.user, reopened.generation, reopened.content_revision, 0, 0)
            self.assertEqual(stored_plan['execution_authority'], 'NONE')
            if content.get('additional_mappings'):
                self.assertEqual(len(stored_plan['input_paths']), 1 + len(content['additional_mappings']))
            from hero_integration.services.mapping_draft_service import DraftConflict
            with self.assertRaises(DraftConflict):
                FrappeMappingDraftStorage(target).materialize_portable_draft(
                    IDENTITY, target.session.user, reopened.generation, reopened.content_revision + 1, 0, 0)
            from hero_integration.services.mapping_contract_v1 import (
                SnapshotAdapter, StoredSnapshot, QueryAdapter, QueryDraft, MutationAdapter, MutationDraft,
            )
            validated = SnapshotAdapter().adapt(StoredSnapshot('snapshot-B', SCHEMA,
                target.snapshot.schema_fingerprint, 'source-B'))
            if direction == 'QUERY_TO_FRAPPE':
                paths = tuple(sorted({content['identity_path']} | {rule['source_path'] for rule in content.get('field_rules', {}).values()}))
                plan = QueryAdapter().materialize(validated, QueryDraft('portable', 'customers', 'customers', paths, {}))
                self.assertTrue(plan.document)
            else:
                action = reopened.content['target']
                plan = MutationAdapter().materialize(validated, MutationDraft(action['operation'],
                    ((action['argument'], action['field']),), 'source-B', reopened.content_revision))
                self.assertFalse(plan['executable'])
            self.assertEqual(payload, exchange.export_package('source-B', 'snapshot-B', direction,
                             format_version=exchange.PORTABLE_FORMAT_VERSION))
            preview = exchange.preview_import(payload, 'source-B', 'snapshot-B', direction)
            states = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            repeated = exchange.save_import(payload, 'source-B', 'snapshot-B', direction, states,
                                             confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            self.assertEqual(repeated['drafts']['unchanged'], [IDENTITY])
            self.assertEqual(len(target.drafts), 1)
            self.assertNotEqual(next(iter(source.drafts)), next(iter(target.drafts)))

    def test_query_roundtrip(self):
        rule_id = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        self.roundtrip('QUERY_TO_FRAPPE', 'QUERY', 'customers',
                       {'records_path': 'customers', 'identity_path': 'id', 'target_operation': 'HeroCustomers',
                        'mapping_definition_ids': [rule_id], 'field_rules': {rule_id: {
                            'source_path': 'email', 'source_type': 'string|null', 'target_doctype': 'Customer',
                            'target_field': 'custom_import_email', 'purpose': 'Field', 'transformation': 'Trim',
                            'relation': '', 'priority': 5}}})

    def test_query_reviewed_nested_address_relationship_roundtrip(self):
        rule_id = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        self.roundtrip('QUERY_TO_FRAPPE', 'QUERY', 'customers',
            {'records_path':'customers', 'identity_path':'id', 'target_operation':'HeroCustomers',
             'mapping_definition_ids':[rule_id], 'field_rules':{rule_id:{'source_path':'address.line_2',
                'source_type':'string|null', 'target_doctype':'Address', 'target_field':'address_line2',
                'purpose':'Native Binding', 'transformation':'Trim', 'relation':'Customer Primary Address', 'priority':5}}})

    def test_parentless_rule_edit_saves_and_reexports_updated_rule(self):
        fake = Fake('target.test', 'source-B', 'snapshot-B')
        rule_id = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        content = {'records_path':'customers', 'identity_path':'id', 'target_operation':'HeroCustomers',
                   'mapping_definition_ids':[rule_id], 'field_rules':{rule_id:{'source_path':'email',
                   'source_type':'string|null', 'target_doctype':'Customer', 'target_field':'custom_import_email',
                   'purpose':'Field', 'transformation':'Trim', 'relation':'', 'priority':5}}}
        definition = {'site_scope':fake.local.site, 'definition_id':IDENTITY, 'source_instance':'source-B',
            'snapshot_name':'snapshot-B', 'schema_fingerprint':fake.snapshot.schema_fingerprint,
            'direction':'QUERY_TO_FRAPPE', 'operation_kind':'QUERY', 'operation_name':'customers'}
        with self.context(fake):
            service = FrappeMappingDraftStorage(fake).service()
            first = service.open_new_draft(definition, fake.session.user, content)
            updated = deepcopy(first.content)
            updated['field_rules'][rule_id]['transformation'] = 'Identity'
            service.save(IDENTITY, fake.session.user, 1, 0, updated, expected_generation=1)
            reopened = service.open_draft(IDENTITY, fake.session.user)
            self.assertEqual(reopened.content['field_rules'][rule_id]['transformation'], 'Identity')
            package = json.loads(exchange.export_package('source-B', 'snapshot-B', 'QUERY_TO_FRAPPE',
                                                         format_version=exchange.PORTABLE_FORMAT_VERSION))
            self.assertEqual(package['mappings'][0]['content']['field_rules'][rule_id]['transformation'], 'Identity')

    def test_mutation_roundtrip(self):
        self.roundtrip('FRAPPE_TO_MUTATION', 'MUTATION', 'updateCustomer',
            {'source': {'doctype': 'Customer', 'field': 'customer_name'},
             'target': {'operation': 'updateCustomer', 'argument': 'input', 'input_type': 'CustomerInput',
                        'field': 'name', 'graphql_type': 'String'}, 'transformation': 'Identity'})

    def test_mutation_multi_assignment_nested_input_roundtrip(self):
        self.roundtrip('FRAPPE_TO_MUTATION', 'MUTATION', 'updateCustomer',
            {'source': {'doctype': 'Customer', 'field': 'customer_name'},
             'target': {'operation': 'updateCustomer', 'argument': 'input', 'input_type': 'CustomerInput',
                        'field': 'name', 'graphql_type': 'String'}, 'transformation': 'Identity',
             'additional_mappings': [{'id': '9d8fe02d-9eb0-49b1-a599-e0630f421010',
                'source': {'doctype': 'Customer', 'field': 'email_id'},
                'target': {'operation': 'updateCustomer', 'argument': 'input', 'input_type': 'CustomerInput',
                           'field': 'contact.email', 'graphql_type': 'String'}, 'transformation': 'Trim'},
               {'id': '21e99ce9-7e38-40f6-a36d-21aa33e43c98', 'source': {'doctype': 'Customer', 'field': 'customer_name'},
                'target': {'operation': 'updateCustomer', 'argument': 'input', 'input_type': 'CustomerInput',
                           'field': 'requiredLabel', 'graphql_type': 'String!'}, 'transformation': 'Identity'}]})

    def test_mutation_list_input_is_rejected_without_a_collection_contract(self):
        target = Fake('target.test', 'source-B', 'snapshot-B')
        content = {'source': {'doctype':'Customer', 'field':'customer_name'},
            'target': {'operation':'updateCustomer', 'argument':'input', 'input_type':'CustomerInput',
                       'field':'tags', 'graphql_type':'[String]'}, 'transformation':'Identity'}
        package = {'format_version':exchange.PORTABLE_FORMAT_VERSION, 'schema_fingerprint':target.snapshot.schema_fingerprint,
            'direction':'FRAPPE_TO_MUTATION', 'schema':{'custom_doctypes':[], 'custom_fields':[], 'relations':[]},
            'mappings':[{'definition_id':IDENTITY, 'operation_kind':'MUTATION', 'operation_name':'updateCustomer',
                'direction':'FRAPPE_TO_MUTATION', 'content':content, 'dependencies':[]}]}
        with self.context(target):
            with self.assertRaises(ValueError):
                exchange.preview_import(json.dumps(package), 'source-B', 'snapshot-B', 'FRAPPE_TO_MUTATION')
            self.assertEqual(target.drafts, {})

    def test_mutation_assignment_identity_collision_is_rejected_before_writes(self):
        target = Fake('target.test', 'source-B', 'snapshot-B')
        child_id = '9d8fe02d-9eb0-49b1-a599-e0630f421010'
        content = {'source': {'doctype':'Customer', 'field':'customer_name'},
            'target': {'operation':'updateCustomer', 'argument':'input', 'input_type':'CustomerInput',
                       'field':'name', 'graphql_type':'String'}, 'transformation':'Identity',
            'additional_mappings':[{'id':child_id, 'source':{'doctype':'Customer','field':'email_id'},
                'target':{'operation':'updateCustomer','argument':'input','input_type':'CustomerInput',
                          'field':'contact.email','graphql_type':'String'},'transformation':'Trim'}]}
        package = {'format_version':exchange.PORTABLE_FORMAT_VERSION, 'schema_fingerprint':target.snapshot.schema_fingerprint,
            'direction':'FRAPPE_TO_MUTATION', 'schema':{'custom_doctypes':[], 'custom_fields':[], 'relations':[]},
            'mappings':[{'definition_id':IDENTITY, 'operation_kind':'MUTATION', 'operation_name':'updateCustomer',
                'direction':'FRAPPE_TO_MUTATION', 'content':content, 'dependencies':[]}]}
        target.active.append({'definition_id':child_id, 'portable_definition_id':'', 'name':'existing-child'})
        with self.context(target):
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'assignment identity'):
                exchange.preview_import(json.dumps(package), 'source-B', 'snapshot-B', 'FRAPPE_TO_MUTATION')
            self.assertEqual(target.drafts, {})
            target.active.clear()
            target.drafts['foreign'] = {'draft_key':'foreign', 'definition_id':'other-definition', 'owner_user':'other@example.invalid',
                'site_scope':'target.test', 'state':'OPEN', 'content_json':json.dumps(content),
                'new_definition_json':json.dumps({'direction':'FRAPPE_TO_MUTATION'})}
            with self.assertRaisesRegex(ValueError, 'different draft'):
                exchange.preview_import(json.dumps(package), 'source-B', 'snapshot-B', 'FRAPPE_TO_MUTATION')
            self.assertEqual(len(target.drafts), 1)

    def test_foreign_parentless_rule_identity_conflict_is_rejected(self):
        target = Fake('target.test', 'source-B', 'snapshot-B')
        rule_id = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        rule = {'source_path':'email', 'source_type':'string|null', 'target_doctype':'Customer',
            'target_field':'custom_import_email', 'purpose':'Field', 'transformation':'Trim', 'relation':'', 'priority':5}
        content = {'records_path':'customers', 'identity_path':'id', 'target_operation':'HeroCustomers',
                   'mapping_definition_ids':[rule_id], 'field_rules':{rule_id:rule}}
        package = {'format_version':exchange.PORTABLE_FORMAT_VERSION, 'schema_fingerprint':target.snapshot.schema_fingerprint,
            'direction':'QUERY_TO_FRAPPE', 'schema':{'custom_doctypes':[], 'custom_fields':[], 'relations':[]},
            'mappings':[{'definition_id':IDENTITY, 'operation_kind':'QUERY', 'operation_name':'customers',
                'direction':'QUERY_TO_FRAPPE', 'content':content, 'dependencies':[]}]}
        hex_identity = rule_id.replace('-', '')
        foreign_content = {**content, 'mapping_definition_ids':[hex_identity], 'field_rules':{hex_identity:rule}}
        target.drafts['foreign'] = {'draft_key':'foreign', 'definition_id':'other-definition', 'owner_user':'other@example.invalid',
            'site_scope':'target.test', 'state':'OPEN', 'content_json':json.dumps(foreign_content),
            'new_definition_json':json.dumps({'source_instance':'source-B','snapshot_name':'snapshot-B','direction':'QUERY_TO_FRAPPE'})}
        with self.context(target):
            with self.assertRaisesRegex(ValueError, 'different draft'):
                exchange.preview_import(json.dumps(package), 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            self.assertEqual(len(target.drafts), 1)
            target.drafts['foreign']['content_json'] = json.dumps({'additional_mappings':[{
                'id':rule_id, 'source':{'field':'email'}, 'target':{'doctype':'Customer','field':'email_id'},
                'transformation':'Trim'}]})
            with self.assertRaisesRegex(ValueError, 'different draft'):
                exchange.preview_import(json.dumps(package), 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            self.assertEqual(len(target.drafts), 1)

    def test_active_query_export_fresh_import_and_existing_owned_update(self):
        source, target = Fake('source.test', 'source-A', 'snapshot-A'), Fake('target.test', 'source-B', 'snapshot-B')
        content = {'records_path': 'customers', 'identity_path': 'id', 'mapping_definition_ids': []}
        def active(fake, local_id, name):
            return {'name': name, 'definition_id': local_id, 'portable_definition_id': IDENTITY,
                    'source_instance': fake.snapshot.source_instance, 'schema_snapshot': fake.snapshot.name,
                    'schema_fingerprint': fake.snapshot.schema_fingerprint, 'direction': 'QUERY_TO_FRAPPE',
                    'operation_kind': 'QUERY', 'operation_name': 'customers', 'status': 'Active',
                    'mapping_revision': 2, 'mapping_json': json.dumps(content)}
        source.active.append(active(source, 'legacy-local-A', 'DOC-A'))
        with self.context(source):
            raw = exchange.export_package('source-A', 'snapshot-A', 'QUERY_TO_FRAPPE',
                                          format_version=exchange.PORTABLE_FORMAT_VERSION)
        with self.context(target):
            preview = exchange.preview_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            expected = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            exchange.save_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE', expected,
                                 confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            self.assertEqual(FrappeMappingDraftStorage(target).service().open_draft(IDENTITY, target.session.user).content, content)
        existing = Fake('existing.test', 'source-C', 'snapshot-C')
        local_active = active(existing, 'legacy-local-C', 'DOC-C')
        local_active['mapping_json'] = json.dumps({**content, 'identity_path': 'email'})
        existing.active.append(local_active)
        with self.context(existing):
            preview = exchange.preview_import(raw, 'source-C', 'snapshot-C', 'QUERY_TO_FRAPPE')
            self.assertEqual(preview['items'][0]['local_definition_id'], 'legacy-local-C')
            expected = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            result = exchange.save_import(raw, 'source-C', 'snapshot-C', 'QUERY_TO_FRAPPE', expected,
                                          confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            self.assertFalse(result['activation'])
            self.assertEqual(FrappeMappingDraftStorage(existing).service().open_draft('legacy-local-C', existing.session.user).content, content)
            self.assertEqual(existing.active[0]['name'], 'DOC-C')
            self.assertEqual(json.loads(existing.active[0]['mapping_json'])['identity_path'], 'email')

    def test_active_mutation_export_fresh_target(self):
        source, target = Fake('source.test', 'source-A', 'snapshot-A'), Fake('target.test', 'source-B', 'snapshot-B')
        content = {'source': {'doctype': 'Customer', 'field': 'customer_name'},
                   'target': {'operation': 'updateCustomer', 'argument': 'input', 'input_type': 'CustomerInput',
                              'field': 'name', 'graphql_type': 'String'}, 'transformation': 'Identity',
                   'additional_mappings': [{'id':'9d8fe02d-9eb0-49b1-a599-e0630f421010',
                       'source':{'doctype':'Customer','field':'email_id'},
                       'target':{'operation':'updateCustomer','argument':'input','input_type':'CustomerInput',
                                 'field':'contact.email','graphql_type':'String'}, 'transformation':'Trim'},
                       {'id':'21e99ce9-7e38-40f6-a36d-21aa33e43c98',
                        'source':{'doctype':'Customer','field':'customer_name'},
                        'target':{'operation':'updateCustomer','argument':'input','input_type':'CustomerInput',
                                  'field':'requiredLabel','graphql_type':'String!'}, 'transformation':'Identity'}]}
        source.active.append({'name': 'DOC-A', 'definition_id': 'legacy-mutation-A', 'portable_definition_id': IDENTITY,
            'source_instance': 'source-A', 'schema_snapshot': 'snapshot-A', 'schema_fingerprint': source.snapshot.schema_fingerprint,
            'direction': 'FRAPPE_TO_MUTATION', 'operation_kind': 'MUTATION', 'operation_name': 'updateCustomer',
            'status': 'Active', 'mapping_revision': 1, 'mapping_json': json.dumps(content)})
        with self.context(source):
            raw = exchange.export_package('source-A', 'snapshot-A', 'FRAPPE_TO_MUTATION',
                                          format_version=exchange.PORTABLE_FORMAT_VERSION)
        with self.context(target):
            preview = exchange.preview_import(raw, 'source-B', 'snapshot-B', 'FRAPPE_TO_MUTATION')
            expected = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            exchange.save_import(raw, 'source-B', 'snapshot-B', 'FRAPPE_TO_MUTATION', expected,
                                 confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            draft = FrappeMappingDraftStorage(target).service().open_draft(IDENTITY, target.session.user)
            self.assertEqual(draft.content, content)
            plan = FrappeMappingDraftStorage(target).materialize_portable_draft(IDENTITY, target.session.user, 1, 1, 0, 0)
            self.assertEqual(len(plan['input_paths']), 3)
            self.assertEqual(raw, exchange.export_package('source-B', 'snapshot-B', 'FRAPPE_TO_MUTATION',
                                                          format_version=exchange.PORTABLE_FORMAT_VERSION))
        existing = Fake('existing.test', 'source-C', 'snapshot-C')
        old_content = {**content, 'transformation': 'Trim', 'additional_mappings': []}
        existing.active.append({'name':'DOC-C', 'definition_id':'legacy-mutation-C',
            'portable_definition_id':IDENTITY, 'source_instance':'source-C', 'schema_snapshot':'snapshot-C',
            'schema_fingerprint':existing.snapshot.schema_fingerprint, 'direction':'FRAPPE_TO_MUTATION',
            'operation_kind':'MUTATION', 'operation_name':'updateCustomer', 'status':'Active',
            'mapping_revision':4, 'mapping_json':json.dumps(old_content)})
        with self.context(existing):
            preview = exchange.preview_import(raw, 'source-C', 'snapshot-C', 'FRAPPE_TO_MUTATION')
            states = [dict(definition_id=item['definition_id'], **item['expected']) for item in preview['items']]
            exchange.save_import(raw, 'source-C', 'snapshot-C', 'FRAPPE_TO_MUTATION', states,
                                 confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            reopened = FrappeMappingDraftStorage(existing).service().open_draft('legacy-mutation-C', existing.session.user)
            self.assertEqual(reopened.content, content)
            self.assertEqual(existing.active[0]['mapping_revision'], 4)
            self.assertEqual(raw, exchange.export_package('source-C', 'snapshot-C', 'FRAPPE_TO_MUTATION',
                                                          format_version=exchange.PORTABLE_FORMAT_VERSION))

    def test_active_query_field_rule_uuid_and_local_provenance_are_projected(self):
        source, target = Fake('source.test', 'source-A', 'snapshot-A'), Fake('target.test', 'source-B', 'snapshot-B')
        local_rule = '09a2bc524b2146b79a113a62bf5a6348'
        content = {'records_path': 'customers', 'identity_path': 'id', 'target_operation': 'HeroCustomers',
                   'mapping_definition_ids': [local_rule]}
        source.active.append({'name': 'DOC-A', 'definition_id': 'legacy-local-A', 'portable_definition_id': IDENTITY,
            'source_instance': 'source-A', 'schema_snapshot': 'snapshot-A', 'schema_fingerprint': source.snapshot.schema_fingerprint,
            'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers',
            'status': 'Active', 'mapping_revision': 2, 'mapping_json': json.dumps(content)})
        source.rules.append({'name': 'RULE-A', 'definition_id': local_rule, 'status': 'Active',
            'portable_definition_id': '09a2bc52-4b21-46b7-9a11-3a62bf5a6348',
            'schema_snapshot': 'snapshot-A', 'schema_fingerprint': source.snapshot.schema_fingerprint,
            'operation': 'HeroCustomers', 'mapping_revision': 1, 'personal_graphql_mapping': 'DOC-A',
            'source_path': 'email', 'source_type': 'string|null', 'target_doctype': 'Customer',
            'target_field': 'custom_import_email', 'purpose': 'Field', 'transformation': 'Trim',
            'relation': '', 'priority': 5, 'schema_manifest': '', 'schema_dependencies': '[]'})
        with self.context(source):
            raw = exchange.export_package('source-A', 'snapshot-A', 'QUERY_TO_FRAPPE',
                                          format_version=exchange.PORTABLE_FORMAT_VERSION)
        package = json.loads(raw)
        portable_rule = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        self.assertEqual(package['mappings'][0]['content']['mapping_definition_ids'], [portable_rule])
        self.assertNotIn('RULE-A', raw)
        self.assertNotIn('field_rule_bases', raw)
        with self.context(target):
            preview = exchange.preview_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            expected = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            exchange.save_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE', expected,
                                 confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            draft = FrappeMappingDraftStorage(target).service().open_draft(IDENTITY, target.session.user)
            self.assertEqual(draft.content['mapping_definition_ids'], [portable_rule])
            self.assertEqual(draft.content['field_rules'][portable_rule]['target_field'], 'custom_import_email')
        existing = Fake('existing.test', 'source-C', 'snapshot-C')
        target_content = {**content, 'mapping_definition_ids': [portable_rule]}
        existing.active.append({'name': 'DOC-C', 'definition_id': 'legacy-local-C', 'portable_definition_id': IDENTITY,
            'source_instance': 'source-C', 'schema_snapshot': 'snapshot-C', 'schema_fingerprint': existing.snapshot.schema_fingerprint,
            'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers',
            'status': 'Active', 'mapping_revision': 7, 'mapping_json': json.dumps(target_content)})
        existing.rules.append({**source.rules[0], 'name': 'RULE-C', 'definition_id': portable_rule,
                               'schema_snapshot': 'snapshot-C', 'personal_graphql_mapping': 'DOC-C'})
        with self.context(existing):
            preview = exchange.preview_import(raw, 'source-C', 'snapshot-C', 'QUERY_TO_FRAPPE')
            self.assertEqual(preview['items'][0]['rule_remapping'], {portable_rule: portable_rule})
            expected = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            exchange.save_import(raw, 'source-C', 'snapshot-C', 'QUERY_TO_FRAPPE', expected,
                                 confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            reopened = FrappeMappingDraftStorage(existing).service().open_draft('legacy-local-C', existing.session.user)
            self.assertEqual(reopened.content['field_rule_bases'][portable_rule]['name'], 'RULE-C')
            self.assertEqual(existing.active[0]['mapping_revision'], 7)
            again = exchange.preview_import(raw, 'source-C', 'snapshot-C', 'QUERY_TO_FRAPPE')
            states = [dict(definition_id=i['definition_id'], **i['expected']) for i in again['items']]
            repeated = exchange.save_import(raw, 'source-C', 'snapshot-C', 'QUERY_TO_FRAPPE', states,
                                             confirmed=True, preview_fingerprint=again['preview_fingerprint'])
            self.assertEqual(repeated['drafts']['unchanged'], [IDENTITY])
            self.assertEqual(len(existing.drafts), 1)

    def test_active_query_schema_relationship_dependencies_roundtrip_without_provisioning(self):
        source, target = Fake('source.test', 'source-A', 'snapshot-A'), Fake('target.test', 'source-B', 'snapshot-B')
        manifest = {'custom_doctypes': [], 'custom_fields': [
            {'id': 'field:email', 'doctype': 'Customer', 'fieldname': 'custom_hero_email',
             'label': 'Email', 'fieldtype': 'Data', 'options': '', 'insert_after': ''},
            {'id': 'field:link', 'doctype': 'Customer', 'fieldname': 'custom_hero_related',
             'label': 'Related', 'fieldtype': 'Link', 'options': 'Customer', 'insert_after': ''}],
            'relations': [{'id': 'relation:customer', 'doctype': 'Customer', 'field_id': 'field:link',
                'fieldname': 'custom_hero_related', 'relation_type': 'Link', 'target_doctype': 'Customer',
                'dependencies': ['field:email', 'field:link']}]}
        for fake in (source, target):
            fake.schema_fields = {('Customer', 'custom_hero_email'): {'fieldtype':'Data', 'options':'', 'label':'Email'},
                                  ('Customer', 'custom_hero_related'): {'fieldtype':'Link', 'options':'Customer', 'label':'Related'}}
        rule_ids = ['09a2bc524b2146b79a113a62bf5a6348', '8a6c58ba08fb4da487e6684bb1b16e19']
        content = {'records_path': 'customers', 'identity_path': 'id', 'target_operation': 'HeroCustomers',
                   'mapping_definition_ids': rule_ids}
        source.active.append({'name':'DOC-A', 'definition_id':'legacy-A', 'portable_definition_id':IDENTITY,
            'source_instance':'source-A', 'schema_snapshot':'snapshot-A', 'schema_fingerprint':source.snapshot.schema_fingerprint,
            'direction':'QUERY_TO_FRAPPE', 'operation_kind':'QUERY', 'operation_name':'customers',
            'status':'Active', 'mapping_revision':2, 'mapping_json':json.dumps(content)})
        for index, identity in enumerate(rule_ids):
            source.rules.append({'name':'RULE-'+str(index), 'definition_id':identity, 'status':'Active',
                'portable_definition_id': str(__import__('uuid').UUID(identity)),
                'schema_snapshot':'snapshot-A', 'schema_fingerprint':source.snapshot.schema_fingerprint,
                'operation':'HeroCustomers', 'mapping_revision':1, 'personal_graphql_mapping':'DOC-A',
                'source_path':'email' if index == 0 else 'address.line_2', 'source_type':'string|null',
                'target_doctype':'Customer' if index == 0 else 'Address',
                'target_field':'custom_hero_email' if index == 0 else 'address_line2',
                'purpose':'Field' if index == 0 else 'Native Binding', 'transformation':'Trim',
                'relation':'' if index == 0 else 'Customer Primary Address', 'priority':5,
                'schema_manifest':json.dumps(manifest) if index == 0 else '',
                'schema_dependencies':'{"ids":["field:email"]}' if index == 0 else '[]'})
        with self.context(source):
            raw = exchange.export_package('source-A', 'snapshot-A', 'QUERY_TO_FRAPPE',
                                          format_version=exchange.PORTABLE_FORMAT_VERSION)
        package = json.loads(raw)
        self.assertEqual(set(package['mappings'][0]['dependencies']), {'field:email', 'field:link', 'relation:customer'})
        self.assertEqual(len(package['mappings'][0]['content']['field_rules']), 2)
        with self.context(target):
            preview = exchange.preview_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            self.assertTrue(preview['can_save'])
            self.assertTrue(all(item['state'] == 'Unchanged' for item in preview['schema']['items']))
            states = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            result = exchange.save_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE', states,
                                          confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            self.assertFalse(result['activation'])
            self.assertEqual(raw, exchange.export_package('source-B', 'snapshot-B', 'QUERY_TO_FRAPPE',
                                                          format_version=exchange.PORTABLE_FORMAT_VERSION))
        existing = Fake('existing.test', 'source-C', 'snapshot-C')
        existing.schema_fields = deepcopy(source.schema_fields)
        existing_content = {**content, 'mapping_definition_ids': ['target-rule-1', 'target-rule-2']}
        existing.active.append({'name':'DOC-C', 'definition_id':'legacy-C', 'portable_definition_id':IDENTITY,
            'source_instance':'source-C', 'schema_snapshot':'snapshot-C', 'schema_fingerprint':existing.snapshot.schema_fingerprint,
            'direction':'QUERY_TO_FRAPPE', 'operation_kind':'QUERY', 'operation_name':'customers',
            'status':'Active', 'mapping_revision':4, 'mapping_json':json.dumps(existing_content)})
        for index, rule in enumerate(source.rules):
            existing.rules.append({**rule, 'name':'RULE-C-'+str(index), 'definition_id':'target-rule-'+str(index + 1),
                'schema_snapshot':'snapshot-C', 'personal_graphql_mapping':'DOC-C'})
        with self.context(existing):
            preview = exchange.preview_import(raw, 'source-C', 'snapshot-C', 'QUERY_TO_FRAPPE')
            self.assertEqual(set(preview['items'][0]['rule_remapping'].values()), {'target-rule-1', 'target-rule-2'})
            states = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            exchange.save_import(raw, 'source-C', 'snapshot-C', 'QUERY_TO_FRAPPE', states,
                                 confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            reopened = FrappeMappingDraftStorage(existing).service().open_draft('legacy-C', existing.session.user)
            self.assertEqual(reopened.content['field_rule_bases']['target-rule-1']['name'], 'RULE-C-0')
            self.assertEqual(existing.active[0]['mapping_revision'], 4)
            self.assertEqual(raw, exchange.export_package('source-C', 'snapshot-C', 'QUERY_TO_FRAPPE',
                                                          format_version=exchange.PORTABLE_FORMAT_VERSION))
        target.schema_fields.pop(('Customer', 'custom_hero_related'))
        with self.context(target):
            blocked = exchange.preview_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            self.assertFalse(blocked['can_save'])
            self.assertEqual(blocked['items'][0]['state'], 'BLOCKED')

    def test_duplicate_keys_and_unsupported_dependencies_rejected(self):
        with self.assertRaises(exchange.MappingDraftExchangeError):
            exchange._portable_version('{"format_version":"x","format_version":"y"}')
        with self.assertRaises(exchange.MappingDraftExchangeError):
            exchange._portable_content({'mapping_definition_ids': [IDENTITY], 'field_rules': {}}, 'QUERY_TO_FRAPPE')

    def test_dependency_graph_handles_cycles_and_rejects_missing_nodes(self):
        schema = {'custom_doctypes': [], 'custom_fields': [{'id':'field:link', 'doctype':'Customer',
            'fieldname':'custom_hero_related', 'label':'Related', 'fieldtype':'Link', 'options':'Customer', 'insert_after':''}],
            'relations':[{'id':'relation:link', 'doctype':'Customer', 'field_id':'field:link',
                'fieldname':'custom_hero_related', 'relation_type':'Link', 'target_doctype':'Customer',
                'dependencies':['relation:link']}]}
        package = {'format_version':exchange.PORTABLE_FORMAT_VERSION, 'schema_fingerprint':analyze_snapshot(SCHEMA).fingerprint,
            'direction':'QUERY_TO_FRAPPE', 'schema':schema, 'mappings':[{'definition_id':IDENTITY,
            'operation_kind':'QUERY', 'operation_name':'customers', 'direction':'QUERY_TO_FRAPPE',
            'content':{'records_path':'customers', 'identity_path':'id', 'mapping_definition_ids':[], 'field_rules':{}},
            'dependencies':['relation:link']}]}
        self.assertEqual(exchange._parse_portable(json.dumps(package), 'QUERY_TO_FRAPPE')['schema'], schema)
        package['schema']['relations'][0]['dependencies'].append('missing')
        with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'dependency is missing'):
            exchange._parse_portable(json.dumps(package), 'QUERY_TO_FRAPPE')

    def test_second_draft_failure_rolls_back_first_draft_and_revision(self):
        source, target = Fake('source.test', 'source-A', 'snapshot-A'), Fake('target.test', 'source-B', 'snapshot-B')
        identities = [IDENTITY, 'bd850de8-a445-4d13-9aa6-8dd086047d5a']
        content = {'records_path': 'customers', 'identity_path': 'id', 'mapping_definition_ids': [], 'field_rules': {}}
        with self.context(source):
            for identity in identities:
                definition = {'site_scope': source.local.site, 'definition_id': identity, 'source_instance': 'source-A',
                    'snapshot_name': 'snapshot-A', 'schema_fingerprint': source.snapshot.schema_fingerprint,
                    'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers'}
                FrappeMappingDraftStorage(source).service().open_new_draft(definition, source.session.user, content)
            raw = exchange.export_package('source-A', 'snapshot-A', 'QUERY_TO_FRAPPE',
                                          format_version=exchange.PORTABLE_FORMAT_VERSION)
        with self.context(target):
            preview = exchange.preview_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            states = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            original = FrappeMappingDraftStorage.save_draft
            def fail_second(storage, value):
                if value.definition_id == sorted(identities)[1]: raise ValueError('synthetic persistence failure')
                return original(storage, value)
            with patch.object(FrappeMappingDraftStorage, 'save_draft', fail_second):
                with self.assertRaisesRegex(ValueError, 'synthetic persistence failure'):
                    exchange.save_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE', states,
                                         confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            self.assertEqual(target.drafts, {})
            self.assertEqual(target.revisions, {})

    def test_parentless_target_metadata_change_invalidates_preview(self):
        source, target = Fake('source.test', 'source-A', 'snapshot-A'), Fake('target.test', 'source-B', 'snapshot-B')
        content = {'source': {'doctype': 'Customer', 'field': 'customer_name'},
                   'target': {'operation': 'updateCustomer', 'argument': 'input', 'input_type': 'CustomerInput',
                              'field': 'name', 'graphql_type': 'String'}, 'transformation': 'Identity'}
        definition = {'site_scope': source.local.site, 'definition_id': IDENTITY, 'source_instance': 'source-A',
            'snapshot_name': 'snapshot-A', 'schema_fingerprint': source.snapshot.schema_fingerprint,
            'direction': 'FRAPPE_TO_MUTATION', 'operation_kind': 'MUTATION', 'operation_name': 'updateCustomer'}
        with self.context(source):
            FrappeMappingDraftStorage(source).service().open_new_draft(definition, source.session.user, content)
            raw = exchange.export_package('source-A', 'snapshot-A', 'FRAPPE_TO_MUTATION',
                                          format_version=exchange.PORTABLE_FORMAT_VERSION)
        with self.context(target):
            preview = exchange.preview_import(raw, 'source-B', 'snapshot-B', 'FRAPPE_TO_MUTATION')
            states = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            target.customer_name_type = 'Int'
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'preview changed'):
                exchange.save_import(raw, 'source-B', 'snapshot-B', 'FRAPPE_TO_MUTATION', states,
                                     confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            self.assertEqual(target.drafts, {})

    def test_parentless_list_read_and_revision_checked_api_save(self):
        fake = Fake('target.test', 'source-B', 'snapshot-B')
        definition = {'site_scope': fake.local.site, 'definition_id': IDENTITY, 'source_instance': 'source-B',
            'snapshot_name': 'snapshot-B', 'schema_fingerprint': fake.snapshot.schema_fingerprint,
            'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers'}
        content = {'records_path': 'customers', 'identity_path': 'id', 'mapping_definition_ids': [], 'field_rules': {}}
        with self.context(fake), patch.object(api, '_mapping_request_fields'), \
             patch('hero_integration.services.frappe_mapping_draft_storage.FrappeMappingDraftStorage',
                   side_effect=lambda: FrappeMappingDraftStorage(fake)):
            service = FrappeMappingDraftStorage(fake).service()
            service.open_new_draft(definition, fake.session.user, content)
            result = api.mapping_draft_list('QUERY_TO_FRAPPE')
            self.assertEqual([item['definition_id'] for item in result['items']], [IDENTITY])
            updated = {**content, 'identity_path': 'email'}
            saved = api.mapping_draft_save(IDENTITY, 1, 1, 0, json.dumps(updated))
            self.assertEqual(saved['draft']['content'], updated)
            self.assertEqual(service.open_draft(IDENTITY, fake.session.user).content, updated)
            plan = api.mapping_draft_materialize_plan(IDENTITY, 1, 2, 0, 0)
            self.assertEqual(plan['execution_authority'], 'NONE')
            self.assertEqual(plan['selected_paths'], ['email'])
            fake.session.user = 'other@example.invalid'
            self.assertEqual(api.mapping_draft_list('QUERY_TO_FRAPPE')['items'], [])

    def test_invalid_package_cannot_change_target_and_foreign_owner_is_denied(self):
        source, target = Fake('source.test', 'source-A', 'snapshot-A'), Fake('target.test', 'source-B', 'snapshot-B')
        definition = {'site_scope': source.local.site, 'definition_id': IDENTITY, 'source_instance': 'source-A',
            'snapshot_name': 'snapshot-A', 'schema_fingerprint': source.snapshot.schema_fingerprint,
            'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers'}
        content = {'records_path': 'customers', 'identity_path': 'id', 'mapping_definition_ids': [], 'field_rules': {}}
        with self.context(source):
            FrappeMappingDraftStorage(source).service().open_new_draft(definition, source.session.user, content)
            raw = exchange.export_package('source-A', 'snapshot-A', 'QUERY_TO_FRAPPE', format_version=exchange.PORTABLE_FORMAT_VERSION)
        with self.context(target):
            package = json.loads(raw)
            package['mappings'][0]['content']['identity_path'] = 'missing'
            with self.assertRaises(exchange.MappingDraftExchangeError):
                exchange.preview_import(json.dumps(package), 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            self.assertEqual(target.drafts, {})
            package = json.loads(raw)
            package['mappings'][0]['direction'] = 'FRAPPE_TO_MUTATION'
            with self.assertRaises(exchange.MappingDraftExchangeError):
                exchange.preview_import(json.dumps(package), 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            package = json.loads(raw)
            bad = deepcopy(package['mappings'][0])
            bad['definition_id'] = '0e15170c-6f28-4b89-aa37-6bf14f8e0160'
            bad['content']['identity_path'] = 'missing'
            package['mappings'].append(bad)
            with self.assertRaises(exchange.MappingDraftExchangeError):
                exchange.preview_import(json.dumps(package), 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            self.assertEqual(target.drafts, {})
            package = json.loads(raw)
            package['schema_fingerprint'] = '0' * 64
            with self.assertRaises(exchange.MappingDraftExchangeError):
                exchange.preview_import(json.dumps(package), 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            target.get_roles = lambda: []
            with self.assertRaises(PermissionError):
                exchange.preview_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            target.get_roles = lambda: ['System Manager']
            preview = exchange.preview_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            states = [dict(definition_id=i['definition_id'], **i['expected']) for i in preview['items']]
            exchange.save_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE', states,
                                 confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            target.session.user = 'other@example.invalid'
            with self.assertRaises(ValueError):
                exchange.preview_import(raw, 'source-B', 'snapshot-B', 'QUERY_TO_FRAPPE')
            self.assertEqual(len(target.drafts), 1)

    def test_explicit_identity_enrollment_is_repeatable_and_checks_revision_and_collisions(self):
        from contextlib import contextmanager
        from hero_integration.services.mapping_draft_service import DraftConflict
        class Parent:
            name, source_instance, schema_snapshot, direction, mapping_revision = 'LOCAL-1', 'source', 'snapshot', 'QUERY_TO_FRAPPE', 3
            portable = ''
            def get(self, key): return self.portable if key == 'portable_definition_id' else None
            def check_permission(self, permission): self.permission = permission
        parent = Parent()
        collision = []
        def sql(query, params, **kwargs):
            if query.startswith('select source_instance'):
                return [dict(source_instance='source', schema_snapshot='snapshot', direction='QUERY_TO_FRAPPE')]
            if query.startswith('select name, definition_id'):
                return collision
            if query.startswith('select definition_id from `tabHERO Mapping Draft`'):
                return []
            if query.startswith('update `tabHERO GraphQL Mapping`'):
                parent.portable = params[0]
                return []
            if query.startswith('select portable_definition_id'):
                return [dict(portable_definition_id=parent.portable, mapping_revision=parent.mapping_revision)]
            raise AssertionError(query)
        class Storage:
            _definition = parent
            def _authorize(self, actor):
                if actor != 'owner': raise PermissionError
            @contextmanager
            def transaction(self, identity, actor): yield
        fake = SimpleNamespace(local=SimpleNamespace(site='test'), session=SimpleNamespace(user='owner'),
                               db=SimpleNamespace(sql=sql))
        with patch.object(exchange, 'frappe', fake), patch.object(exchange, 'FrappeMappingDraftStorage', Storage), \
             patch.object(exchange, 'assert_portable_identity_schema'), patch.object(exchange, 'lock_definition_drafts', return_value=[]), \
             patch.object(exchange, 'lock_identity_drafts', return_value=[]), \
             patch.object(exchange, '_lock_context'), \
             patch.object(hero_frappe_mapping, '_lock_active_target'):
            with self.assertRaises(DraftConflict):
                exchange.enroll_portable_identity('legacy', IDENTITY, 2)
            collision.append(dict(name='other', definition_id='other'))
            with self.assertRaises(DraftConflict):
                exchange.enroll_portable_identity('legacy', IDENTITY, 3)
            self.assertEqual(parent.portable, '')
            collision.clear()
            self.assertFalse(exchange.enroll_portable_identity('legacy', IDENTITY, 3)['unchanged'])
            self.assertTrue(exchange.enroll_portable_identity('legacy', IDENTITY, 3)['unchanged'])
            with self.assertRaises(DraftConflict):
                exchange.enroll_portable_identity('legacy', 'bd850de8-a445-4d13-9aa6-8dd086047d5a', 3)

    def test_explicit_rule_identity_enrollment_preserves_local_identity_and_cas(self):
        from contextlib import contextmanager
        from hero_integration.services.mapping_draft_service import DraftConflict
        class Parent:
            source_instance, schema_snapshot, direction, mapping_revision = 'source', 'snapshot', 'QUERY_TO_FRAPPE', 3
            mapping_json = '{"mapping_definition_ids":["rule-local"]}'
            def check_permission(self, _permission): pass
        class Rule:
            portable = ''
            def get(self, key): return self.portable if key == 'portable_definition_id' else None
            def check_permission(self, _permission): pass
        parent, rule, collision, foreign = Parent(), Rule(), [], []
        def sql(query, params, **kwargs):
            if query.startswith('select source_instance'):
                return [dict(source_instance='source', schema_snapshot='snapshot', direction='QUERY_TO_FRAPPE')]
            if query.startswith('select name from `tabHERO Frappe Mapping`'):
                return collision
            if query.startswith('select content_json'):
                return foreign
            if query.startswith('update `tabHERO Frappe Mapping`'):
                rule.portable = params[0]; return []
            if query.startswith('select portable_definition_id'):
                return [dict(portable_definition_id=rule.portable, mapping_revision=2)]
            raise AssertionError(query)
        class Storage:
            _definition = parent
            def draft(self, key): return None
            def _authorize(self, actor):
                if actor != 'owner': raise PermissionError
            @contextmanager
            def transaction(self, identity, actor): yield
        fake = SimpleNamespace(local=SimpleNamespace(site='test'), session=SimpleNamespace(user='owner'),
            db=SimpleNamespace(sql=sql), get_doc=lambda *args, **kwargs: rule)
        base = {'name':'RULE-LOCAL', 'revision':2}
        with patch.object(exchange, 'frappe', fake), patch.object(exchange, 'FrappeMappingDraftStorage', Storage), \
             patch.object(exchange, 'assert_portable_identity_schema'), \
             patch.object(exchange, '_lock_context'), patch.object(exchange, '_current_rule_snapshot', return_value=({}, base)), \
             patch.object(exchange, 'lock_identity_drafts', side_effect=lambda *_: [Row(item) for item in foreign]), \
             patch.object(hero_frappe_mapping, '_lock_active_target'):
            with self.assertRaises(DraftConflict):
                exchange.enroll_portable_rule_identity('mapping-local', 'rule-local', IDENTITY, 3, 1)
            collision.append({'name':'another-rule'})
            with self.assertRaises(DraftConflict):
                exchange.enroll_portable_rule_identity('mapping-local', 'rule-local', IDENTITY, 3, 2)
            collision.clear()
            foreign.append({'content_json': json.dumps({'mapping_definition_ids': [IDENTITY], 'field_rules': {IDENTITY: {}}})})
            with self.assertRaises(DraftConflict):
                exchange.enroll_portable_rule_identity('mapping-local', 'rule-local', IDENTITY, 3, 2)
            foreign.clear()
            result = exchange.enroll_portable_rule_identity('mapping-local', 'rule-local', IDENTITY, 3, 2)
            self.assertEqual(result['definition_id'], 'rule-local')
            self.assertFalse(result['unchanged'])
            self.assertTrue(exchange.enroll_portable_rule_identity('mapping-local', 'rule-local', IDENTITY, 3, 2)['unchanged'])
