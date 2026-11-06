from hero_integration.tests.saved_reader_fixture import isolate_saved_materialization
"""Complete mapping-owned Project histories without new read services or duplicate Comments."""
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch
from html import escape
import json

from hero_integration.services import project_logbook as lb, query_mapping as qm
from hero_integration.tests.test_query_mapping_v2 import project_rules, plan, rule, fixture


class TestMappedLogbookPagination(TestCase):
    def setUp(self):
        isolate_saved_materialization(self)

    def source(self, pages, **options):
        config = {'path': 'histories', 'identity_path': 'id', 'limit': 100,
                  'offset_variable': 'history_offset', 'max_pages': 5, **options}
        contract = NS(adapter_options={'logbook': config}, page_size=1,
                      identity_path='id', records_path='project_matches')
        calls = []
        def query(document, variables):
            calls.append((document, variables))
            value = pages.pop(0)
            return {'project_matches': value}
        source = NS(contract=contract, service=NS(query_document='exact approved document'),
                    client=NS(query=query), revalidate=lambda: contract)
        source._saved_request = lambda variables: NS(document=source.service.query_document, variables=variables)
        return source, calls

    def test_short_server_pages_are_exhausted_using_the_exact_approved_document(self):
        source, calls = self.source([[{'id': 7, 'histories': [{'id': 2}]}], [{'id': 7, 'histories': []}]])
        result = lb.complete_mapped_histories(source, [{'id': 7, 'histories': [{'id': 1}]}],
                                               {'first': 1, 'offset': 9, 'history_offset': 0})
        self.assertEqual([r['id'] for r in result[0]['histories']], [1, 2])
        self.assertTrue(result[0]['_hero_logbook_complete'])
        self.assertEqual([c[1]['history_offset'] for c in calls], [1, 2])
        self.assertTrue(all(c[0] == 'exact approved document' and c[1]['offset'] == 9 for c in calls))

    def test_project_reader_exhausts_children_before_yielding_annotated_project(self):
        from hero_integration.sources.hero import GraphQLServiceSource
        source, calls = self.source([[{'id': 7, 'histories': [{'id': 1}]}],
                                     [{'id': 7, 'histories': [{'id': 2}]}], [{'id': 7, 'histories': []}]])
        source.contract.target_operation = 'HeroProjectMatches'
        source.contract.variables = {'first': '$page_size', 'offset': '$offset', 'history_offset': 0}
        source.query_adapter = source.contract.adapter_options
        source.source_instance = 'source-a'
        result = GraphQLServiceSource._page(source, 0, 'HeroProjectMatches')
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result.records[0]['histories']), 2)
        self.assertEqual(result.records[0]['_hero_source_instance'], 'source-a')
        self.assertEqual(result.next_offset, 1)

    def test_repeated_identity_parent_loss_and_page_bound_fail_closed(self):
        for pages, options, message in [
            ([[{'id': 7, 'histories': [{'id': 1}]}]], {}, 'repeated'),
            ([[{'id': 8, 'histories': []}]], {}, 'parent'),
            ([[{'id': 7, 'histories': [{'id': 2}]}]], {'max_pages': 1}, 'bound'),
        ]:
            with self.subTest(message=message):
                source, _ = self.source(pages, **options)
                with self.assertRaisesRegex(ValueError, message):
                    lb.complete_mapped_histories(source, [{'id': 7, 'histories': [{'id': 1}]}], {})

    def test_revoked_approval_prevents_next_request(self):
        source, calls = self.source([])
        source.revalidate = lambda: None
        with self.assertRaisesRegex(ValueError, 'approval changed'):
            lb.complete_mapped_histories(source, [{'id': 7, 'histories': [{'id': 1}]}], {})
        self.assertEqual(calls, [])

    def test_empty_final_page_at_bound_is_complete(self):
        source, _ = self.source([[{'id': 7, 'histories': []}]], max_pages=1)
        self.assertTrue(lb.complete_mapped_histories(source, [{'id': 7, 'histories': [{'id': 1}]}], {})[0]['_hero_logbook_complete'])


class TestCompleteMappedLogbook(TestCase):
    def setUp(self):
        self.options = {'version': 2, 'transform_options': {'created': {'timezone': 'UTC'}, 'modified': {'timezone': 'UTC'}},
            'logbook': {'path': 'histories', 'identity_path': 'id', 'limit': 100,
                       'context_paths': ['histories.user_id', 'histories.additional_file_uploads.id', 'histories.weather_attachment.temp'],
                       'visibility_path': 'histories.role_visibility'}}
        rules = project_rules(True) + [
            rule('history-title', 'histories.custom_title', 'Comment', 'subject', operation='HeroProjectMatches'),
            rule('author', 'histories.author_name', 'Comment', 'comment_by', operation='HeroProjectMatches'),
            rule('email', 'histories.user.email', 'Comment', 'comment_email', operation='HeroProjectMatches'),
            rule('modified', 'histories.modified', 'Comment', 'modified', operation='HeroProjectMatches', transform='Local Datetime'),
        ]
        self.plan = plan(rules, self.options, operation='HeroProjectMatches')
        self.record = {'id': 7, '_hero_source_instance': 'source-a', 'histories': [{
            'id': 9, 'custom_title': 'Title', 'custom_text': '<p>Text</p>', 'author_name': 'Name',
            'user': {'email': 'synthetic@example.invalid'}, 'user_id': 8, 'role_visibility': '',
            'created': '2026-01-01T00:00:00Z', 'modified': '2026-01-02T00:00:00Z',
            'additional_file_uploads': [{'id': 42}], 'weather_attachment': {'temp': 18},
        }]}
        self.docs = {}; self.links = []; self.writes = []
        def get_doc(*args, **kwargs):
            if isinstance(args[0], dict):
                values = dict(args[0]); doc = NS(name='doc-' + str(len(self.docs)), **values)
                def insert(**kw):
                    self.docs[doc.name] = doc
                    if values['doctype'] == 'HERO Integration Link': self.links.append(doc)
                    return doc
                doc.insert = insert
                return doc
            if args[0] == 'Project': return NS(name=args[1])
            return self.docs[args[1]]
        def set_value(dt, name, values, **kw):
            self.writes.append((dt, name, values))
            for field, value in values.items(): setattr(self.docs[name], field, value)
        def locked_links(query, values, **kwargs):
            if 'target_doctype=%s' in query:
                keys = ['source_system', 'source_instance', 'source_type', 'target_doctype', 'target_name']
            else:
                keys = ['source_system', 'source_type', 'source_instance', 'source_id']
            return [x for x in self.links if all(getattr(x, key, None) == value for key, value in zip(keys, values))]
        self.fake = NS(get_doc=get_doc, get_all=lambda dt, filters, **kw: [x for x in self.links
                if all(getattr(x, key, None) == value for key, value in filters.items())],
            has_permission=lambda *a, **kw: True,
            db=NS(exists=lambda dt, name: name in self.docs, set_value=set_value, sql=locked_links),
            utils=NS(escape_html=escape, now_datetime=lambda: '2026-02-01 00:00:00'))

    def sync(self, **options):
        with patch.object(lb, 'frappe', self.fake):
            return lb.sync_mapped_entries(self.plan, self.record, 'P1', **options)

    def test_native_metadata_context_and_updates_are_mapped_without_duplicates(self):
        self.assertEqual(self.sync(), 1)
        comment = self.docs[self.links[0].target_name]
        self.assertEqual((comment.subject, comment.comment_by, comment.comment_email), ('Title', 'Name', 'synthetic@example.invalid'))
        self.assertEqual(comment.creation, '2026-01-01 00:00:00')
        self.assertEqual(comment.modified, '2026-01-02 00:00:00')
        self.assertEqual(json.loads(comment.custom_hero_logbook_context), {'user_id': 8, 'additional_file_uploads.id': [42], 'weather_attachment.temp': 18})
        self.record['histories'][0]['custom_text'] = 'Changed'
        self.assertEqual(self.sync(), 0)
        self.assertEqual(comment.content, 'Changed')
        self.assertEqual(len(self.links), 1)
        self.assertEqual(self.links[0].sync_status, 'Synchronized')

    def test_unchanged_logbook_does_not_rewrite_comments_or_bindings(self):
        self.sync()
        self.writes.clear()
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.writes, [])

    def test_frappe_line_break_serialization_is_not_a_local_edit(self):
        self.record['histories'][0]['custom_text'] = 'First</br>Second'
        self.sync()
        comment = self.docs[self.links[0].target_name]
        comment.content = comment.content.replace('</br>', '<br>')
        self.writes.clear()
        conflicts = []
        self.sync(conflicts=conflicts)
        self.assertEqual(conflicts, [])
        self.assertEqual(comment.content, 'First<br>Second')
        self.assertEqual(self.links[0].sync_status, 'Synchronized')
        self.assertTrue(all(doctype != 'Comment' for doctype, _, _ in self.writes))
        comment.content = 'Actual local change'
        self.sync(conflicts=conflicts)
        self.assertEqual(conflicts, [self.links[0].name])

    def test_local_edit_is_retained_but_new_restriction_still_applies(self):
        self.sync(); comment = self.docs[self.links[0].target_name]
        comment.content = 'Local edit'
        self.record['histories'][0]['role_visibility'] = 'manager'
        conflicts = []
        self.sync(conflicts=conflicts)
        self.assertEqual(conflicts, [self.links[0].name])
        self.assertEqual(comment.content, 'Local edit')
        self.assertEqual(comment.reference_doctype, 'HERO Integration Link')
        self.assertEqual(self.links[0].sync_status, 'Conflict')
        self.record['histories'][0]['role_visibility'] = ''
        self.sync()
        self.assertEqual(comment.reference_doctype, 'HERO Integration Link')

    def test_new_private_entry_never_becomes_a_public_timeline_entry(self):
        self.record['histories'][0]['role_visibility'] = 'manager'
        self.sync(); comment = self.docs[self.links[0].target_name]
        self.assertEqual((comment.reference_doctype, comment.reference_name), ('HERO Integration Link', self.links[0].name))
        self.record['histories'][0]['role_visibility'] = ''
        self.sync()
        self.assertEqual(comment.reference_doctype, 'HERO Integration Link')

    def test_missing_visibility_or_context_fails_before_writes(self):
        for missing in ('role_visibility', 'additional_file_uploads'):
            saved = self.record['histories'][0].pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValueError): self.sync()
            self.record['histories'][0][missing] = saved
        self.assertEqual(self.docs, {})

    def test_new_native_fields_validate_and_other_comment_fields_remain_protected(self):
        for spec in self.plan.definitions: qm.validate_v2_spec(spec, None, self.options)
        bad = rule('owner', 'histories.author_name', 'Comment', 'owner', operation='HeroProjectMatches')
        with self.assertRaisesRegex(ValueError, 'protected'): qm.validate_v2_spec(bad, None, self.options)


class TestLogbookReadSeal(TestCase):
    def test_cursor_is_approved_only_for_a_single_project_and_exclusive_variable(self):
        service, snapshot, _ = fixture()
        options = {'version': 2, 'transform_options': {'created': {'timezone': 'UTC'}},
            'logbook': {'path': 'histories', 'identity_path': 'id', 'limit': 20,
                       'offset_variable': 'history_offset', 'max_pages': 20}}
        p = plan(project_rules(True), options, operation='HeroProjectMatches')
        config = {'target_operation': 'HeroProjectMatches', 'records_path': 'project_matches', 'identity_path': 'id',
                  'page_size': 1, 'variables': {'first': '$page_size', 'offset': '$offset', 'history_offset': 0}, 'query_adapter': options}
        document = '''query projects($first:Int!,$offset:Int!,$history_offset:Int!){
            project_matches(first:$first,offset:$offset){id project_title project_nr project_type customer_id
              customer {title full_name} address {full_address}
              histories(first:20,offset:$history_offset){id custom_text author_name created}}}'''
        service = NS(operation_name='projects', query_document=document)
        self.assertTrue(qm.seal_query(service, snapshot, config, p))
        for bad in ({**config, 'page_size': 2}, {**config, 'variables': {**config['variables'], 'history_offset': 1}}):
            with self.assertRaisesRegex(ValueError, 'one Project'):
                qm.seal_query(service, snapshot, bad, p)
        bad_service = NS(operation_name='projects', query_document=document.replace('first:20', 'first:$history_offset'))
        with self.assertRaises(ValueError): qm.seal_query(bad_service, snapshot, config, p)

    def test_configured_context_must_be_selected_in_the_query(self):
        service, snapshot, _ = fixture()
        options = {'version': 2, 'transform_options': {'created': {'timezone': 'UTC'}},
            'logbook': {'path': 'histories', 'identity_path': 'id', 'limit': 20, 'context_paths': ['histories.author_name']}}
        p = plan(project_rules(True), options, operation='HeroProjectMatches')
        config = {'target_operation': 'HeroProjectMatches', 'records_path': 'project_matches', 'identity_path': 'id',
                  'variables': {'first': '$page_size', 'offset': '$offset'}, 'query_adapter': options}
        service = NS(operation_name='projects', query_document='''query projects($first:Int!,$offset:Int!){
            project_matches(first:$first,offset:$offset){id project_title project_nr project_type customer_id
              customer {title full_name} address {full_address} histories(first:20){id custom_text created}}}''')
        with self.assertRaises(ValueError): qm.seal_query(service, snapshot, config, p)


class TestCommentTimestampPublication(TestCase):
    def test_only_registered_comment_timestamps_get_standard_metadata(self):
        import sys
        from hero_integration.services.frappe_mapping_catalog import query_standard_target
        fake = NS(db=NS(has_column=lambda dt, field: field in {'creation', 'modified'}), _dict=lambda **kw: NS(**kw))
        with patch.dict(sys.modules, {'frappe': fake, 'frappe.model': NS(default_fields=['creation', 'modified', 'owner'])}):
            for field in ('creation', 'modified'):
                metadata = query_standard_target('Comment', field, True)
                self.assertEqual((metadata.fieldname, metadata.fieldtype), (field, 'Datetime'))
            self.assertIsNone(query_standard_target('Comment', 'owner', True))
            self.assertIsNone(query_standard_target('Project', 'modified', True))
            self.assertIsNone(query_standard_target('Comment', 'modified', False))

    def test_publication_resolves_modified_without_creating_a_custom_field(self):
        from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
        storage = FrappeMappingDraftStorage(NS(get_meta=lambda dt: self.fail('Standard timestamp is not a DocField.')))
        content = {'query_adapter': {'version': 2}, 'mapping_definition_ids': ['modified'],
                   'field_rules': {'modified': {'target_doctype': 'Comment', 'target_field': 'modified', 'transformation': 'Local Datetime'}}}
        updated = storage._complete_query_target_types(content)
        self.assertEqual(updated['query_adapter']['transform_options']['modified']['target_type'], 'Datetime')


class TestOutboundLogbookEcho(TestCase):
    setUp = TestCompleteMappedLogbook.setUp
    sync = TestCompleteMappedLogbook.sync

    def test_outbound_echo_retains_native_content_author_and_timestamps_without_duplicate(self):
        from hero_integration.services.project_logbook_writeback import OUTBOUND_VERSION, _hash
        from hero_integration.services.task_send import plain_text
        self.sync()
        link = self.links[0];comment = self.docs[link.target_name]
        comment.content = '<p>Text</p>';comment.comment_by = 'Local author';comment.comment_email = 'local@example.invalid'
        comment.creation = '2025-03-04 05:06:07';comment.modified = '2025-03-04 05:06:08'
        link.mapping_version = OUTBOUND_VERSION
        link.source_payload_hash = _hash(plain_text(self.record['histories'][0]['custom_text']))
        link.target_payload_hash = _hash(comment.content)
        before = {key:getattr(comment,key) for key in ('content','comment_by','comment_email','creation','modified')}
        self.assertEqual(self.sync(),0)
        self.assertEqual({key:getattr(comment,key) for key in before},before)
        self.assertEqual(len(self.links),1)
        comment.content = 'Edited locally'
        conflicts=[];self.sync(conflicts=conflicts)
        self.assertEqual(comment.content,'Edited locally')
        self.assertEqual(conflicts,[link.name])

    def test_uncertain_project_result_blocks_import_before_comment_creation(self):
        from hero_integration.services.project_logbook_writeback import PENDING_TYPE
        self.links.append(NS(name='pending',source_system='HERO',source_instance='source-a',
            source_type=PENDING_TYPE,target_doctype='Project',target_name='P1',sync_status='Pending Review'))
        with self.assertRaisesRegex(ValueError,'unconfirmed outbound'):self.sync()
        self.assertEqual(self.docs,{})

    def test_version_echo_retains_original_change_record_without_comment_copy(self):
        from hero_integration.services.project_logbook_writeback import OUTBOUND_VERSION, _hash
        from hero_integration.services.task_send import plain_text
        self.sync();link=self.links[0]
        version=NS(name='V1',doctype='Version',ref_doctype='Project',docname='P1',data='{"changed":[["status","Open","Completed"]]}')
        self.docs['V1']=version;link.target_doctype='Version';link.target_name='V1';link.mapping_version=OUTBOUND_VERSION
        link.source_payload_hash=_hash(plain_text(self.record['histories'][0]['custom_text']))
        link.target_payload_hash=_hash(version.data)
        original=version.data;count=len(self.docs);self.writes.clear()
        self.assertEqual(self.sync(),0)
        self.assertEqual(version.data,original)
        self.assertEqual(len(self.docs),count)
        self.assertTrue(all(dt=='HERO Integration Link' for dt,_,_ in self.writes))
