"""Configured Mutation groups drive inputs; failed proofs cannot reach transport."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from graphql import build_schema, introspection_from_schema

from hero_integration.services import mapped_send as ms
from hero_integration.services.change_detection import encode, field_hashes
from hero_integration.services.direct_mapping import MappingSpec
from hero_integration.services.graphql_contract import parse_mutation
from hero_integration.services.graphql_graph import analyze_snapshot

SCHEMA = introspection_from_schema(build_schema('''
type Query { contacts(first: Int, offset: Int): [Contact!]! }
type Contact { id: Int!, remarks: String, address: Address }
type Address { id: Int!, street: String }
input AddressInput { id: Int, street: String }
input CustomerInput { id: Int!, remarks: String, address: AddressInput }
type Mutation { update_contact(contact: CustomerInput!): Contact! }
'''))
DOCUMENT = 'mutation ConfiguredSend($input: CustomerInput!) { update_contact(contact: $input) { id } }'


def content():
    return {'usage': ms.USAGE, 'source': {'doctype': 'Customer', 'field': 'custom_notes'},
        'target': {'operation': 'update_contact', 'argument': 'contact', 'input_type': 'CustomerInput', 'field': 'remarks'},
        'transformation': 'Identity', 'send_options': {'read_service': 'READ-1', 'mode': 'UPDATE',
            'identity_argument': 'contact', 'identity_field': 'id', 'read_identity_path': 'id',
            'assignments': {'primary': {'resolver': 'FIELD', 'read_path': 'remarks', 'missing': 'ERROR', 'null': 'OMIT'}}}}


class _Doc(dict):
    doctype = 'Customer'
    name = 'C1'
    modified = '2026-11-02 12:00:00'


def context():
    rule = MappingSpec('HeroCustomers', 'remarks', 'Customer', 'custom_notes', 'string|null')
    link = NS(name='LINK-1', source_system='HERO', mapping_version='approved-baseline-version', source_type='customer', source_id='42', source_instance='source-a',
              target_doctype='Customer', target_name='C1', sync_status='Synchronized', source_field_hashes=encode(field_hashes({'custom_notes': 'Old'})),
              target_field_hashes=encode(field_hashes({'custom_notes': 'Old'})))
    schema_fingerprint = analyze_snapshot(SCHEMA).fingerprint
    return NS(document=_Doc(custom_notes='Expected'), content=ms.normalise_send_mapping(content(), SCHEMA),
        mapping=NS(name='MAP-1', operation_name='update_contact', mapping_revision=2),
        snapshot=NS(name='SNAP-1', snapshot_json=json.dumps(SCHEMA), schema_fingerprint=schema_fingerprint,
                    analysis_status='Validated'),
        link=link, baseline_links={}, proof=NS(source_instance='source-a', fingerprint='source-proof'), hero_id=42, related={},
        service=NS(query_document=DOCUMENT), contract=NS(contract_fingerprint='contract-proof', contract_revision=3),
        parsed=parse_mutation(DOCUMENT), read=NS(service=NS(name='READ-1', query_document='approved-query'),
        mapping=NS(name='QUERY-MAP-1'), contract=NS(target_operation='HeroCustomers', service_revision=4,
            mapping_revision=5)), rule=rule)


class TestSendProfileNormalization(TestCase):
    def test_configured_pair_is_not_limited_to_the_legacy_fixed_customer_fields(self):
        actual = ms.normalise_send_mapping(content(), SCHEMA)
        self.assertEqual(actual['target']['field'], 'remarks')
        self.assertEqual(ms.send_sources(actual), (('Customer', 'custom_notes'),))
        self.assertEqual(actual['target']['graphql_type'], 'String')

    def test_nested_additional_mapping_stays_in_one_group(self):
        value = content()
        identity = '53f327b2-f967-48b7-b6e4-cc44fa75e2b8'
        value['additional_mappings'] = [{'id': identity, 'source': {'doctype': 'Address', 'field': 'address_line1'},
            'target': {'operation': 'update_contact', 'argument': 'contact', 'input_type': 'CustomerInput',
                       'field': 'address.street'}, 'transformation': 'Trim'}]
        value['send_options']['related_identities'] = {'contact.address.id': {
            'doctype': 'Address', 'source_type': 'customer_address', 'read_path': 'address.id'}}
        value['send_options']['assignments'][identity] = {
            'resolver': 'FIELD', 'read_path': 'address.street', 'missing': 'ERROR', 'null': 'OMIT'}
        actual = ms.normalise_send_mapping(value, SCHEMA)
        self.assertEqual(actual['additional_mappings'][0]['target']['field'], 'address.street')
        self.assertEqual(len(tuple(ms.assignments(actual))), 2)

    def test_invalid_or_implicit_configuration_is_rejected(self):
        variants = []
        for path, val in [('usage', 'WRITEBACK'), ('additional_mappings', [None])]:
            value = content(); value[path] = val; variants.append(value)
        for key, val in [('read_service', ''), ('mode', 'CREATE'), ('identity_field', 'remarks')]:
            value = content(); value['send_options'][key] = val; variants.append(value)
        value = content(); value['target']['field'] = 'id'; variants.append(value)
        value = content(); value['target']['field'] = 'unknown'; variants.append(value)
        value = content(); value['send_options']['assignments'] = {}; variants.append(value)
        value = content(); value['send_options']['assignments']['primary']['resolver'] = 'USER_LINK'; variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ms.normalise_send_mapping(value, SCHEMA)

    def test_value_map_requires_explicit_unambiguous_conversion_pairs(self):
        value = content()
        setting = value['send_options']['assignments']['primary']
        setting.update(resolver='VALUE_MAP', map={'local': 'remote'})
        self.assertEqual(ms.normalise_send_mapping(value, SCHEMA)['send_options']['assignments']['primary']['map'], {'local': 'remote'})
        setting['map'] = {'a': 'same', 'b': 'same'}
        with self.assertRaises(ms.MappedSendError):
            ms.normalise_send_mapping(value, SCHEMA)


class _TransportFixture(TestCase):
    def setUp(self):
        self.ctx = context()
        self.statuses = []
        self.writes = []
        self.mutation = NS(execute_once=Mock(return_value={'update_contact': {'id': 42}}))
        self.frappe = NS(has_permission=Mock(), get_meta=lambda doctype: NS(get_field=lambda field: NS(label='Mapped notes',
            fieldtype='Small Text', hidden=0, is_virtual=0, permlevel=0)),
            db=NS(savepoint=Mock(), rollback=Mock(), set_value=lambda *a, **kw: self.writes.append((a, kw))),
            get_doc=lambda doctype, name, **kw: self.ctx.link if doctype == 'HERO Integration Link' else self.ctx.document)
        patches = [patch.object(ms, 'frappe', self.frappe), patch.object(ms, '_load_context', return_value=self.ctx),
            patch.object(ms, '_read_record', return_value={'id': 42, 'remarks': 'Old'}),
            patch('hero_integration.services.migration._mapping_plan_for_run', return_value=NS(definitions=(self.ctx.rule,))),
            patch('hero_integration.services.writeback_source.validate_source', return_value=NS(endpoint='endpoint', api_token='synthetic', timeout_seconds=1)),
            patch('hero_integration.api._mutation_execution_state', return_value={'mutation_execution_enabled': True}),
            patch('hero_integration.api._assert_mutation_execution_released'),
            patch('hero_integration.api._reserve_mutation', return_value=NS(correlation_id='AUDIT-1')),
            patch('hero_integration.api._finalize_mutation', side_effect=lambda audit, status, detail: self.statuses.append(status))]
        for item in patches:
            item.start(); self.addCleanup(item.stop)

    def preview(self):
        return ms.preview('Customer', 'C1', client=NS())

    def send(self, fingerprint):
        return ms.send('Customer', 'C1', fingerprint=fingerprint, confirmed=1,
                       client=NS(), mutation_client=self.mutation)



class TestMappedSendTransport(_TransportFixture):
    def test_configured_pair_drives_real_mutation_arguments_and_approved_service(self):
        fingerprint = self.preview()['fingerprint']
        with patch.object(ms, '_read_record', side_effect=[{'id': 42, 'remarks': 'Old'}, {'id': 42, 'remarks': 'Expected'}]):
            result = self.send(fingerprint)
        self.mutation.execute_once.assert_called_once_with(DOCUMENT, {'input': {'id': 42, 'remarks': 'Expected'}})
        self.assertEqual(result['status'], 'SENT')
        self.assertEqual(self.statuses, ['Succeeded'])
        self.assertEqual(len(self.writes), 1)

    def test_post_read_mismatch_never_acknowledges_or_advances_baselines(self):
        fingerprint = self.preview()['fingerprint']
        with self.assertRaises(ms.MappedSendError):
            self.send(fingerprint)
        self.assertEqual(self.statuses, ['Failed'])
        self.assertEqual(self.writes, [])
        self.mutation.execute_once.assert_called_once()

    def test_mapping_change_invalidates_the_preview_before_mutation(self):
        fingerprint = self.preview()['fingerprint']
        self.ctx.mapping.mapping_revision += 1
        with self.assertRaises(ms.MappedSendError):
            self.send(fingerprint)
        self.mutation.execute_once.assert_not_called()
        self.assertEqual(self.statuses, [])

    def test_remote_baseline_change_and_missing_baseline_block_transport(self):
        for baseline in ('Other', None):
            self.ctx.link.source_field_hashes = encode(field_hashes({'custom_notes': baseline})) if baseline else ''
            preview = self.preview()
            self.assertFalse(preview['can_send'])
            self.assertEqual(len(preview['conflicts']), 1)
            with self.assertRaises(ms.MappedSendError):
                self.send(preview['fingerprint'])
        self.mutation.execute_once.assert_not_called()

    def test_source_change_immediately_before_mutation_blocks_transport(self):
        fingerprint = self.preview()['fingerprint']
        config = NS(endpoint='endpoint', api_token='synthetic', timeout_seconds=1)
        with patch('hero_integration.services.writeback_source.validate_source',
                   side_effect=[config, ms.MappedSendError('Source changed')]), self.assertRaises(ms.MappedSendError):
            self.send(fingerprint)
        self.mutation.execute_once.assert_not_called()
        self.assertEqual(self.statuses, ['Failed'])

    def test_no_profile_cannot_fall_back_to_a_legacy_native_send(self):
        from hero_integration.services import customer_send, project_send, task_send
        for module, name in ((customer_send, 'Customer'), (project_send, 'Project'), (task_send, 'Task')):
            with patch.object(ms, 'preview', side_effect=ms.MappedSendError('No active profile')), \
                    patch.object(module, 'legacy_preview') as legacy, \
                    patch('hero_integration.services.project_delivery._access'), self.assertRaises(ValueError):
                module.preview('TEST-1', client=Mock())
            legacy.assert_not_called()


class TestNativeSendWrappers(TestCase):
    def test_public_preview_and_send_delegate_to_the_grouped_configured_runtime(self):
        from hero_integration.services import customer_send, project_send, task_send
        client, mutation = object(), object()
        for module, doctype in ((customer_send, 'Customer'), (task_send, 'Task')):
            with patch.object(ms, 'preview', return_value={'configured': True}) as preview, \
                    patch.object(ms, 'send', return_value={'status': 'SENT'}) as send, \
                    patch.object(module, 'legacy_preview') as legacy_preview, \
                    patch.object(module, 'legacy_send') as legacy_send:
                extra = {'phase_id': 7} if doctype == 'Project' else {}
                self.assertEqual(module.preview('DOC-1', client=client, **extra), {'configured': True})
                self.assertEqual(module.send('DOC-1', fingerprint='proof', confirmed=1,
                    client=client, mutation_client=mutation, **extra), {'status': 'SENT'})
                preview.assert_called_once_with(doctype, 'DOC-1', client=client, **extra)
                send.assert_called_once_with(doctype, 'DOC-1', fingerprint='proof', confirmed=1,
                    client=client, mutation_client=mutation, **extra)
                legacy_preview.assert_not_called(); legacy_send.assert_not_called()

    def test_missing_profile_rejects_before_client_construction(self):
        fake = NS(get_roles=lambda: ['HERO Writeback'], get_doc=lambda *args: _Doc(custom_notes='Expected'),
            has_permission=Mock(), get_all=lambda *args, **kwargs: [])
        with patch.object(ms, 'frappe', fake), \
                patch('hero_integration.client.settings.get_configuration', return_value=NS(source_instance='source-a')) as config, \
                patch('hero_integration.client.graphql.HeroGraphQLClient') as client, \
                self.assertRaisesRegex(ms.MappedSendError, 'Exactly one active Send'):
            ms.preview('Customer', 'C1')
        config.assert_called_once_with(fresh=True)
        client.assert_not_called()


class TestGroupedNestedSend(_TransportFixture):
    def test_grouped_nested_inputs_are_built_and_read_back_with_bound_related_identity(self):
        identity = '53f327b2-f967-48b7-b6e4-cc44fa75e2b8'
        value = content()
        value['additional_mappings'] = [{'id': identity, 'source': {'doctype': 'Customer', 'field': 'custom_street'},
            'target': {'operation': 'update_contact', 'argument': 'contact', 'input_type': 'CustomerInput',
                       'field': 'address.street'}, 'transformation': 'Identity'}]
        value['send_options']['assignments'][identity] = {
            'resolver': 'FIELD', 'read_path': 'address.street', 'missing': 'ERROR', 'null': 'OMIT'}
        value['send_options']['related_identities'] = {'contact.address.id': {
            'doctype': 'Address', 'source_type': 'customer_address', 'read_path': 'address.id'}}
        self.ctx.content = ms.normalise_send_mapping(value, SCHEMA)
        self.ctx.document['custom_street'] = 'New Street'
        self.ctx.document['customer_primary_address'] = 'A1'
        self.ctx.link.source_field_hashes = encode(field_hashes({'custom_notes': 'Old', 'custom_street': 'Old Street'}))
        self.ctx.link.target_field_hashes = self.ctx.link.source_field_hashes
        class Address(_Doc):
            doctype = 'Address'
            name = 'A1'
        self.ctx.related['Address'] = Address(links=[{'link_doctype': 'Customer', 'link_name': 'C1'}])
        address_rule = MappingSpec('HeroCustomers', 'address.street', 'Customer', 'custom_street', 'string|null')
        before = {'id': 42, 'remarks': 'Old', 'address': {'id': 9, 'street': 'Old Street'}}
        after = {'id': 42, 'remarks': 'Expected', 'address': {'id': 9, 'street': 'New Street'}}
        self.frappe.get_all = lambda *args, **kwargs: ['9']
        with patch('hero_integration.services.migration._mapping_plan_for_run',
                   return_value=NS(definitions=(self.ctx.rule, address_rule))), \
                patch.object(ms, '_read_record', return_value=before):
            fingerprint = self.preview()['fingerprint']
        with patch('hero_integration.services.migration._mapping_plan_for_run',
                   return_value=NS(definitions=(self.ctx.rule, address_rule))), \
                patch.object(ms, '_read_record', side_effect=[before, after]):
            self.send(fingerprint)
        self.mutation.execute_once.assert_called_once_with(DOCUMENT, {'input': {
            'id': 42, 'remarks': 'Expected', 'address': {'id': 9, 'street': 'New Street'}}})
        self.assertEqual(self.statuses, ['Succeeded'])

    def test_omitted_optional_address_does_not_block_an_independent_customer_field(self):
        identity = '7d0c1f7e-58a4-4c0e-9a3f-2c4d8f0b6a11'
        value = content()
        value['additional_mappings'] = [{'id': identity, 'source': {'doctype': 'Address', 'field': 'address_line1'},
            'target': {'operation': 'update_contact', 'argument': 'contact', 'input_type': 'CustomerInput',
                       'field': 'address.street'}, 'transformation': 'Identity'}]
        value['send_options']['assignments'][identity] = {
            'resolver': 'FIELD', 'read_path': 'address.street', 'missing': 'OMIT', 'null': 'OMIT'}
        value['send_options']['related_identities'] = {'contact.address.id': {
            'doctype': 'Address', 'source_type': 'customer_address', 'read_path': 'address.id'}}
        self.ctx.content = ms.normalise_send_mapping(value, SCHEMA)
        self.ctx.document['customer_primary_address'] = None  # this Customer has no address
        before = {'id': 42, 'remarks': 'Old', 'address': None}
        after = {'id': 42, 'remarks': 'Expected', 'address': None}
        with patch.object(ms, '_read_record', return_value=before):
            fingerprint = self.preview()['fingerprint']
        with patch.object(ms, '_read_record', side_effect=[before, after]):
            result = self.send(fingerprint)
        self.mutation.execute_once.assert_called_once_with(DOCUMENT, {'input': {'id': 42, 'remarks': 'Expected'}})
        self.assertEqual((result['status'], self.statuses), ('SENT', ['Succeeded']))

    def test_related_identity_is_selected_only_for_sent_nested_inputs(self):
        self.ctx.content['send_options']['related_identities'] = {'contact.address.id': {
            'doctype': 'Address', 'source_type': 'customer_address', 'read_path': 'address.id'}}
        self.assertEqual(ms._selected_related_identities(self.ctx, [('contact', 'remarks'), ('contact', 'id')]), {})
        self.assertEqual(list(ms._selected_related_identities(self.ctx, [('contact', 'address', 'street')])),
                         ['contact.address.id'])

    def test_related_address_identity_must_be_proven_by_pre_read(self):
        self.ctx.content['send_options']['related_identities'] = {'contact.address.id': {
            'doctype': 'Address', 'source_type': 'customer_address', 'read_path': 'address.id'}}
        class Address(_Doc):
            doctype = 'Address'
            name = 'A1'
        self.ctx.related['Address'] = Address(links=[{'link_doctype': 'Customer', 'link_name': 'C1'}])
        self.frappe.get_all = lambda *args, **kwargs: ['9']
        with self.assertRaises(ms.MappedSendError):
            ms._related_identity_values(self.ctx, {'id': 42, 'address': {'id': 10}})
        self.mutation.execute_once.assert_not_called()


class TestVerifiedPhaseApply(TestCase):
    def context(self):
        class Project(_Doc):
            doctype = 'Project'
            def set(self, field, value):
                self[field] = value
        old = Project(custom_phase='Old phase')
        current = Project(custom_phase='Old phase')
        current.save = Mock()
        profile = {'source': {'doctype': 'Project', 'field': 'custom_phase'},
            'send_options': {'assignments': {'primary': {'resolver': 'PHASE', 'read_path': 'status.step_id'}}}}
        ctx = NS(document=old, content=profile, link=NS(name='LINK-1'))
        change = {'id': 'primary', 'local': 'Old phase'}
        return ctx, change, current

    def test_phase_updates_only_the_exact_query_mapped_source_after_readback(self):
        ctx, change, current = self.context()
        fake = NS(get_doc=Mock(return_value=current), has_permission=Mock())
        with patch.object(ms, 'frappe', fake), patch.object(ms, '_baseline_remote', return_value='New phase') as canonical:
            ms._apply_phase_source(ctx, [change], {'status': {'step_id': 7}})
        fake.get_doc.assert_called_once_with('Project', 'C1', for_update=True)
        current.save.assert_called_once()
        self.assertEqual(dict(current), {'custom_phase': 'New phase'})
        self.assertEqual(change['local'], 'New phase')
        self.assertEqual(canonical.call_args.args[-1], 7)

    def test_changed_local_document_or_phase_does_not_overwrite_or_acknowledge(self):
        for modified, value in [('newer', 'Old phase'), ('2026-11-02 12:00:00', 'Changed locally')]:
            ctx, change, current = self.context()
            current.modified = modified
            current['custom_phase'] = value
            fake = NS(get_doc=Mock(return_value=current), has_permission=Mock())
            with patch.object(ms, 'frappe', fake), patch.object(ms, '_baseline_remote') as canonical, \
                    self.assertRaises(ms.MappedSendError):
                ms._apply_phase_source(ctx, [change], {'status': {'step_id': 7}})
            current.save.assert_not_called()
            canonical.assert_not_called()


class TestApprovedSendPagination(TestCase):
    def setUp(self):
        self.ctx = context()
        self.ctx.read.service.query_document = 'query Read($first:Int!,$offset:Int!){contacts(first:$first,offset:$offset){id remarks}}'
        self.ctx.read.contract = NS(target_operation='HeroCustomers', page_size=2, records_path='contacts',
            identity_path='id', variables={'first': '$page_size', 'offset': '$offset'}, materialized_request=None)
        self.frappe = NS(get_doc=lambda doctype, name: self.ctx.read.service if doctype == 'HERO GraphQL Service' else self.ctx.read.mapping)
        patches = [patch.object(ms, 'frappe', self.frappe),
            patch('hero_integration.services.writeback_source.validate_source'),
            patch('hero_integration.services.read_contract.from_documents', return_value=self.ctx.read.contract)]
        for item in patches:
            item.start(); self.addCleanup(item.stop)

    def test_bound_record_on_second_page_uses_only_approved_offset_variables(self):
        client = NS(query=Mock(side_effect=[{'contacts': [{'id': 1}, {'id': 2}]},
            {'contacts': [{'id': 42, 'remarks': 'Expected'}]}, {'contacts': []}]))
        result = ms._read_record(self.ctx, client)
        self.assertEqual(result['id'], 42)
        self.assertEqual([call.args for call in client.query.call_args_list], [
            (self.ctx.read.service.query_document, {'first': 2, 'offset': 0}),
            (self.ctx.read.service.query_document, {'first': 2, 'offset': 2}),
            (self.ctx.read.service.query_document, {'first': 2, 'offset': 3})])

    def test_missing_bound_record_and_repeated_identities_are_rejected(self):
        for pages in ([{'contacts': [{'id': 1}]}, {'contacts': []}],
                      [{'contacts': [{'id': 1}]}, {'contacts': [{'id': 1}]}]):
            with self.subTest(pages=pages), self.assertRaises(ms.MappedSendError):
                ms._read_record(self.ctx, NS(query=Mock(side_effect=pages)))

    def test_exhausting_the_bounded_read_is_never_treated_as_empty_success(self):
        client = NS(query=Mock(side_effect=lambda document, variables:
            {'contacts': [{'id': variables['offset'] + 1000}]}))
        with self.assertRaisesRegex(ms.MappedSendError, 'bounded page limit'):
            ms._read_record(self.ctx, client)
        self.assertEqual(client.query.call_count, 100)


class TestConfiguredProjectPhase(_TransportFixture):
    def test_nested_phase_mutation_reads_back_id_and_applies_exact_query_label_with_cas(self):
        schema = introspection_from_schema(build_schema('''
        type Query { project_matches(first:Int,offset:Int): [ProjectMatch!]! }
        type ProjectMatch { id:Int!, current_project_match_status:Status, type:ProjectType! }
        type ProjectType { project_status_steps:[Phase!]! }
        type Status { step_id:Int, step:Phase }
        type Phase { id:Int!, name:String!, is_active:Boolean! }
        input PhaseInput { step_id:Int }
        input ProjectMatchInput { id:Int!, current_project_match_status:PhaseInput }
        type Mutation { update_project_match(project_match:ProjectMatchInput!):ProjectMatch! }
        '''))
        profile = {'usage': ms.USAGE, 'source': {'doctype': 'Project', 'field': 'custom_phase'},
            'target': {'operation': 'update_project_match', 'argument': 'project_match',
                'input_type': 'ProjectMatchInput', 'field': 'current_project_match_status.step_id'},
            'transformation': 'Identity', 'send_options': {'read_service': 'READ-1', 'mode': 'UPDATE',
                'identity_argument': 'project_match', 'identity_field': 'id', 'read_identity_path': 'id',
                'assignments': {'primary': {'resolver': 'PHASE', 'read_path': 'current_project_match_status.step_id',
                    'query_path': 'current_project_match_status.step.name', 'missing': 'ERROR', 'null': 'OMIT',
                    'lookup': {'path': 'type.project_status_steps', 'key': 'id', 'value': 'name', 'active_path': 'is_active', 'active_value': True}}}}}
        document = 'mutation ConfiguredPhase($input:ProjectMatchInput!){update_project_match(project_match:$input){id}}'
        class Project(_Doc):
            doctype = 'Project'
            def set(self, field, value):
                self[field] = value
        self.ctx.document = Project(custom_phase='Old')
        current = Project(custom_phase='Old')
        current.save = Mock()
        self.frappe.get_doc = Mock(side_effect=lambda doctype, name, **kw:
            self.ctx.link if doctype == 'HERO Integration Link' else current)
        self.frappe.has_permission = Mock()
        self.ctx.content = ms.normalise_send_mapping(profile, schema)
        self.ctx.mapping.operation_name = 'update_project_match'
        self.ctx.snapshot.snapshot_json = json.dumps(schema)
        self.ctx.snapshot.schema_fingerprint = analyze_snapshot(schema).fingerprint
        self.ctx.service.query_document = document
        self.ctx.parsed = parse_mutation(document)
        self.ctx.read.contract.target_operation = 'HeroProjectMatches'
        self.ctx.link.source_field_hashes = encode(field_hashes({'custom_phase': 'Old'}))
        self.ctx.link.target_field_hashes = self.ctx.link.source_field_hashes
        rule = MappingSpec('HeroProjectMatches', 'current_project_match_status.step.name', 'Project', 'custom_phase', 'string|null')
        phases = [{'id': 1, 'name': 'Old', 'is_active': True}, {'id': 2, 'name': 'New', 'is_active': True},
                  {'id': 3, 'name': 'Inactive', 'is_active': False}]
        before = {'id': 42, 'current_project_match_status': {'step_id': 1, 'step': {'name': 'Old'}}, 'type': {'project_status_steps': phases}}
        after = {'id': 42, 'current_project_match_status': {'step_id': 2, 'step': {'name': 'New'}}, 'type': {'project_status_steps': phases}}
        self.mutation.execute_once.return_value = {'update_project_match': {'id': 42}}
        with patch('hero_integration.services.migration._mapping_plan_for_run', return_value=NS(definitions=(rule,))), \
                patch.object(ms, '_read_record', return_value=before):
            preview = ms.preview('Project', 'C1', phase_id=2, client=NS())
        self.assertEqual(preview['phases'], [{'id': 1, 'name': 'Old'}, {'id': 2, 'name': 'New'}])
        self.assertEqual(preview['hero']['phase_id'], 1)
        with patch('hero_integration.services.migration._mapping_plan_for_run', return_value=NS(definitions=(rule,))), \
                patch.object(ms, '_read_record', side_effect=[before, after]):
            ms.send('Project', 'C1', phase_id=2, fingerprint=preview['fingerprint'], confirmed=1,
                    client=NS(), mutation_client=self.mutation)
        self.mutation.execute_once.assert_called_once_with(document, {'input': {
            'id': 42, 'current_project_match_status': {'step_id': 2}}})
        self.assertEqual(dict(current), {'custom_phase': 'New'})
        current.save.assert_called_once()
        hashes = self.writes[-1][0][2]
        self.assertEqual(json.loads(hashes['source_field_hashes']), field_hashes({'custom_phase': 'New'}))
        self.assertEqual(json.loads(hashes['target_field_hashes']), field_hashes({'custom_phase': 'New'}))



class TestExplicitClearSemantics(TestCase):
    def test_empty_wire_clear_can_be_verified_as_null_only_when_configured(self):
        self.assertTrue(ms._equal_target({'resolver': 'FIELD', 'null': 'EMPTY_STRING'}, None, ''))
        self.assertFalse(ms._equal_target({'resolver': 'FIELD', 'null': 'EXPLICIT_NULL'}, None, ''))
        self.assertFalse(ms._equal_target({'resolver': 'FIELD', 'null': 'OMIT'}, None, ''))
        self.assertFalse(ms._equal_target({'resolver': 'FIELD', 'null': 'EMPTY_STRING'}, 'stored', ''))


class TestConfiguredTaskCompletion(_TransportFixture):
    def task_context(self, status, local_date='2026-11-02'):
        schema = introspection_from_schema(build_schema('''
        type Query { tasks(first:Int,offset:Int):[Task!]! }
        type Task { id:Int!, done_date:String }
        input TaskInput { id:Int!, done_date:String }
        type Mutation { update_task(task:TaskInput!):Task! }
        '''))
        profile = {'usage': ms.USAGE, 'source': {'doctype': 'Task', 'field': 'completed_on'},
            'target': {'operation': 'update_task', 'argument': 'task', 'input_type': 'TaskInput', 'field': 'done_date'},
            'transformation': 'Identity', 'send_options': {'read_service': 'READ-1', 'mode': 'UPDATE',
                'identity_argument': 'task', 'identity_field': 'id', 'read_identity_path': 'id',
                'assignments': {'primary': {'resolver': 'DATE_TIME', 'date_mode': 'DATE', 'read_path': 'done_date',
                    'missing': 'ERROR', 'null': 'OMIT',
                    'when': {'field': 'status', 'equals': 'Completed', 'otherwise': 'EMPTY_STRING'}}}}}
        class Task(_Doc):
            doctype = 'Task'
        self.ctx.document = Task(completed_on=local_date, status=status)
        self.ctx.content = ms.normalise_send_mapping(profile, schema)
        self.ctx.mapping.operation_name = 'update_task'
        self.ctx.snapshot.snapshot_json = json.dumps(schema)
        self.ctx.snapshot.schema_fingerprint = analyze_snapshot(schema).fingerprint
        document = 'mutation ConfiguredTask($input:TaskInput!){update_task(task:$input){id}}'
        self.ctx.service.query_document = document
        self.ctx.parsed = parse_mutation(document)
        self.ctx.read.contract.target_operation = 'HeroTasks'
        self.ctx.link.source_field_hashes = encode(field_hashes({'completed_on': '2026-11-01'}))
        self.ctx.link.target_field_hashes = self.ctx.link.source_field_hashes
        self.mutation.execute_once.return_value = {'update_task': {'id': 42}}
        self.frappe.get_doc = Mock(side_effect=lambda doctype, name, **kw:
            self.ctx.link if doctype == 'HERO Integration Link' else self.ctx.document)
        self.frappe.has_permission = Mock()
        return document

    def test_task_completion_uses_explicit_date_mapping_and_condition(self):
        document = self.task_context('Completed')
        before, after = {'id': 42, 'done_date': '2026-11-01T12:00:00+00:00'}, {'id': 42, 'done_date': '2026-11-02T00:00:00Z'}
        with patch('frappe.utils.get_system_timezone', return_value='UTC'), \
                patch.object(ms, '_baseline_remote', side_effect=lambda ctx, item, setting, observed, **kw: observed[:10] if observed else None), \
                patch.object(ms, '_read_record', return_value=before):
            fingerprint = ms.preview('Task', 'C1', client=NS())['fingerprint']
        with patch('frappe.utils.get_system_timezone', return_value='UTC'), \
                patch.object(ms, '_baseline_remote', side_effect=lambda ctx, item, setting, observed, **kw: observed[:10] if observed else None), \
                patch.object(ms, '_read_record', side_effect=[before, after]):
            ms.send('Task', 'C1', fingerprint=fingerprint, confirmed=1, client=NS(), mutation_client=self.mutation)
        self.mutation.execute_once.assert_called_once_with(document, {'input': {'id': 42, 'done_date': '2026-11-02T12:00:00+00:00'}})
        self.assertEqual(self.statuses, ['Succeeded'])
        self.assertIn(('Task', 'status'), ms.send_sources(self.ctx.content))

    def test_reopened_task_emits_only_configured_empty_clear_and_verifies_null(self):
        document = self.task_context('Open', '2026-11-01')
        before, after = {'id': 42, 'done_date': '2026-11-01T12:00:00Z'}, {'id': 42, 'done_date': None}
        with patch.object(ms, '_baseline_remote', side_effect=lambda ctx, item, setting, observed, **kw: observed[:10] if observed else None), \
                patch.object(ms, '_read_record', return_value=before):
            fingerprint = ms.preview('Task', 'C1', client=NS())['fingerprint']
        with patch.object(ms, '_baseline_remote', side_effect=lambda ctx, item, setting, observed, **kw: observed[:10] if observed else None), \
                patch.object(ms, '_read_record', side_effect=[before, after]):
            ms.send('Task', 'C1', fingerprint=fingerprint, confirmed=1, client=NS(), mutation_client=self.mutation)
        self.mutation.execute_once.assert_called_once_with(document, {'input': {'id': 42, 'done_date': ''}})
        self.assertEqual(self.statuses, ['Succeeded'])

    def test_condition_changes_after_preview_and_during_transport_cannot_acknowledge(self):
        self.task_context('Open', '2026-11-01')
        before = {'id': 42, 'done_date': '2026-11-01T12:00:00Z'}
        canonical = lambda ctx, item, setting, observed, **kw: observed[:10] if observed else None
        with patch.object(ms, '_baseline_remote', side_effect=canonical), patch.object(ms, '_read_record', return_value=before):
            fingerprint = ms.preview('Task', 'C1', client=NS())['fingerprint']
            self.ctx.document['status'] = 'Cancelled'
            with self.assertRaises(ms.MappedSendError):
                ms.send('Task', 'C1', fingerprint=fingerprint, confirmed=1, client=NS(), mutation_client=self.mutation)
        self.mutation.execute_once.assert_not_called()
        self.ctx.document['status'] = 'Open'
        with patch.object(ms, '_baseline_remote', side_effect=canonical), patch.object(ms, '_read_record', return_value=before):
            fingerprint = ms.preview('Task', 'C1', client=NS())['fingerprint']
        def changed_status():
            self.ctx.document['status'] = 'Cancelled'
            return {'id': 42, 'done_date': None}
        reads = []
        def read(*args):
            reads.append(1)
            return before if len(reads) == 1 else changed_status()
        with patch.object(ms, '_baseline_remote', side_effect=canonical), \
                patch.object(ms, '_read_record', side_effect=read), self.assertRaises(ms.MappedSendError):
            ms.send('Task', 'C1', fingerprint=fingerprint, confirmed=1, client=NS(), mutation_client=self.mutation)
        self.mutation.execute_once.assert_called_once()
        self.assertEqual(self.statuses, ['Failed'])
        self.assertEqual(self.writes, [])


class TestAcknowledgmentTransaction(_TransportFixture):
    def test_unrelated_concurrent_baselines_are_preserved(self):
        fingerprint = self.preview()['fingerprint']
        current = deepcopy(self.ctx.link)
        current.source_field_hashes = encode(field_hashes({'custom_notes': 'Old', 'other': 'New remote'}))
        current.target_field_hashes = encode(field_hashes({'custom_notes': 'Old', 'other': 'New local'}))
        self.frappe.get_doc = lambda *args, **kwargs: current
        with patch.object(ms, '_read_record', side_effect=[{'id': 42, 'remarks': 'Old'}, {'id': 42, 'remarks': 'Expected'}]):
            self.send(fingerprint)
        values = self.writes[0][0][2]
        self.assertEqual(json.loads(values['source_field_hashes'])['other'], field_hashes({'other': 'New remote'})['other'])
        self.assertEqual(json.loads(values['target_field_hashes'])['other'], field_hashes({'other': 'New local'})['other'])

    def test_sent_baseline_cas_failure_rolls_back_before_failed_audit_finalization(self):
        fingerprint = self.preview()['fingerprint']
        current = deepcopy(self.ctx.link)
        current.source_field_hashes = encode(field_hashes({'custom_notes': 'Changed concurrently'}))
        self.frappe.get_doc = lambda *args, **kwargs: current
        events = []
        self.frappe.db.rollback.side_effect = lambda: events.append('rollback')
        with patch.object(ms, '_read_record', side_effect=[{'id': 42, 'remarks': 'Old'}, {'id': 42, 'remarks': 'Expected'}]), \
                patch('hero_integration.api._finalize_mutation', side_effect=lambda audit, status, detail: events.append(status)), \
                self.assertRaises(ms.MappedSendError):
            self.send(fingerprint)
        self.assertEqual(events, ['rollback', 'Failed'])
        self.assertEqual(self.writes, [])
        self.mutation.execute_once.assert_called_once()

    def test_full_rollback_discards_phase_callbacks_before_failed_audit_commit(self):
        from frappe.utils import CallbackManager
        fingerprint = self.preview()['fingerprint']
        callbacks = CallbackManager()
        delivered, events = [], []
        def rollback():
            events.append('rollback')
            callbacks.reset()
        self.frappe.db.rollback.side_effect = rollback
        def apply(*args):
            callbacks.add(lambda: delivered.append('phase event'))
            raise ms.MappedSendError('Local phase validation failed')
        def finalize(audit, status, detail):
            events.append(status)
            callbacks.run()
        with patch.object(ms, '_read_record', side_effect=[{'id': 42, 'remarks': 'Old'}, {'id': 42, 'remarks': 'Expected'}]), \
                patch.object(ms, '_apply_phase_source', side_effect=apply), \
                patch('hero_integration.api._finalize_mutation', side_effect=finalize), self.assertRaises(ms.MappedSendError):
            self.send(fingerprint)
        self.assertEqual(events, ['rollback', 'Failed'])
        self.assertEqual(delivered, [])
        self.assertEqual(self.writes, [])


class TestMappedTaskAssignee(_TransportFixture):
    def test_current_todo_assignee_uses_acknowledged_query_user_baseline(self):
        import sys
        schema = introspection_from_schema(build_schema('''
        type Query { tasks(first:Int,offset:Int):[Task!]! }
        type Task { id:Int!, title:String, target_user_id:Int }
        input TaskInput { id:Int!, title:String, target_user_id:Int }
        type Mutation { update_task(task:TaskInput!):Task! }
        '''))
        identity = '53f327b2-f967-48b7-b6e4-cc44fa75e2b8'
        profile = {'usage': ms.USAGE, 'source': {'doctype': 'Task', 'field': 'subject'},
            'target': {'operation': 'update_task', 'argument': 'task', 'input_type': 'TaskInput', 'field': 'title'},
            'transformation': 'Identity', 'additional_mappings': [{'id': identity,
                'source': {'doctype': 'ToDo', 'field': 'allocated_to'},
                'target': {'operation': 'update_task', 'argument': 'task', 'input_type': 'TaskInput', 'field': 'target_user_id'},
                'transformation': 'Identity'}],
            'send_options': {'read_service': 'READ-1', 'mode': 'UPDATE', 'identity_argument': 'task',
                'identity_field': 'id', 'read_identity_path': 'id', 'assignments': {
                    'primary': {'resolver': 'FIELD', 'read_path': 'title', 'missing': 'ERROR', 'null': 'OMIT'},
                    identity: {'resolver': 'USER_LINK', 'read_path': 'target_user_id', 'missing': 'OMIT', 'null': 'OMIT',
                        'lookup': {'service': 'USER-LOOKUP', 'key': 'user.email', 'value': 'user.id'}}}}}
        class Task(_Doc):
            doctype = 'Task'
        class ToDo(_Doc):
            doctype = 'ToDo'
            name = 'TODO-1'
        self.ctx.document = Task(subject='Expected')
        self.ctx.related['ToDo'] = ToDo(allocated_to='new@example.invalid')
        self.ctx.content = ms.normalise_send_mapping(profile, schema)
        self.ctx.mapping.operation_name = 'update_task'
        self.ctx.snapshot.snapshot_json = json.dumps(schema)
        self.ctx.snapshot.schema_fingerprint = analyze_snapshot(schema).fingerprint
        document = 'mutation ConfiguredUser($input:TaskInput!){update_task(task:$input){id}}'
        self.ctx.service.query_document = document
        self.ctx.parsed = parse_mutation(document)
        self.ctx.read.contract.target_operation = 'HeroTasks'
        self.ctx.link.source_field_hashes = encode(field_hashes({'ToDo.allocated_to': 'old@example.invalid'}))
        self.ctx.link.target_field_hashes = self.ctx.link.source_field_hashes
        rules = (MappingSpec('HeroTasks', 'title', 'Task', 'subject', 'string|null'),
            MappingSpec('HeroTasks', 'target_user_id', 'ToDo', 'allocated_to', 'int|null',
                        transformation='Link Map', definition_id='user-rule'))
        options = {'transform_options': {'user-rule': {'map': {'7': 'old@example.invalid', '8': 'new@example.invalid'}}}}
        def transform(spec, value, all_options, *, source_instance=None):
            self.assertEqual(source_instance, self.ctx.proof.source_instance)
            self.assertIs(all_options, options)
            return all_options['transform_options'][spec.definition_id]['map'][str(value)] if spec.transformation == 'Link Map' else value
        user = NS(name='new@example.invalid', get=lambda field: 'new@example.invalid' if field == 'email' else None)
        self.frappe.get_doc = lambda doctype, name, **kw: self.ctx.link if doctype == 'HERO Integration Link' else user
        self.frappe.has_permission = Mock()
        self.mutation.execute_once.return_value = {'update_task': {'id': 42}}
        before, after = {'id': 42, 'title': 'Expected', 'target_user_id': 7}, {'id': 42, 'title': 'Expected', 'target_user_id': 8}
        with patch.dict(sys.modules, {'hero_integration.services.query_mapping': NS(transform=transform)}), \
                patch('hero_integration.services.migration._mapping_plan_for_run', return_value=NS(definitions=rules, adapter_options=options)), \
                patch.object(ms, '_lookup', return_value=[('new@example.invalid', 8)]), \
                patch.object(ms, '_read_record', return_value=before):
            fingerprint = ms.preview('Task', 'C1', client=NS())['fingerprint']
        with patch.dict(sys.modules, {'hero_integration.services.query_mapping': NS(transform=transform)}), \
                patch('hero_integration.services.migration._mapping_plan_for_run', return_value=NS(definitions=rules, adapter_options=options)), \
                patch.object(ms, '_lookup', return_value=[('new@example.invalid', 8)]), \
                patch.object(ms, '_read_record', side_effect=[before, after]):
            ms.send('Task', 'C1', fingerprint=fingerprint, confirmed=1, client=NS(), mutation_client=self.mutation)
        self.mutation.execute_once.assert_called_once_with(document, {'input': {'id': 42, 'target_user_id': 8}})
        values = self.writes[-1][0][2]
        expected = field_hashes({'ToDo.allocated_to': 'new@example.invalid'})
        self.assertEqual(json.loads(values['source_field_hashes']), expected)
        self.assertEqual(json.loads(values['target_field_hashes']), expected)

    def test_absent_optional_todo_source_is_omitted_but_multiple_assignments_block(self):
        ctx = self.ctx
        class Task(_Doc):
            doctype = 'Task'
        ctx.document = Task(subject='Expected')
        source = {'source': {'doctype': 'ToDo', 'field': 'allocated_to'}}
        setting = {'resolver': 'USER_LINK', 'missing': 'OMIT'}
        self.frappe.get_all = lambda *a, **kw: []
        self.assertIs(ms._resolve(ctx, source, setting, {}, NS(), None), ms._MISSING)
        self.frappe.get_all = lambda *a, **kw: ['TODO-1', 'TODO-2']
        with self.assertRaises(ms.MappedSendError):
            ms._resolve(ctx, source, setting, {}, NS(), None)


class TestTemporalMutationOptIn(TestCase):
    def snapshot(self, schema):
        from hero_integration.services.mapping_contract_v1 import SnapshotAdapter, StoredSnapshot
        return SnapshotAdapter().adapt(StoredSnapshot('SNAP-1', schema, analyze_snapshot(schema).fingerprint, 'test-source'))

    def profile(self):
        value = content()
        value['source'] = {'doctype': 'Task', 'field': 'completed_on'}
        value['target'] = {'operation': 'update_task', 'argument': 'task', 'input_type': 'TaskInput', 'field': 'done_date'}
        value['send_options']['identity_argument'] = 'task'
        value['send_options']['assignments']['primary'] = {
            'resolver': 'DATE_TIME', 'date_mode': 'DATE', 'read_path': 'done_date', 'missing': 'ERROR', 'null': 'EMPTY_STRING'}
        return value

    def schema(self):
        return introspection_from_schema(build_schema('''
        scalar DateTime
        type Query { tasks: [Task!]! }
        type Task { id:Int!, done_date:DateTime }
        input TaskInput { id:Int, done_date:DateTime }
        type Mutation { update_task(task:TaskInput):Task }
        '''))

    def assert_temporal_contract(self, schema):
        from hero_integration.services.frappe_writeback import normalise_draft_mapping
        from hero_integration.services.mapping_contract_v1 import MutationAdapter, MutationDraft
        from hero_integration.services.mapping_contract_v1 import MappingContractError
        profile = self.profile()
        normalized = ms.normalise_send_mapping(profile, schema)
        self.assertEqual(normalized['target']['graphql_type'], 'DateTime')
        single = {key: profile[key] for key in ('source', 'target', 'transformation')}
        with self.assertRaises(ValueError):
            normalise_draft_mapping(single, schema)  # Existing field/Draft contract stays closed.
        snapshot = self.snapshot(schema)
        draft = MutationDraft('update_task', (('task', 'id'), ('task', 'done_date')), 'test-source', 1)
        values = {'task.id': 42, 'task.done_date': '2026-11-02T12:00:00+00:00'}
        with self.assertRaises(MappingContractError):
            MutationAdapter().construct_preview_values(snapshot, draft, values)
        actual = MutationAdapter().construct_preview_values(snapshot, draft, values, temporal=True)
        self.assertEqual(actual['variables']['task']['done_date'], values['task.done_date'])
        values['task.done_date'] = ''
        self.assertEqual(MutationAdapter().construct_preview_values(snapshot, draft, values, temporal=True)['variables']['task']['done_date'], '')
        for invalid in (123, {}, 'not-a-date', '2026-11-02T12:00:00'):
            values['task.done_date'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(MappingContractError):
                MutationAdapter().construct_preview_values(snapshot, draft, values, temporal=True)

    def test_datetime_is_opt_in_and_typed_without_widening_existing_drafts(self):
        self.assert_temporal_contract(self.schema())

    def test_actual_saved_hero_schema_when_explicitly_supplied(self):
        import os
        from pathlib import Path
        source = os.environ.get('HERO_REFERENCE_SCHEMA')
        if not source:
            self.skipTest('requires HERO_REFERENCE_SCHEMA pointing to a read-only saved schema')
        schema = json.loads(Path(source).read_text())
        schema = schema.get('data', schema)
        self.assertGreater(len(schema.get('__schema', schema)['types']), 100)
        self.assert_temporal_contract(schema)


class TestFrappeCheckboxSources(_TransportFixture):
    def test_zero_and_one_are_resolved_as_typed_booleans(self):
        self.frappe.get_meta = lambda doctype: NS(get_field=lambda field:
            NS(fieldtype='Check', hidden=0, is_virtual=0, permlevel=0))
        assignment = {'source': {'doctype': 'Customer', 'field': 'custom_flag'},
            'target': {'graphql_type': 'Boolean'}, 'transformation': 'Identity'}
        setting = {'resolver': 'FIELD', 'missing': 'ERROR', 'null': 'OMIT'}
        for stored, expected in ((0, False), (1, True)):
            self.ctx.document['custom_flag'] = stored
            actual = ms._resolve(self.ctx, assignment, setting, {}, NS(), None)
            self.assertIs(actual, expected)


class TestTemporalScalarBoundaries(TestCase):
    def test_date_and_datetime_validation_is_narrow_and_opt_in(self):
        from hero_integration.graphql_protocol.graphql_argument_values import _validate, GraphQLArgumentValueError
        from hero_integration.graphql_protocol.schema import GraphQLTypeRef
        date_ref = GraphQLTypeRef('SCALAR', name='Date')
        datetime_ref = GraphQLTypeRef('SCALAR', name='DateTime')
        other_ref = GraphQLTypeRef('SCALAR', name='Money')
        with self.assertRaises(GraphQLArgumentValueError):
            _validate('2026-11-02', date_ref, 'date', strict_scalars=True)
        self.assertEqual(_validate('2026-11-02', date_ref, 'date', strict_scalars=True, temporal=True), '2026-11-02')
        for value, ref in [('2026-11-02T12:00:00Z', date_ref), ('2026-11-02', datetime_ref), ('10.00', other_ref),
                           ('', GraphQLTypeRef('NON_NULL', of_type=datetime_ref))]:
            with self.subTest(value=value), self.assertRaises(GraphQLArgumentValueError):
                _validate(value, ref, 'date', strict_scalars=True, temporal=True)

    def test_date_readback_accepts_declared_date_shape_and_rejects_naive_datetime(self):
        settings = {'resolver': 'DATE_TIME', 'date_mode': 'DATE', 'null': 'OMIT'}
        with patch('frappe.utils.get_system_timezone', return_value='UTC'):
            self.assertTrue(ms._equal_target(settings, '2026-11-02', '2026-11-02'))
            self.assertTrue(ms._equal_target(settings, '2026-11-02', '2026-11-02T12:00:00Z'))
            self.assertFalse(ms._equal_target(settings, '2026-11-02T12:00:00', '2026-11-02T12:00:00Z'))


class TestMutationIdentityNamespaces(TestCase):
    def test_same_numeric_id_in_different_schema_type_cannot_authorize_send(self):
        for root_type, mutation_type, doctype, query_root, argument, input_type in (
                ('Customer', 'Contact', 'Customer', 'contacts', 'contact', 'CustomerInput'),
                ('Project', 'ProjectMatch', 'Project', 'project_matches', 'project_match', 'ProjectMatchInput')):
            schema = introspection_from_schema(build_schema(f'''
            type Query {{ {query_root}: [{root_type}!]! }}
            type {root_type} {{ id:Int!, remarks:String }}
            type {mutation_type} {{ id:Int!, remarks:String }}
            input {input_type} {{ id:Int, remarks:String }}
            type Mutation {{ update({argument}:{input_type}):{mutation_type} }}
            '''))
            value = content()
            value['source']['doctype'] = doctype
            value['target'].update(operation='update', argument=argument, input_type=input_type)
            value['send_options']['identity_argument'] = argument
            with self.subTest(doctype=doctype), self.assertRaisesRegex(ms.MappedSendError, 'different object type'):
                ms.normalise_send_mapping(value, schema)


class TestExplicitLookupMissingPolicy(TestCase):
    def test_optional_partner_user_is_skipped_only_when_configured(self):
        ctx = context()
        setting = {'resolver': 'USER_LINK', 'lookup': {'service': 'USERS', 'key': 'user.email', 'value': 'user.id'}}
        rows = [{'user': None}, {'user': {'email': 'person@example.invalid', 'id': 7}}]
        with patch.object(ms, '_validate_user_lookup_namespace'), patch.object(ms, '_load_read'), patch.object(ms, '_read_rows', return_value=rows):
            with self.assertRaises(ms.MappedSendError):
                ms._lookup(ctx, setting, {}, NS())
            setting['lookup']['missing'] = 'OMIT'
            self.assertEqual(ms._lookup(ctx, setting, {}, NS()), [('person@example.invalid', 7)])

    def test_user_lookup_cannot_alias_two_local_users_to_the_same_hero_identity(self):
        setting = {'resolver': 'USER_LINK', 'lookup': {'service': 'USERS', 'key': 'user.email', 'value': 'user.id'}}
        rows = [{'user': {'email': 'a@example.invalid', 'id': 7}}, {'user': {'email': 'b@example.invalid', 'id': 7}}]
        with patch.object(ms, '_validate_user_lookup_namespace'), patch.object(ms, '_load_read'), patch.object(ms, '_read_rows', return_value=rows), self.assertRaises(ms.MappedSendError):
            ms._lookup(context(), setting, {}, NS())


class TestNativeReferenceNamespaces(TestCase):
    def test_project_and_user_resolvers_cannot_supply_a_different_numeric_foreign_key(self):
        schema = introspection_from_schema(build_schema('''
        type Query { tasks:[Task!]! }
        type Task { id:Int!, author_user_id:Int, target_user_id:Int, target_project_match_id:Int }
        input TaskInput { id:Int, author_user_id:Int, target_user_id:Int, target_project_match_id:Int }
        type Mutation { update_task(task:TaskInput):Task }
        '''))
        for resolver, target in [('PROJECT_LINK', 'author_user_id'), ('USER_LINK', 'target_project_match_id')]:
            value = content()
            value['source'] = {'doctype': 'Task', 'field': 'project'}
            value['target'] = {'operation': 'update_task', 'argument': 'task', 'input_type': 'TaskInput', 'field': target}
            value['send_options']['identity_argument'] = 'task'
            value['send_options']['assignments']['primary'].update(resolver=resolver, read_path=target)
            with self.subTest(resolver=resolver), self.assertRaisesRegex(ms.MappedSendError, 'identity resolver|reference namespace'):
                ms.normalise_send_mapping(value, schema)


CREATE_SCHEMA = introspection_from_schema(build_schema('''
scalar DateTime
type Query { tasks(first:Int,offset:Int):[Task!]! }
type Task { id:Int!, title:String, target_project_match_id:Int, due_date:DateTime }
input TaskInput { id:Int, title:String, target_project_match_id:Int, due_date:DateTime, author_user_id:Int }
type Mutation { update_task(task:TaskInput!):Task! }
'''))
PROJECT_ASSIGNMENT = '53f327b2-f967-48b7-b6e4-cc44fa75e2b8'


def create_content():
    value = content()
    value.update(source={'doctype': 'Task', 'field': 'subject'},
        target={'operation': 'update_task', 'argument': 'task', 'input_type': 'TaskInput', 'field': 'title'},
        additional_mappings=[{'id': PROJECT_ASSIGNMENT, 'source': {'doctype': 'Task', 'field': 'project'},
            'target': {'operation': 'update_task', 'argument': 'task', 'input_type': 'TaskInput',
                       'field': 'target_project_match_id'}, 'transformation': 'Identity'}])
    value['send_options'].update(mode='CREATE', identity_argument='task', assignments={
        'primary': {'resolver': 'FIELD', 'read_path': 'title', 'missing': 'ERROR', 'null': 'OMIT'},
        PROJECT_ASSIGNMENT: {'resolver': 'PROJECT_LINK', 'read_path': 'target_project_match_id',
                             'missing': 'ERROR', 'null': 'OMIT'}})
    return value


class TestTaskCreateNormalization(TestCase):
    def test_create_requires_explicit_task_title_and_reviewed_project_identity(self):
        self.assertEqual(ms.normalise_send_mapping(create_content(), CREATE_SCHEMA)['send_options']['mode'], 'CREATE')
        for field, resolver in [('target_project_match_id', 'FIELD'), ('author_user_id', 'FIELD')]:
            profile = create_content()
            profile['additional_mappings'][0]['target']['field'] = field
            profile['send_options']['assignments'][PROJECT_ASSIGNMENT]['resolver'] = resolver
            with self.subTest(field=field), self.assertRaises(ms.MappedSendError):
                ms.normalise_send_mapping(profile, CREATE_SCHEMA)

    def test_create_compiles_without_id_using_real_schema_when_supplied(self):
        import os
        from pathlib import Path
        from hero_integration.services.mapping_contract_v1 import MutationAdapter, MutationDraft
        schemas = [CREATE_SCHEMA]
        if os.environ.get('HERO_REFERENCE_SCHEMA'):
            schema = json.loads(Path(os.environ['HERO_REFERENCE_SCHEMA']).read_text())
            schemas.append(schema.get('data', schema))
        for schema in schemas:
            with self.subTest(real=schema is not CREATE_SCHEMA):
                ms.normalise_send_mapping(create_content(), schema)
                snapshot = TestTemporalMutationOptIn().snapshot(schema)
                draft = MutationDraft('update_task', (('task', 'title'), ('task', 'target_project_match_id'),
                                      ('task', 'due_date')), 'test-source', 1)
                result = MutationAdapter().construct_preview_values(snapshot, draft,
                    {'task.title': 'Synthetic test task', 'task.target_project_match_id': 17,
                     'task.due_date': '2026-11-02T12:00:00+00:00'}, temporal=True)
                self.assertNotIn('id', result['variables']['task'])


class TestTaskCreate(_TransportFixture):
    def setUp(self):
        super().setUp()
        class Task(_Doc):
            doctype = 'Task'
        self.ctx.document = Task(subject='Synthetic test task', project='PROJECT-TEST')
        self.ctx.hero_id, self.ctx.link = None, None
        self.ctx.content = ms.normalise_send_mapping(create_content(), CREATE_SCHEMA)
        self.ctx.mapping.operation_name = 'update_task'
        self.ctx.snapshot.snapshot_json = json.dumps(CREATE_SCHEMA)
        self.ctx.snapshot.schema_fingerprint = analyze_snapshot(CREATE_SCHEMA).fingerprint
        self.ctx.service.query_document = 'mutation ConfiguredCreate($input:TaskInput!){update_task(task:$input){id}}'
        self.ctx.parsed = parse_mutation(self.ctx.service.query_document)
        self.ctx.read.contract.target_operation = 'HeroTasks'
        self.ctx.query_plan = NS(definitions=(MappingSpec('HeroTasks', 'title', 'Task', 'subject', 'string|null'),
            MappingSpec('HeroTasks', 'target_project_match_id', 'Task', 'project', 'int|null',
                        purpose='Project Relation', transformation='HERO Project Link')), mapping_version='approved-query-version')
        self.audit_rows, self.bindings, self.links = [], [], []
        self.created = []
        self.frappe.local = NS(site='synthetic-test-site')
        self.frappe.utils = NS(now_datetime=lambda: '2026-11-02 12:00:00')
        self.frappe.get_all = self.get_all
        def get_doc(doctype, name=None, **kwargs):
            if isinstance(doctype, dict):
                self.created.append(doctype)
                return NS(insert=lambda **kwargs: NS(name='NEW-LINK'))
            return self.ctx.document if doctype == 'Task' else NS(doctype='Project', name='PROJECT-TEST')
        self.frappe.get_doc = get_doc
        self.mutation.execute_once.return_value = {'update_task': {'id': 9001}}
        self.after = {'id': 9001, 'title': 'Synthetic test task', 'target_project_match_id': 17}
        item = patch('hero_integration.services.migration._project_name_for_source', return_value='PROJECT-TEST')
        item.start(); self.addCleanup(item.stop)

    def get_all(self, doctype, **kwargs):
        if doctype == 'HERO Mutation Audit': return self.audit_rows
        if doctype == 'HERO Object Binding': return self.bindings
        if doctype == 'HERO Integration Link':
            return [17] if kwargs.get('filters', {}).get('source_type') == 'project' else self.links
        return []

    def preview(self):
        return ms.preview('Task', 'C1', client=NS())

    def send(self, fingerprint):
        return ms.send('Task', 'C1', fingerprint=fingerprint, confirmed=1, client=NS(), mutation_client=self.mutation)

    def test_create_reads_back_before_linking_and_uses_approved_query_version(self):
        preview = self.preview()
        self.assertTrue(preview['create'])
        self.assertTrue(preview['can_send'])
        with patch.object(ms, '_read_record', return_value=self.after):
            result = self.send(preview['fingerprint'])
        self.mutation.execute_once.assert_called_once_with(self.ctx.service.query_document,
            {'input': {'title': 'Synthetic test task', 'target_project_match_id': 17}})
        self.assertEqual(result['hero_id'], 9001)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.created[0]['mapping_version'], 'approved-query-version')
        self.assertEqual(json.loads(self.created[0]['source_field_hashes']), field_hashes(
            {'subject': 'Synthetic test task', 'project': 'PROJECT-TEST'}))
        self.assertEqual(self.statuses, ['Succeeded'])

    def test_prior_uncertain_create_is_durable_across_local_edits(self):
        self.audit_rows = [NS(correlation_id='OLD', status='Failed', detail='Uncertain remote result')]
        self.ctx.document['subject'] = 'Edited again'
        with self.assertRaisesRegex(ms.MappedSendError, 'manual reconciliation'):
            self.send(self.preview()['fingerprint'])
        self.mutation.execute_once.assert_not_called()
        self.assertEqual(self.created, [])

    def test_timeout_and_bad_readback_never_link_or_permit_automatic_retry(self):
        fingerprint = self.preview()['fingerprint']
        with patch('hero_integration.api._finalize_mutation', side_effect=lambda audit, status, detail:
                   self.audit_rows.append(NS(correlation_id=audit.correlation_id, status=status, detail=detail))), \
                patch.object(ms, '_read_record', return_value={**self.after, 'title': 'Ignored'}), \
                self.assertRaises(ms.MappedSendError):
            self.send(fingerprint)
        self.assertEqual(self.created, [])
        self.assertNotEqual(self.audit_rows[-1].detail, ms._CREATE_PRETRANSPORT_FAILURE)
        with self.assertRaises(ms.MappedSendError):
            ms._assert_create_available(self.ctx)

    def test_stale_unbound_context_is_rechecked_after_reservation(self):
        fingerprint = self.preview()['fingerprint']
        self.bindings = ['ALREADY-BOUND']
        with self.assertRaises(ms.MappedSendError):
            self.send(fingerprint)
        self.mutation.execute_once.assert_not_called()
        self.assertEqual(self.statuses, ['Failed'])

    def test_local_change_after_external_create_rolls_back_and_remains_latched(self):
        fingerprint = self.preview()['fingerprint']
        def readback(*args):
            self.ctx.document.modified = '2026-11-02 13:00:00'
            return self.after
        # Preserve the preview version; the live document is a different Frappe instance.
        live = deepcopy(self.ctx.document)
        live.modified = '2026-11-02 13:00:00'
        original = self.frappe.get_doc
        self.frappe.get_doc = lambda dt, name=None, **kw: live if kw.get('for_update') and dt == 'Task' else original(dt, name, **kw)
        with patch.object(ms, '_read_record', return_value=self.after), self.assertRaises(ms.MappedSendError):
            self.send(fingerprint)
        self.frappe.db.rollback.assert_called_once()
        self.assertEqual(self.created, [])


    def test_known_pretransport_failure_alone_allows_another_reviewed_attempt(self):
        self.audit_rows = [NS(correlation_id='STOPPED', status='Failed', detail=ms._CREATE_PRETRANSPORT_FAILURE)]
        ms._assert_create_available(self.ctx)
        self.audit_rows.append(NS(correlation_id='CURRENT', status='Approved', detail='Reserved'))
        ms._assert_create_available(self.ctx, exclude='CURRENT')
        with self.assertRaises(ms.MappedSendError):
            ms._assert_create_available(self.ctx)

    def test_timeout_is_latched_even_without_a_returned_remote_identity(self):
        fingerprint = self.preview()['fingerprint']
        self.mutation.execute_once.side_effect = TimeoutError('Synthetic timeout')
        with patch('hero_integration.api._finalize_mutation', side_effect=lambda audit, status, detail:
                   self.audit_rows.append(NS(correlation_id=audit.correlation_id, status=status, detail=detail))), \
                self.assertRaises(ms.MappedSendError):
            self.send(fingerprint)
        self.assertEqual(self.created, [])
        with self.assertRaises(ms.MappedSendError):
            ms._assert_create_available(self.ctx)


class TestUserLookupSchemaProof(TestCase):
    def test_lookup_requires_same_schema_user_email_and_id_before_network(self):
        schema = introspection_from_schema(build_schema('''
        type Query { company:Company, contacts:[Customer!]! }
        type Company { partners:[Partner!]! }
        type Partner { user:User }
        type User { id:Int!, email:String }
        type Customer { id:Int!, email:String }
        '''))
        ctx = NS(snapshot=NS(snapshot_json=json.dumps(schema)))
        good = NS(contract=NS(records_path='company.partners'))
        config = {'service': 'USERS', 'key': 'user.email', 'value': 'user.id'}
        ms._validate_user_lookup_namespace(ctx, config, good)
        bad = NS(contract=NS(records_path='contacts'))
        with patch.object(ms, '_load_read', return_value=bad), patch.object(ms, '_read_rows') as network, \
                self.assertRaisesRegex(ms.MappedSendError, 'same schema User'):
            ms._lookup(ctx, {'resolver': 'USER_LINK', 'lookup': {'service': 'CONTACTS', 'key': 'email', 'value': 'id'}}, {}, NS())
        network.assert_not_called()

    def test_resolver_options_cannot_be_ignored(self):
        for extra in ({'date_mode': 'DATE'}, {'lookup': {'service': 'X', 'key': 'email', 'value': 'id'}}):
            value = content()
            value['send_options']['assignments']['primary'].update(extra)
            with self.subTest(extra=extra), self.assertRaisesRegex(ms.MappedSendError, 'not applicable'):
                ms.normalise_send_mapping(value, SCHEMA)


class TestBindingIdentityAcknowledgment(_TransportFixture):
    def test_changed_binding_identity_or_conflict_does_not_receive_old_record_ack(self):
        fingerprint = self.preview()['fingerprint']
        original = self.ctx.link
        changed = deepcopy(original)
        changed.source_instance = 'different-source'
        changed.source_id = '999'
        changed.target_name = 'C2'
        changed.sync_status = 'Conflict'
        self.frappe.get_doc = lambda doctype, name, **kw: changed if doctype == 'HERO Integration Link' else self.ctx.document
        with patch.object(ms, '_read_record', side_effect=[{'id': 42, 'remarks': 'Old'}, {'id': 42, 'remarks': 'Expected'}]), \
                self.assertRaises(ms.MappedSendError):
            self.send(fingerprint)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.statuses, ['Failed'])
        self.frappe.db.rollback.assert_called_once()


class TestCreateUpdateProfileSelection(TestCase):
    def test_separate_create_and_update_profiles_are_selected_by_binding_mode(self):
        creating, updating = create_content(), create_content()
        updating['send_options']['mode'] = 'UPDATE'
        rows = [NS(name='CREATE-1', mapping_json=json.dumps(creating)),
                NS(name='UPDATE-1', mapping_json=json.dumps(updating))]
        self.assertEqual(ms._profile_name(rows, 'Task', 'CREATE'), 'CREATE-1')
        self.assertEqual(ms._profile_name(rows, 'Task', 'UPDATE'), 'UPDATE-1')
        for mode in ('CREATE', 'UPDATE'):
            with self.subTest(mode=mode), self.assertRaises(ms.MappedSendError):
                ms._profile_name(rows + rows, 'Task', mode)
