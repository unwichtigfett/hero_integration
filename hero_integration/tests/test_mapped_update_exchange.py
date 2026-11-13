"""Mapped update JSON uses the established portable draft gates without sending HERO mutations."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch, Mock
from graphql import build_schema, introspection_from_schema

from hero_integration.services import personal_mapping_content as personal, mapping_draft_exchange as exchange
from hero_integration.services import mapped_update_v2 as update
from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage


SCHEMA = introspection_from_schema(build_schema('''
type Query { contacts: [Contact] }
type Contact { id: Int full_name: String }
input ContactInput { id: Int full_name: String }
type Mutation { update_contact(contact: ContactInput!): Contact }
'''))


def content():
    return {'usage': 'MAPPED_UPDATE_V2', 'version': 2, 'area': 'HeroCustomers', 'read_service': 'source-local-read-name',
        'source': {'doctype': 'Customer'}, 'target': {'operation': 'update_contact'},
        'identity': {'read_path': 'id', 'input_path': 'contact.id', 'mutation_read_path': 'id', 'response_path': 'id'},
        'assignments': [{'id': '3d9865a4-e97f-4018-a13d-749a96eb9773', 'source': {'doctype': 'Customer', 'field': 'customer_name'},
                         'input_path': 'contact.full_name', 'read_path': 'full_name', 'query_rule_id': '', 'resolver': 'FIELD', 'options': {}}],
        'required_readback': []}


class TestMappedUpdatePortableContent(TestCase):
    def test_v2_local_dependency_and_onboarding_envelopes_keep_closed_shapes(self):
        base = {'query_adapter': {'version': 2}, 'mapping_definition_ids': [], 'field_rules': {}}
        project = {**base, 'target_operation': 'HeroProjectMatches', 'customer_service': 'customer-read',
                   'project_contract_version': 'v5'}
        self.assertEqual(exchange._portable_content(project, 'QUERY_TO_FRAPPE'), project)
        task = {**base, 'target_operation': 'HeroTasks', 'native_task_onboarding': {
            'source_instance': 'source-a', 'source_type': 'Task', 'confirmed': True}}
        self.assertEqual(exchange._portable_content(task, 'QUERY_TO_FRAPPE'), task)
        for extra in ({'confirmed': 1}, {'source_type': 'Customer'}, {'source_instance': ' source-a'}, {'grant': True}):
            broken = deepcopy(task); broken['native_task_onboarding'].update(extra)
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                exchange._portable_content(broken, 'QUERY_TO_FRAPPE')
        for patch_value in ({'customer_service': ''}, {'customer_service': {}},
                            {'target_operation': 'HeroTasks'}, {'project_contract_version': 'v6'}):
            with self.subTest(patch_value=patch_value), self.assertRaises(ValueError):
                exchange._portable_content({**project, **patch_value}, 'QUERY_TO_FRAPPE')
        with self.assertRaisesRegex(ValueError, 'confirmed locally'):
            personal.prepare_exchange_content(task, base, lambda _: self.fail('No rules.'))

    def test_portable_roundtrip_keeps_all_configuration_without_legacy_primary_field(self):
        value = content()
        self.assertEqual(exchange._portable_content(value, 'FRAPPE_TO_MUTATION'), value)
        package = {'format_version': exchange.PORTABLE_FORMAT_VERSION, 'schema_fingerprint': 'a' * 64,
            'direction': 'FRAPPE_TO_MUTATION', 'schema': {'custom_doctypes': [], 'custom_fields': [], 'relations': []},
            'mappings': [{'definition_id': '630916cb-1695-4263-9abd-a3b29ee7d597', 'operation_kind': 'MUTATION',
                          'operation_name': 'update_contact', 'direction': 'FRAPPE_TO_MUTATION', 'content': value, 'dependencies': []}]}
        parsed = exchange._parse_portable(json.dumps(package), 'FRAPPE_TO_MUTATION')
        self.assertEqual(parsed['mappings'][0]['content'], value)
        self.assertEqual(update.normalise_mapping(value, SCHEMA), value)

    def test_query_direction_unknown_authority_and_legacy_extra_keys_are_rejected(self):
        with self.assertRaises(ValueError): exchange._portable_content(content(), 'QUERY_TO_FRAPPE')
        for extra in ({'approved': True}, {'field_rules': {}}, {'send_options': {}}, {'transformation': 'Identity'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                exchange._portable_content({**content(), **extra}, 'FRAPPE_TO_MUTATION')

    def test_draft_edits_and_exchange_validation_preserve_assignment_collection(self):
        old = content(); new = deepcopy(old); new['assignments'][0]['resolver'] = 'PLAIN_TEXT'
        self.assertEqual(personal.validate_edits(old, new), new)
        action = personal.validate_exchange_action(new, SCHEMA, 'update_contact')
        self.assertEqual(action['mapping'], new)
        self.assertIsNone(action['graphql_type'])
        with self.assertRaises(ValueError): personal.validate_exchange_action(new, SCHEMA, 'update_task')
        prepared = personal.prepare_exchange_content(new, old, lambda _: self.fail('No Query rule provenance is imported.'))
        self.assertEqual(prepared['content'], new)

    def test_publisher_uses_update_normalizer_and_local_source_checks(self):
        storage = FrappeMappingDraftStorage(NS())
        with patch.object(update, 'validate_sources') as validate:
            self.assertEqual(storage._validate_configured_send(content(), SCHEMA), content())
            validate.assert_called_once_with(content())
        broken = content(); broken['assignments'][0]['input_path'] = 'contact.id'
        with patch.object(update, 'validate_sources'), self.assertRaises(ValueError):
            storage._validate_configured_send(broken, SCHEMA)

    def test_portable_metadata_uses_assignment_sources_not_missing_root_field(self):
        captured = []
        def metadata(probe, _):
            captured.append(probe)
            return {'Customer': {'fields': [{'fieldname': 'customer_name', 'hidden': 0, 'permlevel': 0, 'is_virtual': 0}]}}
        with patch.object(exchange, '_rule_target_metadata', side_effect=metadata):
            exchange._portable_target_metadata(content(), 'FRAPPE_TO_MUTATION')
        self.assertEqual(captured[0]['field_rules']['0'], {'target_doctype': 'Customer', 'target_field': 'customer_name'})


class TestPortableMappedReadback(TestCase):
    def run_binding(self, candidates):
        snapshot = NS(name='target-snapshot', source_instance='source-a', analysis_status='Validated', schema_fingerprint='hash')
        docs = {('HERO GraphQL Schema Snapshot', snapshot.name): snapshot}
        for name, area, revision in candidates:
            docs[('HERO GraphQL Service', name)] = NS(name=name, read_mapping=name + '-mapping')
            docs[('HERO GraphQL Mapping', name + '-mapping')] = NS(name=name + '-mapping', content_hash='hash-' + name)
        fake = NS(get_doc=lambda dt, name: docs[(dt, name)], get_all=lambda *a, **kw: [x[0] for x in candidates], db=NS(sql=Mock()))
        def read(service, mapping, snapshot):
            _, area, revision = next(x for x in candidates if x[0] == service.name)
            return NS(target_operation=area, identity_path='id', service_revision=revision, mapping_revision=3)
        package = {'mappings': [{'definition_id': 'definition', 'content': content()}]}
        with patch.object(exchange, 'frappe', fake), patch('hero_integration.services.read_contract.from_documents', side_effect=read):
            bindings = exchange._bind_mapped_update_readbacks(package, 'source-a', 'target-snapshot')
        return package, bindings

    def test_foreign_service_name_resolves_to_unique_approved_target_area(self):
        package, bindings = self.run_binding([('target-read', 'HeroCustomers', 7), ('unrelated', 'HeroTasks', 1)])
        self.assertEqual(package['mappings'][0]['content']['read_service'], 'target-read')
        self.assertEqual(bindings['definition']['service_revision'], 7)
        self.assertEqual(bindings['definition']['mapping_revision'], 3)

    def test_ambiguous_or_missing_target_readback_is_blocked(self):
        for candidates in ([], [('one', 'HeroCustomers', 1), ('two', 'HeroCustomers', 2)]):
            with self.subTest(candidates=candidates), self.assertRaisesRegex(ValueError, 'exactly one'):
                self.run_binding(candidates)


class TestTechnicalInboundMetadata(TestCase):
    def test_hidden_inbound_and_outbound_metadata_remains_usable(self):
        value = {'target_operation': 'HeroCustomers', 'query_adapter': {'version': 2}, 'mapping_definition_ids': ['rule'],
                 'field_rules': {'rule': {'source_path': 'is_contact_person', 'target_doctype': 'Customer',
                                          'target_field': 'custom_hero_is_contact_person'}}}
        metadata = {'Customer': {'fields': [{'fieldname': 'custom_hero_is_contact_person', 'hidden': 1, 'permlevel': 0, 'is_virtual': 0}]}}
        with patch.object(exchange, '_rule_target_metadata', return_value=metadata):
            self.assertEqual(exchange._portable_target_metadata(value, 'QUERY_TO_FRAPPE'), metadata)
            for change in ('source', 'operation', 'virtual', 'permission'):
                altered = deepcopy(value); meta = deepcopy(metadata)
                if change == 'source': altered['field_rules']['rule']['source_path'] = 'full_name'
                if change == 'operation': altered['target_operation'] = 'HeroProjectMatches'
                if change == 'virtual': meta['Customer']['fields'][0]['is_virtual'] = 1
                if change == 'permission': meta['Customer']['fields'][0]['permlevel'] = 1
                with self.subTest(change=change), patch.object(exchange, '_rule_target_metadata', return_value=meta):
                    if change in {'virtual', 'permission'}:
                        with self.assertRaises(ValueError):
                            exchange._portable_target_metadata(altered, 'QUERY_TO_FRAPPE')
                    else:
                        self.assertEqual(exchange._portable_target_metadata(altered, 'QUERY_TO_FRAPPE'), meta)
            outbound = content(); outbound['assignments'][0]['source']['field'] = 'custom_hero_is_contact_person'
            self.assertEqual(exchange._portable_target_metadata(outbound, 'FRAPPE_TO_MUTATION'), metadata)


class TestLockedPropertySetters(TestCase):
    def test_declared_target_allows_hidden_flag_and_hidden_property_setter(self):
        content = {'mapping_definition_ids': ['rule'], 'field_rules': {
            'rule': {'target_doctype': 'Customer', 'target_field': 'custom_label'}}}
        definition = {'doctype': 'Customer', 'fieldname': 'custom_label', 'fieldtype': 'Data'}
        schema = {'custom_fields': [definition]}
        field = {'name': 'custom-label', 'fieldname': 'custom_label', 'fieldtype': 'Data',
                 'options': '', 'reqd': 0, 'read_only': 0, 'hidden': 1, 'permlevel': 0, 'is_virtual': 0}
        setter = {'name': 'hide-label', 'doctype_or_field': 'DocField', 'field_name': 'custom_label',
                  'property': 'hidden', 'value': '1'}
        for overrides in ([], [setter]):
            fake = NS(db=NS(sql=Mock(side_effect=[[{'name': 'Customer'}], [], [field], overrides])))
            with self.subTest(overrides=overrides), patch.object(exchange, 'frappe', fake):
                self.assertIn('Customer', exchange._rule_target_metadata(content, schema))
        for flag in ('permlevel', 'is_virtual', 'read_only'):
            fake = NS(db=NS(sql=Mock(side_effect=[[{'name': 'Customer'}], [], [{**field, flag: 1}], []])))
            with self.subTest(flag=flag), patch.object(exchange, 'frappe', fake), self.assertRaises(ValueError):
                exchange._rule_target_metadata(content, schema)
        for prop in ('fieldtype', 'options', 'reqd', 'read_only', 'permlevel', 'is_virtual'):
            fake = NS(db=NS(sql=Mock(side_effect=[[{'name': 'Customer'}], [], [field], [{**setter, 'property': prop}]])))
            with self.subTest(prop=prop), patch.object(exchange, 'frappe', fake), self.assertRaisesRegex(ValueError, 'reconciliation'):
                exchange._rule_target_metadata(content, schema)

    def test_cached_hidden_difference_is_ignored_and_hidden_metadata_is_retained(self):
        import frappe
        from hero_integration import api

        document = {'name': 'Address', 'module': 'Contacts', 'custom': 0, 'istable': 0, 'issingle': 0, 'is_virtual': 0}
        stored = {'name': 'address-line2', 'fieldname': 'address_line2', 'fieldtype': 'Data',
                  'options': '', 'reqd': 0, 'read_only': 0, 'hidden': 1, 'permlevel': 0, 'is_virtual': 0}
        cached = frappe._dict({**stored, 'hidden': 0})
        snapshot = NS(name='snapshot', schema_fingerprint='fingerprint')
        storage = NS(_definition=NS(schema_snapshot='snapshot', direction='QUERY_TO_FRAPPE'))
        for changed in (None, 'reqd', 'read_only', 'permlevel', 'is_virtual', 'fieldtype', 'options'):
            field = frappe._dict(cached)
            if changed:
                field[changed] = 'different' if changed in {'fieldtype', 'options'} else 1
            fake = NS(db=NS(sql=Mock(side_effect=[[document], [stored], [], []])),
                      get_meta=lambda dt: NS(get=document.get, get_field=lambda name: field))
            with self.subTest(changed=changed), patch.object(exchange, 'frappe', fake), \
                 patch.object(api, '_validated_snapshot', return_value=(snapshot, {})):
                if changed:
                    with self.assertRaisesRegex(ValueError, 'stale'):
                        exchange._assignment_metadata(storage, [{'target': {'doctype': 'Address', 'field': 'address_line2'}}])
                else:
                    metadata = exchange._assignment_metadata(storage, [{'target': {'doctype': 'Address', 'field': 'address_line2'}}])
                    self.assertEqual(metadata['fields'][0]['hidden'], 0)
                    self.assertEqual(metadata['locked_metadata']['Address']['fields'][0]['hidden'], 1)

    def test_projection_matches_installed_flags_without_mutating_stored_metadata(self):
        field = {'name': 'native-field', 'fieldname': 'date_of_birth', 'reqd': 1, 'hidden': 0}
        setters = [{'name': 'dob-optional', 'doctype_or_field': 'DocField', 'field_name': 'date_of_birth', 'property': 'reqd', 'value': '0'}]
        doc, fields = exchange._project_locked_metadata({'name': 'Employee'}, [field], setters)
        self.assertEqual(fields[0]['reqd'], 0)
        self.assertEqual(field['reqd'], 1)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            exchange._project_locked_metadata(doc, [field], [*setters, *setters])
        with self.assertRaisesRegex(ValueError, 'numeric'):
            exchange._project_locked_metadata(doc, [field], [{**setters[0], 'value': 'unsafe'}])


class TestFilteredMappedUpdateExport(TestCase):
    def test_filtered_empty_export_ignores_legacy_and_foreign_context_before_uuid_locks(self):
        legacy = {'definition_id': 'legacy-not-a-uuid', 'mapping_json': json.dumps({'usage': 'SEND_TO_HERO'})}
        foreign = {'definition_id': 'foreign-not-a-uuid', 'new_definition_json': json.dumps({
            'source_instance': 'other-source', 'snapshot_name': 'snapshot', 'direction': 'FRAPPE_TO_MUTATION'}),
            'content_json': json.dumps(content())}
        def rows(doctype, **kwargs): return [foreign] if doctype == 'HERO Mapping Draft' else [legacy]
        fake = NS(get_all=rows, session=NS(user='Administrator'), local=NS(site='site'))
        with patch.object(exchange, 'frappe', fake), patch.object(exchange, 'FrappeMappingDraftStorage'), \
                patch.object(exchange, 'assert_portable_identity_schema'), patch.object(exchange, '_lock_context'), \
                patch.object(exchange, '_portable_snapshot', return_value=(NS(schema_fingerprint='a' * 64), {})), \
                patch.object(exchange, 'lock_identity_drafts', side_effect=AssertionError('No unrelated identity locks')):
            result = json.loads(exchange._export_portable('source-a', 'snapshot', 'FRAPPE_TO_MUTATION', usage='MAPPED_UPDATE_V2'))
        self.assertEqual(result['mappings'], [])
        self.assertEqual(result['schema_fingerprint'], 'a' * 64)
        with self.assertRaises(ValueError): exchange._parse_portable(json.dumps(result), 'FRAPPE_TO_MUTATION')

    def test_filter_cannot_widen_to_queries_or_arbitrary_mutation_usages(self):
        for direction, usage in [('QUERY_TO_FRAPPE', 'MAPPED_UPDATE_V2'), ('FRAPPE_TO_MUTATION', 'SEND_TO_HERO')]:
            with self.subTest(direction=direction, usage=usage), self.assertRaises(ValueError):
                exchange._export_portable('source-a', 'snapshot', direction, usage=usage)
