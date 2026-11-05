"""Portable contracts preserve source scope, native approval and idempotence."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from hero_integration.services import config_mutation_contracts as exchange
from hero_integration.services.graphql_contract import parse_mutation
from hero_integration.tests.test_mapped_update_v2 import mapping, SCHEMA


def fixtures(area='HeroTasks', status='Active'):
    content = mapping(area)
    definition = str(uuid4())
    group = {'definition_id': definition, 'operation_kind': 'MUTATION', 'direction': 'FRAPPE_TO_MUTATION',
             'operation_name': content['target']['operation'], 'content': content, 'dependencies': []}
    operation = group['operation_name']
    if area in ('HeroDocuments', 'HeroPictures'):
        query = 'mutation Update($id:Int,$filename:String){update_file_upload(id:$id,filename:$filename){id}}'
    else:
        query = 'mutation Update($task:TaskInput){update_task(task:$task){id}}'
    item = {'operation_name': operation, 'service_name': 'Aufgaben aktualisieren', 'query': query, 'status': status,
            'mapping': {'mode': 'VALIDATED_RESOLVER', 'resolver': 'MAPPED_UPDATE_V2', 'areas': [area],
                        'mapping_definition_ids': [definition]}}
    return item, group


class TestPortableMutationContractValidation(TestCase):
    def test_new_target_snapshot_preview_needs_no_persisted_document_or_database(self):
        from hero_integration.services import config_exchange
        item, group = fixtures()
        snapshot = SimpleNamespace(name='package-preview', snapshot_json=json.dumps(SCHEMA))
        payload = {'mapping_package': {'schema': {'custom_doctypes': [], 'custom_fields': [], 'relations': []}},
                   'read_services': [], 'mutation_mappings': [group], 'mutation_contracts': [item]}
        with patch('hero_integration.services.schema_provisioning.preview_schema', return_value={'items': []}), \
             patch('hero_integration.services.schema_provisioning._PlannedMetadata') as metadata, \
             patch('hero_integration.services.mapped_update_v2.validate_sources'), \
             patch.object(exchange, 'frappe', SimpleNamespace()):
            report = config_exchange._replacement_check(snapshot, payload, {})
        self.assertTrue(report['mutation_contracts']['can_apply'])
        self.assertEqual(report['mutation_contracts']['counts']['New'], 1)
        self.assertFalse(report['mutation_contracts']['execution'])

    def test_roundtrip_is_canonical_and_does_not_mutate_input(self):
        item, group = fixtures();before = deepcopy(item)
        result = exchange.validate([item], SCHEMA, [group])
        self.assertEqual(item, before)
        self.assertEqual(exchange.validate(result, SCHEMA, [group]), result)
        self.assertEqual(result[0]['status'], 'Active')
        self.assertNotIn('contract_revision', result[0])

    def test_draft_is_explicit_and_not_implicitly_active(self):
        item, group = fixtures(status='Draft')
        self.assertEqual(exchange.validate([item], SCHEMA, [group])[0]['status'], 'Draft')
        item.pop('status')
        with self.assertRaises(ValueError): exchange.validate([item], SCHEMA, [group])

    def test_secrets_gates_and_arbitrary_scope_keys_are_rejected(self):
        item, group = fixtures()
        for key in ('api_token', 'endpoint', 'authorization', 'execute', 'mutation_execution_enabled', 'contract_revision'):
            value = deepcopy(item);value[key] = 'private'
            with self.subTest(key=key), self.assertRaises(ValueError): exchange.validate([value], SCHEMA, [group])
        item['mapping']['allow_delete'] = True
        with self.assertRaises(ValueError): exchange.validate([item], SCHEMA, [group])

    def test_literals_defaults_aliases_directives_and_extra_results_are_rejected(self):
        item, group = fixtures()
        queries = [
          'mutation {update_task(task:{id:2}){id}}',
          'mutation Update($task:TaskInput={id:2}){update_task(task:$task){id}}',
          'mutation Update($task:TaskInput){other:update_task(task:$task){id}}',
          'mutation Update($task:TaskInput){update_task(task:$task){id name}}',
          'mutation Update($task:TaskInput){update_task(task:$task){alias:id}}',
          'mutation Update($task:TaskInput){update_task(task:$task){id @skip(if:true)}}',
          'mutation Update($task:String){update_task(task:$task){id}}',
          'mutation Update($task:TaskInput){update_task(task:$task){id}} mutation Extra{update_task{id}}',
        ]
        for query in queries:
            with self.subTest(query=query), self.assertRaises(ValueError):
                exchange.validate([{**item, 'query': query}], SCHEMA, [group])

    def test_private_comments_do_not_travel(self):
        item, group = fixtures();item['query'] = '# token=private\n' + item['query']
        self.assertNotIn('private', exchange.validate([item], SCHEMA, [group])[0]['query'])

    def test_exact_mapping_ids_areas_and_argument_closure_are_required(self):
        item, group = fixtures()
        for change in ('identity', 'area', 'operation', 'arguments'):
            value = deepcopy(item)
            if change == 'identity': value['mapping']['mapping_definition_ids'] = [str(uuid4())]
            elif change == 'area': value['mapping']['areas'] = ['HeroCustomers']
            elif change == 'operation': value['operation_name'] = 'delete_task'
            else: value['query'] = 'mutation Update{update_task{id}}'
            with self.subTest(change=change), self.assertRaises(ValueError): exchange.validate([value], SCHEMA, [group])

    def test_shared_file_service_covers_both_groups_exactly_once(self):
        item, first = fixtures('HeroDocuments');_, second = fixtures('HeroPictures')
        item['mapping']['areas'].append('HeroPictures')
        item['mapping']['mapping_definition_ids'].append(second['definition_id'])
        self.assertEqual(len(exchange.validate([item], SCHEMA, [first, second])), 1)
        with self.assertRaises(ValueError): exchange.validate([item, item], SCHEMA, [first, second])
        with self.assertRaises(ValueError): exchange.validate([item], SCHEMA, [first])

    def test_schema_must_expose_id_result(self):
        item, group = fixtures();altered = deepcopy(SCHEMA)
        record = next(t for t in altered['__schema']['types'] if t['name'] == 'Record')
        record['fields'] = [f for f in record['fields'] if f['name'] != 'id']
        with self.assertRaises(ValueError): exchange.validate([item], altered, [group])


class FakeStore:
    def __init__(self, item):
        self.snapshot = SimpleNamespace(name='snapshot', source_instance='source', schema_fingerprint='fingerprint',
            snapshot_json=json.dumps(SCHEMA), analysis_status='Validated', execution_contract_status='', check_permission=Mock())
        self.services = {};self.contracts = {};self.writes = [];self.item = item
        self.db = SimpleNamespace(savepoint=Mock(), rollback=Mock(), set_value=self.set_value)

    def documents(self, snapshot, operation, **kwargs):
        return ([doc for doc in self.services.values() if doc.operation_name == operation],
                [doc for doc in self.contracts.values() if doc.operation_name == operation])

    def get_doc(self, data, *args, **kwargs):
        if isinstance(data, str):
            return self.snapshot if data == 'HERO GraphQL Schema Snapshot' else (self.services if data == 'HERO GraphQL Service' else self.contracts)[args[0]]
        store = self
        class Doc(SimpleNamespace):
            def insert(self):
                self.name = 'service' if self.doctype == 'HERO GraphQL Service' else 'contract'
                self.schema_fingerprint = 'fingerprint'
                if self.doctype == 'HERO GraphQL Service':
                    self.status = 'Draft';self.read_mapping = None
                    store.services[self.name] = self
                else:
                    service = store.services[self.graphql_service]
                    self.schema_snapshot = service.schema_snapshot;self.operation_name = service.operation_name
                    self.contract_fingerprint = parse_mutation(service.query_document).fingerprint
                    store.contracts[self.name] = self
                store.writes.append(('insert', self.doctype))
                return self
            def save(self): store.writes.append(('save', self.doctype));return self
            def reload(self): return self
            def add_comment(self, *args): store.writes.append(('comment', self.doctype))
        return Doc(**data)

    def set_value(self, doctype, name, values):
        self.writes.append(('set_value', doctype, deepcopy(values)))
        document = (self.services if doctype == 'HERO GraphQL Service' else self.contracts)[name]
        for key, value in values.items(): setattr(document, key, value)


class TestPortableMutationContractStorage(TestCase):
    def setUp(self):
        self.item, self.group = fixtures()
        self.store = FakeStore(self.item)
        patches = [patch.object(exchange, '_snapshot', return_value=(self.store.snapshot, SCHEMA)),
            patch.object(exchange, '_documents', side_effect=self.store.documents),
            patch.object(exchange.frappe, 'get_doc', side_effect=self.store.get_doc),
            patch.object(exchange.frappe, 'db', self.store.db),
            patch('hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping._lock_active_target')]
        for p in patches: p.start();self.addCleanup(p.stop)

    def apply(self, item=None):
        return exchange.apply('snapshot', [item or self.item], 'source', confirmed=True, mutation_mappings=[self.group])

    def test_new_active_contract_uses_native_saves_and_separate_local_approval(self):
        result = self.apply()
        self.assertEqual(result['counts']['New'], 1)
        self.assertEqual(self.store.contracts['contract'].status, 'Active')
        self.assertEqual(self.store.services['service'].status, 'Validated')
        self.assertFalse(result['execution'])
        self.assertEqual([entry[0] for entry in self.store.writes], ['insert', 'insert', 'set_value', 'comment'])
        self.assertEqual(set(self.store.writes[2][2]), {'status', 'validation_summary'})

    def test_same_import_and_export_are_idempotent(self):
        self.apply();self.store.writes.clear()
        preview = exchange.preview('snapshot', [self.item], 'source', mutation_mappings=[self.group])
        self.assertEqual(preview['counts']['Unchanged'], 1)
        self.assertEqual(self.apply()['counts']['Unchanged'], 1)
        self.assertFalse(self.store.writes)
        exported = exchange.export('source', 'snapshot', mutation_mappings=[self.group])
        self.assertEqual(exported, exchange.validate([self.item], SCHEMA, [self.group]))

    def test_draft_import_never_promotes_service(self):
        self.item['status'] = 'Draft';self.apply()
        self.assertEqual(self.store.services['service'].status, 'Draft')
        self.assertEqual(self.store.contracts['contract'].status, 'Draft')
        self.assertFalse(any(entry[0] == 'set_value' for entry in self.store.writes))
        self.item['status'] = 'Active';self.store.writes.clear()
        self.assertEqual(self.apply()['counts']['Approve'], 1)
        self.assertEqual(self.store.contracts['contract'].status, 'Active')

    def test_changed_service_scope_or_downgrade_conflicts_before_writes(self):
        self.apply()
        for changed in ({**self.item, 'service_name': 'Other label'}, {**self.item, 'status': 'Draft'}):
            self.store.writes.clear()
            preview = exchange.preview('snapshot', [changed], 'source', mutation_mappings=[self.group])
            self.assertFalse(preview['can_apply']);self.assertEqual(preview['counts']['Conflict'], 1)
            with self.assertRaises(ValueError): self.apply(changed)
            self.assertFalse(self.store.writes)

    def test_all_items_validated_before_first_write(self):
        wrong = deepcopy(self.item);wrong['operation_name'] = 'delete_task'
        with self.assertRaises(ValueError):
            exchange.apply('snapshot', [self.item, wrong], 'source', confirmed=True, mutation_mappings=[self.group])
        self.assertFalse(self.store.writes)

    def test_explicit_boolean_confirmation_required_before_access(self):
        for confirmation in (False, 1, '1', 'true'):
            with self.assertRaises(ValueError): exchange.apply('snapshot', [self.item], 'source', confirmed=confirmation)
        self.assertFalse(self.store.writes)

    def test_failed_approval_rolls_back_own_writes_without_commit(self):
        with patch.object(exchange, '_approve', side_effect=ValueError('changed')):
            with self.assertRaises(ValueError): self.apply()
        self.store.db.rollback.assert_called_once_with(save_point='hero_config_mutation_contracts')

    def test_export_ignores_unreferenced_operations(self):
        self.apply()
        self.store.contracts['unrelated'] = SimpleNamespace(operation_name='delete_task', status='Active')
        self.assertEqual(len(exchange.export('source', 'snapshot', mutation_mappings=[self.group])), 1)

    def test_duplicate_services_conflict_without_creating_more(self):
        self.apply();other = deepcopy(self.store.services['service']);other.name = 'other'
        self.store.services['other'] = other;self.store.writes.clear()
        with self.assertRaises(ValueError): self.apply()
        self.assertFalse(self.store.writes)

    def test_source_and_fingerprint_guard_is_independent_of_payload(self):
        # Exercise the real guard rather than the fixture's trusted snapshot hook.
        guard = self._real_snapshot
        with patch.object(exchange.frappe, 'only_for'),patch.object(exchange, 'analyze_snapshot', return_value=SimpleNamespace(fingerprint='fingerprint')):
            with self.assertRaises(ValueError): guard(self.store.snapshot, 'another-source')
            self.store.snapshot.schema_fingerprint = 'stale'
            with self.assertRaises(ValueError): guard(self.store.snapshot, 'source')

    _real_snapshot = staticmethod(exchange._snapshot)


class TestAllRegisteredContractOperations(TestCase):
    def test_eight_contracts_cover_nine_areas_without_duplicate_file_operation(self):
        from graphql import build_client_schema
        from hero_integration.services.mapped_update_v2 import AREAS
        schema = build_client_schema(SCHEMA)
        groups, collected = [], {}
        for area in AREAS:
            content = mapping(area);operation = content['target']['operation']
            identity = str(uuid4())
            groups.append({'definition_id': identity, 'operation_kind': 'MUTATION', 'operation_name': operation,
                           'direction': 'FRAPPE_TO_MUTATION', 'content': content})
            row = collected.setdefault(operation, {'areas': [], 'ids': [], 'arguments': set()})
            row['areas'].append(area);row['ids'].append(identity)
            row['arguments'].update(a['input_path'].split('.')[0] for a in
                                    [content['identity'], *content['assignments'], *content['required_readback']])
        items = []
        for operation, row in collected.items():
            args = sorted(row['arguments']);field = schema.mutation_type.fields[operation]
            query = ('mutation Update(' + ','.join('$'+arg+':'+str(field.args[arg].type) for arg in args)
                     + '){' + operation + '(' + ','.join(arg+':$'+arg for arg in args) + '){id}}')
            items.append({'operation_name': operation, 'service_name': operation, 'query': query, 'status': 'Active',
                          'mapping': {'mode': 'VALIDATED_RESOLVER', 'resolver': 'MAPPED_UPDATE_V2',
                                      'areas': row['areas'], 'mapping_definition_ids': row['ids']}})
        validated = exchange.validate(items, SCHEMA, groups)
        self.assertEqual(len(validated), 8)
        self.assertEqual(sum(len(item['mapping']['areas']) for item in validated), 9)
        files = next(item for item in validated if item['operation_name'] == 'update_file_upload')
        self.assertEqual(files['mapping']['areas'], ['HeroDocuments', 'HeroPictures'])
