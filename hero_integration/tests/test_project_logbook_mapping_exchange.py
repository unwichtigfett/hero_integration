"""Project logbook is a separate scoped mutation profile, never a new area."""
from copy import deepcopy
from contextlib import contextmanager
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from graphql import build_client_schema, extend_schema, introspection_from_schema, parse

from hero_integration.services import personal_mapping_content as personal, mapping_draft_exchange as exchange
from hero_integration.services import config_mutation_drafts as drafts, config_mutation_contracts as contracts
from hero_integration.services import project_logbook_writeback as logbook, simple_mapping
from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.tests.test_mapped_update_v2 import SCHEMA as BASE_SCHEMA, mapping as update_mapping
from hero_integration.services.mapped_update_v2 import AREAS

SCHEMA = introspection_from_schema(extend_schema(build_client_schema(BASE_SCHEMA), parse('''
enum LogbookTarget { project_match contact }
input LogbookEntryInput { target: LogbookTarget! target_id: Int! custom_text: String! }
type LogbookEntry { id: Int custom_text: String }
extend type Mutation { add_logbook_entry(logbook_entry: LogbookEntryInput!): LogbookEntry }
''')))


def content():
    return {'usage': 'PROJECT_LOGBOOK_V1', 'version': 1, 'area': 'HeroProjectMatches',
            'read_service': '$read_service:HeroProjectMatches', 'source': {'doctype': 'Comment'},
            'target': {'operation': 'add_logbook_entry'}, 'parent': deepcopy(logbook.PARENT),
            'assignments': [{'id': str(uuid4()), 'source': {'doctype': 'Comment', 'field': 'content'},
                             'input_path': 'logbook_entry.custom_text', 'resolver': 'PLAIN_TEXT', 'options': {}}]}


def automatic_content():
    return {**content(), 'comment_types': ['Comment', 'Assigned', 'Assignment Completed'],
            'version_source': {'doctype': 'Version', 'field': 'data',
                               'reference_doctype_field': 'ref_doctype', 'reference_name_field': 'docname',
                               'resolver': 'CHANGE_SUMMARY'}}


def group(value=None):
    value = value or content()
    return {'definition_id': str(uuid4()), 'operation_kind': 'MUTATION', 'direction': 'FRAPPE_TO_MUTATION',
            'operation_name': value['target']['operation'], 'content': value, 'dependencies': []}


def portable(groups):
    return {'format_version': exchange.PORTABLE_FORMAT_VERSION, 'schema_fingerprint': 'a'*64,
            'direction': 'FRAPPE_TO_MUTATION', 'schema': {'custom_doctypes': [], 'custom_fields': [], 'relations': []},
            'mappings': groups}


class TestLogbookPortableLifecycle(TestCase):
    def test_automatic_sources_roundtrip_as_one_profile(self):
        row = group(automatic_content())
        parsed = exchange._parse_portable(json.dumps(portable([row])), 'FRAPPE_TO_MUTATION')
        self.assertEqual(parsed['mappings'], [row])
        self.assertEqual(personal.validate_edits(row['content'], deepcopy(row['content'])), row['content'])
        self.assertEqual(personal.validate_exchange_action(row['content'], SCHEMA, logbook.OPERATION)['mapping'], row['content'])
        self.assertEqual(len(contracts._mapping_scopes([row], SCHEMA)), 1)

    def test_automatic_options_are_reserved_to_logbook_and_cannot_escape_normalizer(self):
        for option in ('comment_types', 'version_source'):
            value = update_mapping('HeroProjectMatches')
            value[option] = automatic_content()[option]
            with self.subTest(option=option), self.assertRaises(ValueError):
                personal.validate_mapped_update_shape(value)
        for key, value in (('doctype', 'User'), ('field', 'password'), ('reference_doctype_field', 'owner'),
                           ('reference_name_field', 'name'), ('resolver', 'Identity'), ('execute', True)):
            config = automatic_content();config['version_source'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                personal.validate_exchange_action(config, SCHEMA, logbook.OPERATION)

    def test_automatic_metadata_and_list_have_one_comment_and_one_version_source(self):
        value = automatic_content()
        sources = {(row['doctype'], row['field']) for row in personal.configured_mutation_sources(value)}
        self.assertEqual(sources, {('Comment', field) for field in ('content', 'reference_doctype', 'reference_name', 'comment_type')}
                         | {('Version', field) for field in ('data', 'ref_doctype', 'docname')})
        with patch.object(simple_mapping, '_app', return_value='frappe'):
            rows = simple_mapping._send_rows(value, 'mapping', 1, logbook.OPERATION, 'Active')
        self.assertEqual(len(rows), 2)
        self.assertEqual({(row['target'], row['hero'], row['transformation']) for row in rows}, {
            ('Comment.content', 'logbook_entry.custom_text', 'PLAIN_TEXT'),
            ('Version.data', 'logbook_entry.custom_text', 'CHANGE_SUMMARY')})
        self.assertEqual({row['mapping'] for row in rows}, {'mapping'})

    def test_only_explicit_native_version_data_may_be_a_hidden_source(self):
        value = automatic_content()
        metadata = {}
        for source in personal.configured_mutation_sources(value):
            metadata.setdefault(source['doctype'], {'fields': []})['fields'].append({
                'fieldname': source['field'], 'hidden': int((source['doctype'], source['field']) == ('Version', 'data')),
                'permlevel': 0, 'is_virtual': 0})
        with patch.object(exchange, '_rule_target_metadata', return_value=metadata):
            self.assertEqual(exchange._portable_target_metadata(value, 'FRAPPE_TO_MUTATION'), metadata)
            for flag in ('permlevel', 'is_virtual'):
                metadata['Version']['fields'][0][flag] = 1
                with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, 'restricted'):
                    exchange._portable_target_metadata(value, 'FRAPPE_TO_MUTATION')
                metadata['Version']['fields'][0][flag] = 0
            metadata['Comment']['fields'][0]['hidden'] = 1
            with self.assertRaisesRegex(ValueError, 'restricted'):
                exchange._portable_target_metadata(value, 'FRAPPE_TO_MUTATION')
            metadata['Comment']['fields'][0]['hidden'] = 0
            value['version_source']['resolver'] = 'Identity'
            with self.assertRaisesRegex(ValueError, 'restricted'):
                exchange._portable_target_metadata(value, 'FRAPPE_TO_MUTATION')

    def test_closed_portable_roundtrip_and_real_normalizer(self):
        row = group();parsed = exchange._parse_portable(json.dumps(portable([row])), 'FRAPPE_TO_MUTATION')
        self.assertEqual(parsed['mappings'][0], row)
        self.assertEqual(logbook.normalise_mapping(row['content'], SCHEMA), row['content'])
        with self.assertRaises(ValueError): exchange._portable_content(row['content'], 'QUERY_TO_FRAPPE')
        for extra in ({'identity': {}}, {'required_readback': []}, {'execute': True}, {'transformation': 'Identity'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                exchange._portable_content({**row['content'], **extra}, 'FRAPPE_TO_MUTATION')

    def test_parent_and_assignment_authority_cannot_be_imported(self):
        for change in ('parent', 'assignment', 'source', 'area'):
            value = content()
            if change == 'parent': value['parent']['source_type'] = 'customer'
            elif change == 'assignment': value['assignments'][0]['target_id'] = 5
            elif change == 'source': value['source'] = {'doctype': 'Project'}
            else: value['area'] = 'HeroCustomers'
            with self.subTest(change=change), self.assertRaises(ValueError):
                exchange._portable_content(value, 'FRAPPE_TO_MUTATION')

    def test_draft_edit_exchange_and_publisher_dispatch_preserve_configuration(self):
        value = content()
        self.assertEqual(personal.validate_edits(value, deepcopy(value)), value)
        self.assertEqual(personal.validate_exchange_action(value, SCHEMA, logbook.OPERATION)['mapping'], value)
        with self.assertRaises(ValueError): personal.validate_exchange_action(value, SCHEMA, 'update_project_match')
        self.assertEqual(personal.prepare_exchange_content(value, value, lambda _: self.fail('No Query rules.'))['content'], value)
        with patch.object(logbook, 'validate_sources') as validate:
            self.assertEqual(FrappeMappingDraftStorage(NS())._validate_configured_send(value, SCHEMA), value)
            validate.assert_called_once_with(value)

    def test_parent_reference_and_comment_kind_metadata_are_bound(self):
        probes = []
        def metadata(probe, _manifest):
            probes.append(probe)
            return {'Comment': {'fields': [{'fieldname': name, 'hidden': 0, 'permlevel': 0, 'is_virtual': 0}
                                          for name in ('content', 'reference_doctype', 'reference_name', 'comment_type')]}}
        with patch.object(exchange, '_rule_target_metadata', side_effect=metadata):
            exchange._portable_target_metadata(content(), 'FRAPPE_TO_MUTATION')
        selected = {rule['target_field'] for rule in probes[0]['field_rules'].values()}
        self.assertEqual(selected, {'content', 'reference_doctype', 'reference_name', 'comment_type'})

    def test_list_has_distinct_project_logbook_label_without_duplicate_parent_row(self):
        with patch.object(simple_mapping, '_app', return_value='frappe'):
            rows = simple_mapping._send_rows(content(), 'mapping', 1, logbook.OPERATION, 'Active')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['operation_label'], 'Project logbook')
        self.assertEqual(rows[0]['target'], 'Comment.content')
        self.assertEqual(rows[0]['hero'], 'logbook_entry.custom_text')


class TestLogbookConfigurationAndContract(TestCase):
    def fixture(self):
        groups = [group(update_mapping(area)) for area in AREAS] + [group()]
        payload = {'mutation_mappings': groups, 'schema_snapshot': {'schema_fingerprint': 'a'*64, 'schema': SCHEMA},
                   'read_services': [{'target_operation': area} for area in AREAS]}
        return payload

    def test_tenth_profile_coexists_with_nine_migration_areas(self):
        payload = self.fixture();drafts.validate(payload)
        self.assertEqual(drafts.summary(payload)['mutation_mappings']['count'], 10)
        self.assertFalse(drafts.summary(payload)['mutation_mappings']['execution'])
        repeated = deepcopy(payload);repeated['mutation_mappings'].append(group())
        with self.assertRaises(ValueError): drafts.validate(repeated)
        repeated = deepcopy(payload);repeated['mutation_mappings'][0] = group()
        with self.assertRaises(ValueError): drafts.validate(repeated)

    def test_ninth_contract_uses_exact_logbook_resolver_and_identity(self):
        payload = self.fixture();groups = payload['mutation_mappings'];schema = build_client_schema(SCHEMA)
        scopes = contracts._mapping_scopes(groups, SCHEMA)
        self.assertEqual(len(scopes), 9)
        items = []
        for operation, scope in scopes.items():
            args = sorted(scope['arguments']);field = schema.mutation_type.fields[operation]
            query = ('mutation Run('+','.join('$'+arg+':'+str(field.args[arg].type) for arg in args)+'){'
                     +operation+'('+','.join(arg+':$'+arg for arg in args)+'){id}}')
            items.append({'operation_name': operation, 'service_name': operation, 'status': 'Active', 'query': query,
                'mapping': {'mode': 'VALIDATED_RESOLVER', 'resolver': logbook.USAGE if operation == logbook.OPERATION else 'MAPPED_UPDATE_V2',
                            'areas': scope['areas'], 'mapping_definition_ids': scope['mapping_definition_ids']}})
        checked = contracts.validate(items, SCHEMA, groups)
        self.assertEqual(len(checked), 9)
        entry = next(item for item in checked if item['operation_name'] == logbook.OPERATION)
        self.assertEqual(entry['mapping']['areas'], ['HeroProjectMatches'])
        self.assertEqual(entry['mapping']['mapping_definition_ids'], [groups[-1]['definition_id']])
        for change in ('resolver', 'area', 'identity'):
            broken = deepcopy(entry)
            if change == 'resolver': broken['mapping']['resolver'] = 'MAPPED_UPDATE_V2'
            elif change == 'area': broken['mapping']['areas'] = ['HeroTasks']
            else: broken['mapping']['mapping_definition_ids'] = [groups[1]['definition_id']]
            with self.subTest(change=change), self.assertRaises(ValueError): contracts.validate([broken], SCHEMA, groups)

    def test_configuration_export_selects_both_profiles_but_not_legacy_arbitrary_actions(self):
        selected = [group(update_mapping('HeroProjectMatches')), group()]
        with patch.object(exchange, '_export_portable', return_value=json.dumps(portable(selected))) as export:
            self.assertEqual(drafts.export('source', 'snapshot'), selected)
        export.assert_called_once_with('source', 'snapshot', 'FRAPPE_TO_MUTATION', usage='CONFIGURED_MUTATIONS')


class TestLogbookReadbackAndUniqueness(TestCase):
    def bind(self, *, identity='id', count=1):
        value = content();row = group(value)
        snapshot = NS(name='snapshot', source_instance='source', analysis_status='Validated', schema_fingerprint='schema')
        docs = {('HERO GraphQL Schema Snapshot', 'snapshot'): snapshot}
        services = []
        for index in range(count):
            name = 'reader'+str(index);services.append(name)
            docs[('HERO GraphQL Service', name)] = NS(name=name, read_mapping=name+'-mapping')
            docs[('HERO GraphQL Mapping', name+'-mapping')] = NS(name=name+'-mapping', content_hash='hash')
        fake = NS(get_doc=lambda dt, name: docs[(dt, name)], get_all=lambda *a, **kw: services, db=NS(sql=Mock()))
        contract = NS(target_operation='HeroProjectMatches', identity_path=identity, service_revision=2, mapping_revision=3)
        with patch.object(exchange, 'frappe', fake), patch('hero_integration.services.read_contract.from_documents', return_value=contract):
            bindings = exchange._bind_mapped_update_readbacks({'mappings': [row]}, 'source', 'snapshot')
        return row, bindings

    def test_foreign_reader_is_remapped_to_approved_project_reader_and_versions(self):
        row, bindings = self.bind()
        self.assertEqual(row['content']['read_service'], 'reader0')
        self.assertEqual(bindings[row['definition_id']]['service_revision'], 2)
        self.assertEqual(bindings[row['definition_id']]['mapping_revision'], 3)

    def test_missing_ambiguous_or_wrong_identity_project_reader_blocks(self):
        for options in ({'count': 0}, {'count': 2}, {'identity': 'project_id'}):
            with self.subTest(options=options), self.assertRaises(ValueError): self.bind(**options)

    def test_controller_coexistence_is_profile_specific(self):
        from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping as controller
        existing = update_mapping('HeroProjectMatches')
        row = NS(name='other', mapping_json=json.dumps(existing))
        fake = NS(db=NS(sql=Mock(return_value=[row])), _=lambda value: value, throw=Mock(side_effect=ValueError('conflict')))
        with patch.object(controller, 'frappe', fake),patch.object(controller, '_check_active_writeback_conflicts') as fields, \
             patch('hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping._lock_active_target') as lock:
            controller._check_active_mapped_update_conflicts('source', 'snapshot', content(), 'current')
            lock.assert_called_once_with('mapped-update:source', 'HeroProjectMatches', logbook.USAGE)
            fields.assert_called_once()
            row.mapping_json = json.dumps(content())
            with self.assertRaisesRegex(ValueError, 'conflict'):
                controller._check_active_mapped_update_conflicts('source', 'snapshot', content(), 'current')


class TestLogbookPublicationMetadataLocks(TestCase):
    def test_comment_kind_and_reference_fields_participate_in_locked_metadata(self):
        class AttrDict(dict):
            __getattr__ = dict.get
        definitions = {'Comment': [('content', 'HTML Editor', ''), ('reference_doctype', 'Link', 'DocType'),
                       ('reference_name', 'Dynamic Link', 'reference_doctype'), ('comment_type', 'Select', 'Comment\nInfo')],
                       'Version': [('data', 'Code', ''), ('ref_doctype', 'Link', 'DocType'),
                                   ('docname', 'Data', '')]}
        fields = {doctype: [AttrDict(name='field-'+name, fieldname=name, fieldtype=kind, options=options,
                           reqd=0, read_only=0, hidden=int(doctype == 'Version' and name == 'data'),
                           permlevel=0, is_virtual=0) for name, kind, options in values]
                  for doctype, values in definitions.items()}
        docs = {doctype: AttrDict(name=doctype, module='Core', custom=0, istable=0, issingle=0, is_virtual=0)
                for doctype in definitions}
        def get_meta(doctype):
            meta = AttrDict(docs[doctype])
            meta.get_field = lambda name: next(field for field in fields[doctype] if field.fieldname == name)
            return meta
        def sql(query, *args, **kwargs):
            if '`tabDocType`' in query: return [docs[args[0][0]]]
            if '`tabDocField`' in query: return fields[args[0][0]]
            return []
        fake = NS(db=NS(sql=Mock(side_effect=sql)), get_meta=get_meta)
        storage = NS(_definition=NS(direction='FRAPPE_TO_MUTATION', schema_snapshot='snapshot'))
        snapshot = NS(name='snapshot', schema_fingerprint='fingerprint')
        for value in (content(), automatic_content()):
            with self.subTest(automatic=bool(value.get('version_source'))), patch.object(exchange, 'frappe', fake), \
                 patch('hero_integration.api._validated_snapshot', return_value=(snapshot, SCHEMA)):
                locked = exchange._assignment_metadata(storage, [], content=value)
            expected = {(item['doctype'], item['field']) for item in personal.configured_mutation_sources(value)}
            self.assertEqual({(field['doctype'], field['field']) for field in locked['fields']}, expected)
            self.assertEqual({(doctype, field['fieldname']) for doctype, row in locked['locked_metadata'].items()
                              for field in row['fields']}, expected)
        self.assertTrue(all('for update' in call.args[0].lower() for call in fake.db.sql.call_args_list))

    def test_real_field_conflict_check_allows_project_update_but_blocks_duplicate_comment_owner(self):
        from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping as controller
        row = NS(name='project-update', mapping_json=json.dumps(update_mapping('HeroProjectMatches')))
        fake = NS(db=NS(sql=Mock(return_value=[row])), _=lambda value: value,
                  throw=Mock(side_effect=RuntimeError('field conflict')))
        with patch.object(controller, 'frappe', fake), \
             patch('hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping._lock_active_target'):
            controller._check_active_mapped_update_conflicts('source', 'snapshot', content(), 'logbook')
            fake.throw.assert_not_called()
            row.mapping_json = json.dumps(content())
            with self.assertRaisesRegex(RuntimeError, 'field conflict'):
                controller._check_active_mapped_update_conflicts('source', 'snapshot', content(), 'logbook')


class TestConfiguredMutationActiveExport(TestCase):
    def export_active(self, usage, *, locked_usage=None):
        groups = [group(update_mapping(area)) for area in AREAS] + [group(automatic_content())]
        parents = {row['definition_id']: NS(
            name=row['definition_id'], definition_id=row['definition_id'],
            portable_definition_id=row['definition_id'], source_instance='source',
            schema_snapshot='snapshot', schema_fingerprint='a'*64,
            direction='FRAPPE_TO_MUTATION', operation_kind='MUTATION',
            operation_name=row['operation_name'], mapping_json=json.dumps(row['content']),
        ) for row in groups}
        for parent in parents.values():
            parent.get = lambda key, default=None, parent=parent: getattr(parent, key, default)
        values = {row['definition_id']: row['content'] for row in groups}

        class Storage:
            def _authorize(self, _actor): pass
            @contextmanager
            def transaction(self, identity, _actor):
                self._definition = parents[identity]
                yield
            def draft(self, _key): return None
            def active(self, identity): return NS(content=deepcopy(values[identity]))
            def initial_content(self, value):
                if locked_usage: value['usage'] = locked_usage
                return value
            def validate_new_definition(self, definition, value):
                personal.validate_exchange_action(value, SCHEMA, definition['operation_name'])

        fake = NS(session=NS(user='Administrator'), local=NS(site='test'),
                  get_all=lambda doctype, **kwargs: list(parents.values()) if doctype == 'HERO GraphQL Mapping' else [])
        with patch.object(exchange, 'frappe', fake), \
             patch.object(exchange, 'FrappeMappingDraftStorage', Storage), \
             patch.object(exchange, 'assert_portable_identity_schema'), \
             patch.object(exchange, '_lock_context'), \
             patch.object(exchange, '_portable_snapshot', return_value=(NS(schema_fingerprint='a'*64), SCHEMA)), \
             patch.object(exchange, 'lock_identity_drafts'), \
             patch('hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping._lock_active_target'):
            return json.loads(exchange._export_portable(
                'source', 'snapshot', 'FRAPPE_TO_MUTATION', usage=usage))

    def test_configuration_export_includes_all_ten_active_profiles_after_locking(self):
        package = self.export_active('CONFIGURED_MUTATIONS')
        self.assertEqual(len(package['mappings']), 10)
        self.assertEqual({(row['content']['area'], row['content']['usage']) for row in package['mappings']},
                         {(area, 'MAPPED_UPDATE_V2') for area in AREAS} | {('HeroProjectMatches', 'PROJECT_LOGBOOK_V1')})
        logbook_content = next(row['content'] for row in package['mappings'] if row['content']['usage'] == 'PROJECT_LOGBOOK_V1')
        self.assertEqual(logbook_content['version_source'], automatic_content()['version_source'])
        self.assertEqual(logbook_content['comment_types'], automatic_content()['comment_types'])

    def test_specific_filters_retain_their_original_scope(self):
        for usage, expected in (('MAPPED_UPDATE_V2', 9), ('PROJECT_LOGBOOK_V1', 1)):
            with self.subTest(usage=usage):
                mappings = self.export_active(usage)['mappings']
                self.assertEqual(len(mappings), expected)
                self.assertEqual({row['content']['usage'] for row in mappings}, {usage})

    def test_unapproved_usage_change_after_locking_is_still_blocked(self):
        with self.assertRaisesRegex(ValueError, 'usage changed while locks'):
            self.export_active('CONFIGURED_MUTATIONS', locked_usage='SEND_TO_HERO')
