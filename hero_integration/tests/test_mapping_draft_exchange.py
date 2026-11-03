from __future__ import annotations

from contextlib import contextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import mapping_draft_exchange as exchange


class TestExchangeInputSize(unittest.TestCase):
    def test_multibyte_payload_is_limited_by_encoded_size_before_parsing(self):
        payload = 'é' * 500_001
        self.assertLess(len(payload), 1_000_000)
        for parse in (lambda: exchange._portable_version(payload),
                      lambda: exchange._parse(payload, 'source', 'snapshot', 'QUERY_TO_FRAPPE')):
            with self.subTest(parser=parse), self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'size'):
                parse()


class TestPreviewBinding(unittest.TestCase):
    def test_preview_binding_covers_site_actor_package_and_preview(self):
        fake = SimpleNamespace(local=SimpleNamespace(site='site-a', conf={'encryption_key': 'synthetic-key'}),
                               session=SimpleNamespace(user='owner-a'))
        package, preview = {'mapping': 'one'}, {'can_save': True}
        with patch.object(exchange, 'frappe', fake):
            original = exchange._preview_binding(package, preview)
            self.assertEqual(original, exchange._preview_binding(package, preview))
            self.assertNotEqual(original, exchange._preview_binding({'mapping': 'two'}, preview))
            self.assertNotEqual(original, exchange._preview_binding(package, {'can_save': False}))
            fake.local.site = 'site-b'
            self.assertNotEqual(original, exchange._preview_binding(package, preview))
            fake.local.site = 'site-a'
            fake.session.user = 'owner-b'
            self.assertNotEqual(original, exchange._preview_binding(package, preview))
            fake.local.conf['encryption_key'] = ''
            # A real site creates its key on first use; only an unavailable key must fail.
            with patch('frappe.utils.password.get_encryption_key', side_effect=RuntimeError('no key')), \
                    self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'encryption key'):
                exchange._preview_binding(package, preview)


class _Frappe:
    def __init__(self):
        self.local = SimpleNamespace(site="draft-exchange.test", conf={'encryption_key': 'test-only-key'})
        self.session = SimpleNamespace(user="owner@example.invalid")
        self.db = SimpleNamespace(savepoint=lambda _name: None, rollback=lambda **_kwargs: None)
        self.active = [
            self._active("query-one", "QUERY", "readThings", "QUERY_TO_FRAPPE"),
            self._active("mutation-one", "MUTATION", "writeThings", "FRAPPE_TO_MUTATION"),
        ]
        self.drafts = [self._draft("query-one", "owner@example.invalid"), self._draft("mutation-one", "other@example.invalid")]

    @staticmethod
    def _active(definition_id, operation_kind, operation_name, direction):
        content = {"source": {"field": definition_id}, "value": 9007199254740993}
        return {
            "name": "MAP-" + definition_id, "definition_id": definition_id, "schema_fingerprint": "a" * 64,
            "source_instance": "source-1", "schema_snapshot": "SNAPSHOT", "status": "Active",
            "operation_kind": operation_kind, "operation_name": operation_name, "direction": direction,
            "mapping_revision": 3, "mapping_json": json.dumps(content, sort_keys=True),
        }

    def _draft(self, definition_id, owner):
        row = next(item for item in self.active if item["definition_id"] == definition_id)
        content = {"source": {"field": definition_id}, "value": 9007199254740993}
        return {
            "definition_id": definition_id, "owner_user": owner, "site_scope": self.local.site, "state": "OPEN",
            "content_json": json.dumps(content, sort_keys=True), "content_hash": exchange._hash(content),
            "generation": 1, "content_revision": 2, "base_active_mapping": row["name"],
            "base_active_revision": row["mapping_revision"], "base_active_hash": exchange._hash(content),
            "base_schema_fingerprint": row["schema_fingerprint"], "reconciliation_revision": 0,
        }

    def get_all(self, doctype, filters=None, fields=None, **_kwargs):
        filters = filters or {}
        if doctype == "HERO GraphQL Mapping":
            return [dict(row) for row in self.active if all(row.get(key) == value for key, value in filters.items())]
        if doctype == "HERO Mapping Draft":
            ids = set(filters["definition_id"][1])
            return [dict(row) for row in self.drafts if row["definition_id"] in ids and row["owner_user"] == filters["owner_user"]
                    and row["site_scope"] == filters["site_scope"] and row["state"] == filters["state"]]
        if doctype == "HERO Frappe Mapping":
            return []
        raise AssertionError(doctype)


def _schema_preview(_schema):
    return {"items": [], "counts": {state: 0 for state in ("New", "Unchanged", "Changed", "Conflict", "Invalid")}}


class TestCurrentExchangeLocks(unittest.TestCase):
    def test_native_task_publication_accepts_own_validated_generated_service_without_approving_it(self):
        from hashlib import sha256
        from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
        from hero_integration.services.mapping_draft_service import MappingDraftError
        query = 'query tasks { tasks { id title is_deleted } }'
        row = {'name': 'SERVICE'}
        service = SimpleNamespace(modified_by='owner', operation_name='tasks', schema_fingerprint='fp',
                                  query_document=query, execution_status='Pending Approval',
                                  check_permission=lambda value: None)
        fake = SimpleNamespace(session=SimpleNamespace(user='owner'),
            db=SimpleNamespace(sql=lambda query, *args, **kwargs: [row] if 'schema_snapshot=' in query else []),
            get_doc=lambda value, *args, **kwargs: service if isinstance(value, dict) else SimpleNamespace())
        storage = FrappeMappingDraftStorage(fake)
        storage._definition = SimpleNamespace(schema_snapshot='snapshot', schema_fingerprint='fp', name='MAP')
        content = {'native_task_onboarding': {'confirmed': True},
                   'generated_task_seal': {'document_sha256': sha256(query.encode()).hexdigest()}}
        storage._validate_publication_read_contract(content)
        self.assertEqual(service.execution_status, 'Pending Approval')
        service.modified_by = 'other'
        with self.assertRaisesRegex(MappingDraftError, 'matching validated read service'):
            storage._validate_publication_read_contract(content)

    def test_saved_personal_task_compiles_selected_rules_after_all_existing_guards(self):
        from hero_integration import api
        from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
        from hero_integration.services.mapping_draft_service import ActiveMapping, MappingDraft, draft_key, _hash
        from hero_integration.services.graphql_graph import analyze_snapshot
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping

        scalar = lambda name: {'kind': 'SCALAR', 'name': name}
        raw = {'__schema': {'queryType': {'name': 'Query'}, 'types': [
            {'kind': 'OBJECT', 'name': 'Query', 'fields': [{'name': 'tasks', 'args': [
                {'name': 'first', 'type': scalar('Int')}, {'name': 'offset', 'type': scalar('Int')},
                {'name': 'orderBy', 'type': scalar('String')}],
                'type': {'kind': 'LIST', 'ofType': {'kind': 'OBJECT', 'name': 'Task'}}}]},
            {'kind': 'OBJECT', 'name': 'Task', 'fields': [
                {'name': 'id', 'args': [], 'type': scalar('Int')},
                {'name': 'title', 'args': [], 'type': scalar('String')},
                {'name': 'is_deleted', 'args': [], 'type': scalar('Boolean')}]},
            *({'kind': 'SCALAR', 'name': name} for name in ('Int', 'String', 'Boolean'))]}}
        fingerprint = analyze_snapshot(raw).fingerprint
        content = {'target_operation': 'HeroTasks', 'records_path': 'tasks', 'identity_path': 'id',
            'variables': {'first': '$page_size', 'offset': '$offset'}, 'page_size': 10,
            'native_task_onboarding': {'confirmed': True, 'source_instance': 'source', 'source_type': 'Task'},
            'mapping_definition_ids': ['id-rule', 'title-rule'], 'field_rules': {
                'id-rule': {'source_path': 'id', 'source_type': 'int', 'target_doctype': 'HERO Integration Link',
                            'target_field': 'source_id', 'purpose': 'Source Identity', 'transformation': 'Positive Int To Text', 'relation': '', 'priority': 0},
                'title-rule': {'source_path': 'title', 'source_type': 'string|null', 'target_doctype': 'Task',
                               'target_field': 'subject', 'purpose': 'Record Name', 'transformation': 'Trim', 'relation': '', 'priority': 0}}}
        active = ActiveMapping('site', 'definition', 'mapping', 2, fingerprint, {'base': 'value'})
        key = draft_key('site', 'definition', 'owner')
        draft = MappingDraft(key, 'site', 'definition', 'owner', 1, 'OPEN', content, _hash(content), 3,
                             active.name, 2, active.content_hash, fingerprint, active.content)
        snapshot = SimpleNamespace(name='snapshot', schema_fingerprint=fingerprint, source_instance='source', analysis_status='Validated')
        fake = SimpleNamespace(local=SimpleNamespace(site='site'),
            db=SimpleNamespace(sql=lambda *args, **kwargs: [SimpleNamespace(content_hash=draft.content_hash)]),
            get_doc=lambda *args, **kwargs: SimpleNamespace(check_permission=lambda permission: None))
        storage = FrappeMappingDraftStorage(fake)
        @contextmanager
        def transaction(identity, actor):
            storage._definition = SimpleNamespace(name='mapping', schema_snapshot='snapshot', source_instance='source',
                direction='QUERY_TO_FRAPPE', operation_kind='QUERY', operation_name='tasks')
            try:
                yield
            finally:
                storage._definition = None
        storage.transaction = transaction
        storage.active = lambda identity: active
        storage.draft = lambda identity: draft
        storage.canonicalize = lambda value: value
        storage._assert_locked_rule_baseline = lambda value: None
        metadata = SimpleNamespace(ensure_write=lambda doctype: None,
            field=lambda doctype, field: SimpleNamespace(fieldtype='Data', read_only=0))
        with patch.object(api, '_validated_snapshot', return_value=(snapshot, raw)), \
             patch.object(hero_frappe_mapping, '_FrappeMetadata', return_value=metadata):
            request = storage.materialize_native_task('definition', 'owner', 1, 3, 2, 0, snapshot_name='snapshot')
        self.assertEqual({item.selection_path for item in request.result_bindings}, {('id',), ('title',), ('is_deleted',)})
        self.assertIsNone(request.mutation_safety_request)

    def test_personal_task_materialization_rechecks_draft_revision_and_source_before_generation(self):
        from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
        from hero_integration.services.mapping_draft_service import ActiveMapping, MappingDraft, DraftConflict, _hash, draft_key

        active = ActiveMapping('site', 'definition', 'mapping', 2, 'a' * 64, {'base': 'value'})
        key = draft_key('site', 'definition', 'owner')
        content = {'some': 'content'}
        draft = MappingDraft(key, 'site', 'definition', 'owner', 1, 'OPEN', content, _hash(content), 3,
                             active.name, active.revision, active.content_hash, active.schema_fingerprint, active.content)
        fake = SimpleNamespace(local=SimpleNamespace(site='site'),
            get_doc=lambda *args, **kwargs: SimpleNamespace(check_permission=lambda permission: None))
        storage = FrappeMappingDraftStorage(fake)
        @contextmanager
        def transaction(identity, actor):
            storage._definition = SimpleNamespace(name='mapping', schema_snapshot='snapshot',
                direction='QUERY_TO_FRAPPE', operation_kind='QUERY', operation_name='tasks')
            try:
                yield
            finally:
                storage._definition = None
        storage.transaction = transaction
        storage.active = lambda identity: active
        storage.draft = lambda identity: draft
        with self.assertRaises(DraftConflict):
            storage.materialize_native_task('definition', 'owner', 1, 2, 2, 0, snapshot_name='snapshot')
        with self.assertRaisesRegex(ValueError, 'explicit Task'):
            storage.materialize_native_task('definition', 'owner', 1, 3, 2, 0, snapshot_name='other-snapshot')

    def test_single_publish_rejects_unauthorized_actor_before_context_lookup(self):
        from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage

        db = SimpleNamespace(sql=lambda *args, **kwargs: self.fail('unauthorized lookup'))
        fake = SimpleNamespace(db=db, session=SimpleNamespace(user='other'))
        with self.assertRaises(PermissionError):
            FrappeMappingDraftStorage(fake).publish('definition', 'owner', 1, 1, 1, 0, confirmed=True)

    def test_single_publish_locks_context_before_parent_and_rechecks_scope(self):
        from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping
        from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
        from hero_integration.services.mapping_draft_service import DraftConflict

        events = []
        row = SimpleNamespace(name='mapping', operation_name='customers', source_instance='source', schema_snapshot='snapshot', direction='QUERY_TO_FRAPPE')
        def sql(query, *args, **kwargs):
            if query.startswith('select content_json'):
                events.append('draft-read')
                return [SimpleNamespace(content_json='{}')]
            events.append('lookup')
            return [row]
        fake = SimpleNamespace(local=SimpleNamespace(site='site'), db=SimpleNamespace(sql=sql))
        storage = FrappeMappingDraftStorage(fake)
        storage._authorize = lambda actor: events.append('authorize')
        storage.lock_draft_identities = lambda *args, **kwargs: None
        @contextmanager
        def transaction(identity, actor):
            events.append('parent')
            storage._definition = SimpleNamespace(name='mapping', source_instance='changed',
                                                  schema_snapshot='snapshot', direction='QUERY_TO_FRAPPE')
            try:
                yield
            finally:
                storage._definition = None
        storage.transaction = transaction
        with patch.object(hero_graphql_mapping, 'lock_exchange_context',
                          side_effect=lambda *scope: events.append(('context', scope))):
            with self.assertRaisesRegex(DraftConflict, 'context changed'):
                storage.publish('definition', 'owner', 1, 1, 1, 0, confirmed=True)
        self.assertEqual(events, ['authorize', 'lookup', ('context', ('source', 'snapshot', 'QUERY_TO_FRAPPE')), 'draft-read', 'parent'])

    def test_parent_reads_are_ordered_and_reject_changed_scope(self):
        calls = []
        def sql(query, args, as_dict=False):
            calls.append((query, args[0]))
            return [{'definition_id': args[0], 'status': 'Active', 'source_instance': 'src',
                     'schema_snapshot': 'snap', 'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY'}]
        fake = SimpleNamespace(db=SimpleNamespace(sql=sql))
        with patch.object(exchange, 'frappe', fake):
            rows = exchange._lock_active_definitions({'z': {}, 'a': {}}, 'src', 'snap', 'QUERY_TO_FRAPPE')
            self.assertEqual(list(rows), ['a', 'z'])
            self.assertTrue(all('for update' in query for query, _ in calls))
            self.assertEqual([identity for _, identity in calls], ['a', 'z'])
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'context changed'):
                exchange._lock_active_definitions({'a': {}}, 'other', 'snap', 'QUERY_TO_FRAPPE')

    def test_owner_draft_keys_are_read_in_definition_order(self):
        calls = []
        def sql(query, args, as_dict=False):
            calls.append((query, args[0]))
            return []
        fake = SimpleNamespace(db=SimpleNamespace(sql=sql), local=SimpleNamespace(site='site'),
                               session=SimpleNamespace(user='owner'))
        with patch.object(exchange, 'frappe', fake):
            self.assertEqual(exchange._lock_open_drafts({'z', 'a'}), {})
        self.assertEqual([key for _, key in calls], [exchange.draft_key('site', identity, 'owner')
                                                     for identity in ('a', 'z')])
        self.assertTrue(all('for update' in query for query, _ in calls))


class TestMappingDraftExchange(unittest.TestCase):
    def setUp(self):
        self.frappe = _Frappe()
        self.patch = patch.object(exchange, "frappe", self.frappe)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.schema = patch.object(exchange, "preview_schema", side_effect=_schema_preview)
        self.schema_mock = self.schema.start()
        self.addCleanup(self.schema.stop)
        self.preparation = patch.object(exchange, '_prepare_item', side_effect=lambda item, row, local, schema: {
            'content': item['content'], 'references': [], 'conflicts': [], 'can_save': True})
        self.preparation_mock = self.preparation.start()
        self.addCleanup(self.preparation.stop)
        self.parent_locks = patch.object(exchange, '_lock_active_definitions',
                                         side_effect=lambda active, *_args: active)
        self.parent_locks.start()
        self.addCleanup(self.parent_locks.stop)
        self.draft_locks = patch.object(exchange, '_lock_open_drafts', side_effect=exchange._open_drafts)
        self.draft_locks.start()
        self.addCleanup(self.draft_locks.stop)
        self.context_lock = patch.object(exchange, '_lock_context')
        self.context_lock_mock = self.context_lock.start()
        self.addCleanup(self.context_lock.stop)
        self.identity_lock = patch('hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping._lock_active_target')
        self.identity_lock.start()
        self.addCleanup(self.identity_lock.stop)
        self.fresh_context = patch.object(exchange, '_fresh_active_definitions', side_effect=exchange._active_definitions)
        self.fresh_context.start()
        self.addCleanup(self.fresh_context.stop)

    def package(self):
        return exchange.export_package("source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")

    def test_export_and_preview_are_owner_scoped_directional_and_read_only(self):
        package = json.loads(self.package())
        self.assertEqual(["query-one"], [item["definition_id"] for item in package["mappings"]])
        self.assertEqual("QUERY_TO_FRAPPE", package["direction"])
        preview = exchange.preview_import(self.package(), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")
        self.assertFalse(preview["persisted"])
        self.assertEqual(["UNCHANGED"], [item["state"] for item in preview["items"]])
        self.assertEqual(9007199254740993, package["mappings"][0]["content"]["value"])

    def test_additional_assignment_and_large_number_survive_export_and_preview(self):
        row = self.frappe.drafts[0]
        value = json.loads(row['content_json'])
        value['additional_mappings'] = [{'id': '8a6c58ba-08fb-4da4-87e6-684bb1b16e19',
            'source': {'field': 'nr'}, 'target': {'doctype': 'Customer', 'field': 'alias'},
            'transformation': 'Identity'}]
        row['content_json'] = json.dumps(value)
        row['content_hash'] = exchange._hash(value)
        exported = self.package()
        self.assertEqual(value, json.loads(exported)['mappings'][0]['content'])
        preview = exchange.preview_import(exported, 'source-1', 'SNAPSHOT', 'QUERY_TO_FRAPPE')
        self.assertEqual(['UNCHANGED'], [item['state'] for item in preview['items']])

    def test_only_selected_native_deadline_carries_the_fixed_schema(self):
        rule = {'source_path': 'due_date', 'target_doctype': 'Task', 'target_field': 'custom_hero_due_date',
                'purpose': 'Native Binding', 'transformation': 'Native Adapter'}
        base = {'name': 'RULE', 'revision': 1, 'operation': 'HeroTasks', 'schema_snapshot': 'SNAPSHOT',
                'schema_fingerprint': 'a' * 64}
        row = {'name': 'RULE', 'mapping_revision': 1, 'operation': 'HeroTasks', 'schema_snapshot': 'SNAPSHOT',
               'schema_fingerprint': 'a' * 64, 'status': 'Active', 'personal_graphql_mapping': '',
               'schema_manifest': '', 'schema_dependencies': '[]'}
        item = {'base_active_name': 'MAP', 'base_schema_fingerprint': 'a' * 64, 'operation_name': 'tasks',
                'content': {'target_operation': 'HeroTasks', 'native_task_onboarding': {'confirmed': True},
                            'mapping_definition_ids': ['due'], 'field_rules': {'due': rule}, 'field_rule_bases': {'due': base}}}
        with patch.object(self.frappe, 'get_all', return_value=[row]):
            schema = exchange._export_schema([item], 'QUERY_TO_FRAPPE', 'SNAPSHOT')
            self.assertEqual(schema['custom_fields'][0]['fieldname'], 'custom_hero_due_date')
            self.assertEqual(schema['custom_fields'][0]['read_only'], 1)
            item['content']['mapping_definition_ids'] = []
            self.assertEqual(exchange._export_schema([item], 'QUERY_TO_FRAPPE', 'SNAPSHOT')['custom_fields'], [])
            item['content']['mapping_definition_ids'] = ['due']
            self.assertEqual(exchange._export_schema([item], 'FRAPPE_TO_MUTATION', 'SNAPSHOT')['custom_fields'], [])

    def test_selected_rule_schema_manifest_is_exported_and_missing_dependency_rejected(self):
        field = {'id': 'field:customer', 'doctype': 'Customer', 'fieldname': 'custom_hero_number',
                 'label': 'Number', 'fieldtype': 'Data', 'options': '', 'insert_after': ''}
        manifest = {'custom_doctypes': [], 'custom_fields': [field], 'relations': []}
        base = {'name': 'RULE', 'revision': 1, 'operation': 'HeroCustomers', 'schema_snapshot': 'SNAPSHOT',
                'schema_fingerprint': 'a' * 64}
        row = {'name': 'RULE', 'mapping_revision': 1, 'operation': 'HeroCustomers', 'schema_snapshot': 'SNAPSHOT',
               'schema_fingerprint': 'a' * 64, 'status': 'Active', 'personal_graphql_mapping': '',
               'schema_manifest': json.dumps(manifest), 'schema_dependencies': '{"ids":["field:customer"]}'}
        item = {'base_active_name': 'MAP', 'base_schema_fingerprint': 'a' * 64, 'operation_name': 'contacts',
                'content': {'target_operation': 'HeroCustomers', 'mapping_definition_ids': ['customer'],
                            'field_rules': {'customer': {'target_doctype': 'Customer', 'target_field': 'custom_hero_number'}},
                            'field_rule_bases': {'customer': base}}}
        with patch.object(self.frappe, 'get_all', return_value=[row]):
            self.assertEqual(exchange._export_schema([item], 'QUERY_TO_FRAPPE', 'SNAPSHOT'), manifest)
            row['schema_dependencies'] = '{"ids":["field:missing"]}'
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'invalid or incomplete'):
                exchange._export_schema([item], 'QUERY_TO_FRAPPE', 'SNAPSHOT')
            row['mapping_revision'] = 2
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'provenance is stale'):
                exchange._export_schema([item], 'QUERY_TO_FRAPPE', 'SNAPSHOT')
            row['mapping_revision'] = 1
            row['schema_dependencies'] = '[]'
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'invalid or incomplete'):
                exchange._export_schema([item], 'QUERY_TO_FRAPPE', 'SNAPSHOT')
        with patch.object(self.frappe, 'get_all', return_value=[row, row]):
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'missing or ambiguous'):
                exchange._export_schema([item], 'QUERY_TO_FRAPPE', 'SNAPSHOT')

    def test_preview_reports_omitted_own_drafts_without_touching_other_direction_or_owner(self):
        package = json.loads(self.package())
        package["mappings"] = []
        preview = exchange.preview_import(json.dumps(package), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")
        self.assertEqual(["REMOVE"], [item["state"] for item in preview["items"]])
        self.assertEqual("query-one", preview["items"][0]["definition_id"])

    def test_mixed_or_wrong_direction_is_rejected_before_schema_preview(self):
        package = json.loads(self.package())
        package["mappings"][0]["direction"] = "FRAPPE_TO_MUTATION"
        with self.assertRaisesRegex(exchange.MappingDraftExchangeError, "mixed or wrong"):
            exchange.preview_import(json.dumps(package), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")
        self.schema_mock.assert_not_called()

    def test_duplicate_fields_at_any_level_are_rejected_before_preview_or_writes(self):
        package = self.package()
        payloads = [
            package.replace('"direction":"QUERY_TO_FRAPPE"',
                            '"direction":"FRAPPE_TO_MUTATION","direction":"QUERY_TO_FRAPPE"', 1),
            package.replace('"value":9007199254740993',
                            '"value":1,"value":9007199254740993', 1),
        ]
        with patch.object(exchange, 'provision_schema') as provision:
            for payload in payloads:
                with self.subTest(payload=payload):
                    with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'not valid JSON'):
                        exchange.preview_import(payload, 'source-1', 'SNAPSHOT', 'QUERY_TO_FRAPPE')
                    with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'not valid JSON'):
                        exchange.save_import(payload, 'source-1', 'SNAPSHOT', 'QUERY_TO_FRAPPE', [],
                                             confirmed=True, preview_fingerprint='unused')
        self.context_lock_mock.assert_not_called()
        self.schema_mock.assert_not_called()
        provision.assert_not_called()

    def test_save_uses_expected_states_and_discards_only_omitted_own_draft(self):
        package = json.loads(self.package())
        package["mappings"] = []
        preview = exchange.preview_import(json.dumps(package), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")
        expected = [{"definition_id": item["definition_id"], **item["expected"]} for item in preview["items"]]
        calls = []

        class Service:
            def discard(self, *args, **kwargs):
                calls.append(("discard", args, kwargs))

        class Storage:
            def __init__(self, *_args):
                pass

            def service(self):
                return Service()

        with patch.object(exchange, "FrappeMappingDraftStorage", Storage), patch.object(
            exchange, "provision_schema", return_value={"status": "UNCHANGED", "created": {"custom_doctypes": [], "custom_fields": []}}
        ):
            result = exchange.save_import(json.dumps(package), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE", expected, confirmed=True,
                                          preview_fingerprint=preview['preview_fingerprint'])
        self.assertEqual(["query-one"], result["drafts"]["discarded"])
        self.assertEqual("discard", calls[0][0])
        self.assertEqual("query-one", calls[0][1][0])

    def test_stale_expected_state_is_rejected_before_schema_provisioning(self):
        preview = exchange.preview_import(self.package(), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")
        expected = [{"definition_id": item["definition_id"], **item["expected"]} for item in preview["items"]]
        expected[0]["draft_revision"] += 1
        with patch.object(exchange, "provision_schema") as provision:
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, "state changed"):
                exchange.save_import(self.package(), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE", expected, confirmed=True,
                                     preview_fingerprint=preview['preview_fingerprint'])
        provision.assert_not_called()

    def test_changed_payload_or_reference_invalidates_preview_before_schema(self):
        raw = self.package()
        preview = exchange.preview_import(raw, 'source-1', 'SNAPSHOT', 'QUERY_TO_FRAPPE')
        expected = [{'definition_id': item['definition_id'], **item['expected']} for item in preview['items']]
        package = json.loads(raw)
        package['mappings'][0]['content']['source']['field'] = 'changed'
        with patch.object(exchange, 'provision_schema') as provision:
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'preview changed'):
                exchange.save_import(json.dumps(package), 'source-1', 'SNAPSHOT', 'QUERY_TO_FRAPPE', expected,
                                     confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            self.preparation_mock.side_effect = lambda item, row, local, schema: {
                'content': item['content'], 'references': [{'identity': 'changed'}], 'conflicts': [], 'can_save': True}
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'preview changed'):
                exchange.save_import(raw, 'source-1', 'SNAPSHOT', 'QUERY_TO_FRAPPE', expected,
                                     confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
        provision.assert_not_called()

    def test_new_active_definition_without_draft_invalidates_preview(self):
        raw = self.package()
        preview = exchange.preview_import(raw, 'source-1', 'SNAPSHOT', 'QUERY_TO_FRAPPE')
        expected = [{'definition_id': item['definition_id'], **item['expected']} for item in preview['items']]
        self.frappe.active.append(self.frappe._active('query-new', 'QUERY', 'readThings', 'QUERY_TO_FRAPPE'))
        changed = exchange.preview_import(raw, 'source-1', 'SNAPSHOT', 'QUERY_TO_FRAPPE')
        self.assertEqual(preview['items'], changed['items'])
        self.assertNotEqual(preview['preview_fingerprint'], changed['preview_fingerprint'])
        with patch.object(exchange, 'provision_schema') as provision:
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, 'preview changed'):
                exchange.save_import(raw, 'source-1', 'SNAPSHOT', 'QUERY_TO_FRAPPE', expected,
                                     confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
        provision.assert_not_called()

class TestPortableEmptyTarget(unittest.TestCase):
    """Portable packages create personal drafts without an active definition."""

    def _roundtrip(self, direction, kind, operation, content):
        from hero_integration.tests.test_portable_draft_exchange import Fake, IDENTITY, TestPortableDraftExchange
        helper = TestPortableDraftExchange()
        source = Fake('source.test', 'source-1', 'SNAPSHOT')
        target = Fake('target.test', 'source-2', 'TARGET')
        definition = {'site_scope': source.local.site, 'definition_id': IDENTITY, 'source_instance': 'source-1',
                      'snapshot_name': 'SNAPSHOT', 'schema_fingerprint': source.snapshot.schema_fingerprint,
                      'direction': direction, 'operation_kind': kind, 'operation_name': operation}
        with helper.context(source):
            exchange.FrappeMappingDraftStorage().service().open_new_draft(definition, source.session.user, content)
            package = exchange.export_package('source-1', 'SNAPSHOT', direction, format_version=exchange.PORTABLE_FORMAT_VERSION)
        with helper.context(target):
            preview = exchange.preview_import(package, 'source-2', 'TARGET', direction)
            self.assertTrue(preview['can_save'])
            self.assertEqual(preview['items'][0]['state'], 'NEW')
            expected = [dict(definition_id=item['definition_id'], **item['expected']) for item in preview['items']]
            saved = exchange.save_import(package, 'source-2', 'TARGET', direction, expected,
                                         confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
            self.assertFalse(saved['activation'])
            draft = exchange.FrappeMappingDraftStorage().service().open_draft(IDENTITY, target.session.user)
            self.assertEqual(draft.definition_id, IDENTITY)
            self.assertEqual(draft.content, content)
            self.assertEqual(target.active, [])
            self.assertEqual(package, exchange.export_package('source-2', 'TARGET', direction,
                                                             format_version=exchange.PORTABLE_FORMAT_VERSION))

    def test_query_fresh_target_creates_owned_draft(self):
        self._roundtrip('QUERY_TO_FRAPPE', 'QUERY', 'customers',
                        {'records_path': 'customers', 'identity_path': 'id', 'mapping_definition_ids': [], 'field_rules': {}})

    def test_mutation_fresh_target_creates_owned_draft(self):
        self._roundtrip('FRAPPE_TO_MUTATION', 'MUTATION', 'updateCustomer',
                        {'source': {'doctype': 'Customer', 'field': 'customer_name'},
                         'target': {'operation': 'updateCustomer', 'argument': 'input', 'input_type': 'CustomerInput',
                                    'field': 'name', 'graphql_type': 'String'}, 'transformation': 'Identity'})
