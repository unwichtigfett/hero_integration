"""Approved direct readbacks retain their collection shape and pagination contract."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import mapped_update_v2 as update
from hero_integration.services.read_contract import ReadContract, ReadContractError
from hero_integration.sources.hero import GraphQLServiceSource


class TestDirectReadback(TestCase):
    def context(self, area, path, *, page_size=2):
        variables = {} if area == 'HeroEmployees' else {'first': '$page_size', 'offset': '$offset'}
        contract = ReadContract('reader', 2, 'mapping', 3, area, path, 'id', (), variables, page_size,
                                adapter_options={'version': 2})
        service = NS(name='reader', query_document='saved approved query')
        mapping = NS(name='mapping')
        self.read_contract = Mock(return_value=contract)
        self.source_check = Mock()
        for owner in (
            patch.object(update, 'frappe', NS(get_doc=lambda kind, name: service if name == 'reader' else mapping)),
            patch('hero_integration.services.read_contract.from_documents', self.read_contract),
            patch('hero_integration.services.writeback_source.validate_source', self.source_check),
        ):
            owner.start(); self.addCleanup(owner.stop)
        return NS(read=NS(service=service, mapping=mapping, contract=contract), snapshot=NS(name='snapshot'),
                  content={'area': area, 'identity': {'read_path': 'id'}}, hero_id='7',
                  proof=NS(source_instance='source-a'))

    def test_employees_read_complete_list_exactly_once_for_both_saved_paths(self):
        for path in ('partners', 'company.partners'):
            with self.subTest(path=path):
                ctx = self.context('HeroEmployees', path)
                rows = [{'id': 8}, {'id': 7, 'user_id': 70, 'user': {'id': 70}}]
                data = {'partners': rows} if path == 'partners' else {'company': {'partners': rows}}
                client = NS(query=Mock(return_value=data))
                row = update._read_record(ctx, client)
                self.assertEqual(row['id'], 7)
                self.assertEqual(row['_hero_source_instance'], 'source-a')
                self.assertNotIn('_hero_source_instance', row['user'])
                client.query.assert_called_once_with('saved approved query', {})
                self.assertEqual(self.read_contract.call_count, 2)
                self.source_check.assert_called_once_with(ctx.proof)

    def test_duplicate_employee_id_is_blocked_even_when_not_the_selected_person(self):
        for repeated in (7, 8):
            with self.subTest(repeated=repeated):
                ctx = self.context('HeroEmployees', 'partners', page_size=4)
                rows = [{'id': 7, 'user_id': 70, 'user': {'id': 70}}, {'id': repeated}, {'id': repeated}]
                client = NS(query=Mock(return_value={'partners': rows}))
                with self.assertRaisesRegex(ValueError, 'duplicate employee identities'): update._read_record(ctx, client)
                self.assertEqual(client.query.call_count, 1)

    def test_employee_bound_and_single_request_offset_are_preserved(self):
        ctx = self.context('HeroEmployees', 'partners', page_size=1)
        client = NS(query=Mock(return_value={'partners': [{'id': 7}, {'id': 8}]}))
        source = GraphQLServiceSource(client, ctx.read.service, ctx.read.contract, 'source-a')
        page = source._page(0, 'HeroEmployees')
        self.assertEqual(len(page.records), 2)  # complete list may exceed the irrelevant offset page size
        self.assertIsNone(page.next_offset)
        from hero_integration.services.employee_migration import MAX_RECORDS
        client.query.return_value = {'partners': [{'id': i + 1} for i in range(MAX_RECORDS + 1)]}
        with self.assertRaisesRegex(ReadContractError, 'oversized employee list'): source._page(0, 'HeroEmployees')
        client.query.reset_mock()
        with self.assertRaises(ReadContractError): source._page(1, 'HeroEmployees')
        client.query.assert_not_called()

    def test_document_and_picture_edges_use_saved_path_and_annotate_only_root_records(self):
        for area, category in (('HeroDocuments', 'documents'), ('HeroPictures', 'images')):
            with self.subTest(area=area):
                ctx = self.context(area, 'FileUpload_FileUploads.edges.node')
                data = {'FileUpload_FileUploads': {'edges': [{'node': {'id': 7, 'category': category,
                    'project': {'id': 11}, '_hero_source_instance': 'untrusted'}}]}}
                original = deepcopy(data)
                client = NS(query=Mock(side_effect=[data, {'FileUpload_FileUploads': {'edges': []}}]))
                row = update._read_record(ctx, client)
                self.assertEqual(row['_hero_source_instance'], 'source-a')
                self.assertEqual(row['project'], {'id': 11})
                self.assertEqual(data, original)
                self.assertEqual([call.args[1] for call in client.query.call_args_list],
                                 [{'first': 2, 'offset': 0}, {'first': 2, 'offset': 1}])
                self.assertEqual(self.read_contract.call_count, 4)

    def test_missing_or_invalid_file_connection_is_not_treated_as_end_of_scan(self):
        ctx = self.context('HeroDocuments', 'FileUpload_FileUploads.edges.node')
        for data in ({}, {'FileUpload_FileUploads': {}}, {'FileUpload_FileUploads': {'edges': None}},
                     {'FileUpload_FileUploads': {'edges': [{'node': None}]}},
                     {'FileUpload_FileUploads': {'edges': [None]}}):
            with self.subTest(data=data):
                with self.assertRaises(ReadContractError):
                    update._read_record(ctx, NS(query=Mock(return_value=data)))

    def test_changed_approval_before_or_during_transport_remains_blocked(self):
        ctx = self.context('HeroDocuments', 'FileUpload_FileUploads.edges.node')
        changed = replace(ctx.read.contract, service_revision=3)
        for approvals, calls in (([changed], 0), ([ctx.read.contract, changed], 1)):
            with self.subTest(calls=calls):
                self.read_contract.side_effect = approvals
                client = NS(query=Mock(return_value={'FileUpload_FileUploads': {'edges': []}}))
                with self.assertRaisesRegex(ValueError, 'query changed'): update._read_record(ctx, client)
                self.assertEqual(client.query.call_count, calls)
