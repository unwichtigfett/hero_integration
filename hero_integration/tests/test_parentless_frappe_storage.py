from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

class TestParentlessFrappeStorage(unittest.TestCase):
    """Exercises the real storage service with only the database boundary replaced."""

    def test_parentless_create_save_reopen_and_foreign_owner(self):
        from hero_integration import api
        from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
        from hero_integration.services.mapping_draft_service import DraftConflict
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping
        from hero_integration.services.graphql_graph import analyze_snapshot

        class Row(dict):
            __getattr__ = dict.__getitem__
        schema = {'__schema': {'queryType': {'name': 'Query'}, 'types': [
            {'kind': 'OBJECT', 'name': 'Query', 'fields': [{'name': 'customers', 'args': [],
                'type': {'kind': 'LIST', 'ofType': {'kind': 'OBJECT', 'name': 'Customer'}}}]},
            {'kind': 'OBJECT', 'name': 'Customer', 'fields': [
                {'name': 'id', 'args': [], 'type': {'kind': 'SCALAR', 'name': 'String'}},
                {'name': 'external_id', 'args': [], 'type': {'kind': 'SCALAR', 'name': 'String'}}]},
            {'kind': 'SCALAR', 'name': 'String'}]}}
        fingerprint = analyze_snapshot(schema).fingerprint
        snapshot = SimpleNamespace(schema_fingerprint=fingerprint, source_instance='source', check_permission=lambda *_: None)
        class Fake:
            def __init__(self):
                self.local = SimpleNamespace(site='test.local')
                self.session = SimpleNamespace(user='alice@example.invalid')
                self.drafts, self.revisions = {}, {}
                self.db = SimpleNamespace(sql=self.sql, savepoint=lambda _: None, rollback=lambda **_: None,
                                          has_column=lambda *args: True)
            def get_roles(self): return ['System Manager']
            def sql(self, query, params=(), **kwargs):
                if query.startswith('show index'):
                    return [Row(Column_name='definition_id', Seq_in_index=1, Sub_part=None),
                            Row(Column_name='portable_definition_id', Seq_in_index=1, Sub_part=None, Non_unique=0,
                                Key_name='portable_definition_id')]
                if 'tabHERO GraphQL Mapping' in query: return []
                if 'select owner_user from `tabHERO Mapping Draft`' in query:
                    return [Row(owner_user=row['owner_user']) for row in self.drafts.values() if row['definition_id'] == params[0]]
                if 'tabHERO Mapping Draft' in query:
                    row = self.drafts.get(params[0]); return [Row(row)] if row else []
                if 'tabHERO Mapping Revision' in query:
                    return [Row(name='revision')] if tuple(params) in self.revisions else []
                raise AssertionError(query)
            def new_doc(self, doctype): return Document(self, {'doctype': doctype})
            def get_doc(self, *args, **kwargs):
                if isinstance(args[0], dict): return Document(self, args[0])
                raise AssertionError(args)
        class Document:
            def __init__(self, owner, values): self.owner, self.values = owner, dict(values)
            def update(self, values): self.values.update(values)
            def save(self): self.owner.drafts[self.values['draft_key']] = dict(self.values)
            def insert(self, **kwargs):
                key = tuple(self.values[k] for k in ('draft_key', 'generation', 'revision', 'kind'))
                self.owner.revisions[key] = dict(self.values)
        fake = Fake()
        definition = {'site_scope': 'test.local', 'definition_id': 'a16b15fa-7ca8-4722-b63c-ac60cc46629a',
                      'source_instance': 'source', 'snapshot_name': 'snapshot', 'schema_fingerprint': fingerprint,
                      'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers'}
        service = FrappeMappingDraftStorage(fake).service()
        content = {'records_path': 'customers', 'identity_path': 'id', 'mapping_definition_ids': [], 'field_rules': {}}
        with patch.object(hero_frappe_mapping, '_lock_active_target'), \
             patch.object(api, '_validated_snapshot', return_value=(snapshot, schema)), \
             patch('hero_integration.services.frappe_mapping_draft_storage._fresh_identity_candidates', return_value=[]), \
             patch('hero_integration.services.frappe_mapping_draft_storage._fresh_definition_candidates',
                   side_effect=lambda f, identity: [Row(draft_key=key) for key, row in f.drafts.items()
                                                    if row['definition_id'] == identity]):
            draft = service.open_new_draft(definition, fake.session.user, content)
            self.assertEqual(draft.base_active_revision, 0)
            saved = service.save(draft.definition_id, fake.session.user, 1, 0, {**content, 'identity_path': 'external_id'},
                                 expected_generation=1)
            self.assertEqual(service.open_draft(draft.definition_id, fake.session.user).content, saved.draft.content)
            self.assertEqual(len(fake.revisions), 2)
            fake.session.user = 'bob@example.invalid'
            with self.assertRaises(DraftConflict):
                service.open_new_draft(definition, fake.session.user, content)
            self.assertEqual(len(fake.drafts), 1)

    def test_controller_requires_exact_new_or_active_baseline(self):
        import json
        from hero_integration.hero_integration.doctype.hero_mapping_draft import hero_mapping_draft as controller
        from hero_integration.services import frappe_mapping_draft_storage as storage
        context = {'site_scope': 'test.local', 'definition_id': 'a16b15fa-7ca8-4722-b63c-ac60cc46629a',
                   'source_instance': 'source', 'snapshot_name': 'snapshot', 'schema_fingerprint': 'a' * 64,
                   'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers'}
        class Doc:
            site_scope = 'test.local'
            definition_id = context['definition_id']
            owner_user = 'alice@example.invalid'
            base_schema_fingerprint = 'a' * 64
            base_active_mapping = ''
            base_active_revision = 0
            base_active_hash = ''
            new_definition_json = json.dumps(context)
            def is_new(self): return True
        def fail(message, *args): raise ValueError(message)
        fake = SimpleNamespace(session=SimpleNamespace(user='alice@example.invalid'), throw=fail, PermissionError=PermissionError)
        with patch.object(controller, 'frappe', fake), patch.object(storage, 'assert_storage_write'):
            doc = Doc()
            controller.HEROMappingDraft.validate(doc)
            doc.base_active_mapping = 'forged-active'
            with self.assertRaisesRegex(ValueError, 'cannot claim an active baseline'):
                controller.HEROMappingDraft.validate(doc)
            doc.base_active_mapping = ''
            doc.new_definition_json = ''
            with self.assertRaisesRegex(ValueError, 'requires its active baseline'):
                controller.HEROMappingDraft.validate(doc)

    def test_published_parentless_draft_reopens_only_as_new_active_generation(self):
        from hero_integration.hero_integration.doctype.hero_mapping_draft import hero_mapping_draft as controller
        from hero_integration.services import frappe_mapping_draft_storage as storage
        class Row:
            def __init__(self, **values): self.__dict__.update(values)
            def get(self, key): return getattr(self, key, None)
        identity = 'a16b15fa-7ca8-4722-b63c-ac60cc46629a'
        previous = Row(draft_key='key', site_scope='site', definition_id=identity, owner_user='owner',
                       state='DISCARDED', generation=1, content_revision=1,
                       new_definition_json='{"old":"context"}', base_content_json='{}')
        current = Row(draft_key='key', site_scope='site', definition_id=identity, owner_user='owner',
                      state='OPEN', generation=2, content_revision=0, reconciliation_revision=0,
                      reconciliation_json='', new_definition_json='', base_content_json='{"active":true}',
                      base_active_mapping='active', base_active_revision=1, base_active_hash='hash')
        current.is_new = lambda: False
        current.get_doc_before_save = lambda: previous
        def fail(message, *_): raise ValueError(message)
        fake = SimpleNamespace(session=SimpleNamespace(user='owner'), throw=fail, PermissionError=PermissionError)
        with patch.object(controller, 'frappe', fake), patch.object(storage, 'assert_storage_write'):
            controller.HEROMappingDraft.validate(current)
            current.generation = 1
            with self.assertRaisesRegex(ValueError, 'new generation'):
                controller.HEROMappingDraft.validate(current)
