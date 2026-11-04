"""Project activities are explicit creates with durable duplicate/echo protection."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from graphql import build_schema, get_introspection_query, graphql_sync

from hero_integration.services import project_logbook_writeback as wb

SCHEMA = graphql_sync(build_schema('''
 type Query { project_matches: [Project] }
 type Project { id: Int histories: [History] }
 type History { id: Int custom_text: String target: String target_id: Int }
 enum HistoryTargetEnum { project_match field_service_job }
 input LogbookEntryInput { target: HistoryTargetEnum target_id: Int custom_text: String! }
 type Mutation { add_logbook_entry(logbook_entry: LogbookEntryInput): History }
'''), get_introspection_query()).data


def content():
    return {'usage': wb.USAGE, 'version': 1, 'area': wb.AREA, 'read_service': 'reader',
        'source': {'doctype': 'Comment'}, 'target': {'operation': wb.OPERATION}, 'parent': deepcopy(wb.PARENT),
        'assignments': [{'id': str(uuid4()), 'source': {'doctype': 'Comment', 'field': 'content'},
                         'input_path': 'logbook_entry.custom_text', 'resolver': 'PLAIN_TEXT', 'options': {}}]}


class TestLogbookConfiguration(TestCase):
    def test_schema_bound_configuration_roundtrips_and_retains_input(self):
        value = content(); before = deepcopy(value)
        self.assertEqual(wb.normalise_mapping(value, SCHEMA), before)
        self.assertEqual(value, before)

    def test_parent_identity_author_permissions_and_hidden_literals_cannot_be_mapped(self):
        original = content()
        for mutation in ('target_id', 'author', 'roles', 'source', 'parent', 'extra', 'identity', 'html', 'uuid'):
            value = deepcopy(original)
            if mutation == 'target_id': value['assignments'][0]['input_path'] = 'logbook_entry.target_id'
            elif mutation == 'author': value['assignments'][0]['source']['field'] = 'comment_by'
            elif mutation == 'roles': value['assignments'][0]['options']['role_visibility'] = ['admin']
            elif mutation == 'source': value['source']['doctype'] = 'Communication'
            elif mutation == 'parent': value['parent']['target'] = 'field_service_job'
            elif mutation == 'extra': value['password'] = 'synthetic'
            elif mutation == 'identity': value['parent']['target_id'] = 42
            elif mutation == 'html': value['assignments'][0]['resolver'] = 'HTML'
            else: value['assignments'][0]['id'] = 'not-a-uuid'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): wb.normalise_mapping(value, SCHEMA)

    def test_real_html_editor_and_native_parent_metadata_are_accepted(self):
        types = {'content': 'HTML Editor', 'reference_doctype': 'Link', 'reference_name': 'Dynamic Link', 'comment_type': 'Select'}
        wb.validate_sources(content(), field_provider=lambda dt, name: NS(fieldtype=types[name], is_virtual=False))
        with self.assertRaises(ValueError): wb.validate_sources(content(), field_provider=lambda *a: None)

    def test_stable_scope_survives_content_profile_and_project_changes(self):
        with patch.object(wb, 'frappe', NS(local=NS(site='site'))):
            first = wb._comment_scope('source', 'C1')
            self.assertEqual(first, wb._comment_scope('source', 'C1'))
            self.assertNotEqual(first, wb._comment_scope('other', 'C1'))
            self.assertNotEqual(first, wb._comment_scope('source', 'C2'))


class TestLogbookPreparation(TestCase):
    def context(self):
        from hero_integration.services.graphql_contract import parse_mutation
        value = content()
        return NS(document=NS(name='C1', doctype='Comment', comment_type='Comment', modified='v1', content='<p>New<br>activity</p>',
                   reference_doctype='Project', reference_name='P1'), project=NS(name='P1', modified='p1'),
            mapping=NS(name='mapping', mapping_revision=1), content=value, proof=NS(fingerprint='proof'),
            link={'name': 'link', 'source_id': '7'}, contract=NS(name='contract', contract_revision=1, contract_fingerprint='fp'),
            service=NS(query_document='mutation Add($entry:LogbookEntryInput){add_logbook_entry(logbook_entry:$entry){id}}'),
            read=NS(contract=NS(adapter_options={'logbook': {'path': 'histories', 'identity_path': 'id'}}),
                    service=NS(query_document='query{project_matches{id histories{id custom_text}}}')),
            parsed=parse_mutation('mutation Add($entry:LogbookEntryInput){add_logbook_entry(logbook_entry:$entry){id}}'),
            raw_schema=SCHEMA, hero_id='7')

    def test_only_server_bound_project_and_plain_activity_text_enter_variables(self):
        ctx = self.context()
        with patch.object(wb, '_read_project', return_value={'id': 7, 'histories': [{'id': 9}]}):
            prepared = wb._prepare(ctx, Mock())
        self.assertEqual(prepared.variables, {'entry': {'target': 'project_match', 'target_id': 7, 'custom_text': 'New\nactivity'}})
        self.assertEqual(prepared.history_ids, {'9'})
        previous = prepared.fingerprint
        ctx.document.content = 'Edited'
        self.assertNotEqual(previous, wb._fingerprint(ctx))

    def test_empty_or_omitted_logbook_blocks_prepare(self):
        ctx = self.context();ctx.document.content = '<p></p>'
        with patch.object(wb, '_read_project', return_value={'id': 7, 'histories': []}):
            with self.assertRaises(ValueError): wb._prepare(ctx, Mock())
        with patch.object(wb, '_read_project', return_value={'id': 7}):
            with self.assertRaises(ValueError): wb._prepare(ctx, Mock())

    def test_readback_must_prove_identity_text_and_parent(self):
        ctx = self.context();prepared = NS(text='Expected')
        for entry in ({'id': 1, 'custom_text': 'Wrong', 'target': 'project_match', 'target_id': 7},
                      {'id': 1, 'custom_text': 'Expected', 'target': 'project_match', 'target_id': 8},
                      {'id': 2, 'custom_text': 'Expected', 'target': 'project_match', 'target_id': 7}):
            with patch.object(wb, '_read_project', return_value={'id': 7, 'histories': [entry]}),self.assertRaises(ValueError):
                wb._verify_response(ctx, prepared, 1, Mock())
        with patch.object(wb, '_read_project', return_value={'id': 7, 'histories': [
                {'id': 1, 'custom_text': 'Expected', 'target': 'project_match', 'target_id': 7}]}):
            wb._verify_response(ctx, prepared, 1, Mock())


class TestLogbookSend(TestCase):
    def setUp(self):
        self.ctx = NS(proof=NS(source_instance='source'), snapshot=NS(schema_fingerprint='schema'),
            mapping=NS(mapping_revision=1), project=NS(name='P1'), service=NS(query_document='approved mutation'))
        self.prepared = NS(fingerprint='fingerprint', variables={'entry': {}}, history_ids={'9'})
        self.audit = NS(correlation_id='own-audit')
        self.events = []; self.transport = Mock()
        self.transport.execute_once.return_value = {wb.OPERATION: {'id': 10}}
        self.database = NS(commit=Mock(), rollback=Mock(), set_value=Mock())
        self.fake = NS(local=NS(site='site'), db=self.database, utils=NS(now_datetime=lambda: 'now'))
        patches = [patch.object(wb, 'frappe', self.fake), patch.object(wb, '_load_context', return_value=self.ctx),
            patch.object(wb, '_prepare', return_value=self.prepared), patch.object(wb, '_fingerprint', return_value='fingerprint'),
            patch.object(wb, '_reserve_project', side_effect=lambda *a: self.events.append('marker') or 'marker'),
            patch.object(wb, '_record_response', side_effect=lambda *a: self.events.append('receipt') or 'receipt'),
            patch.object(wb, '_verify_response', side_effect=lambda *a: self.events.append('readback')),
            patch('hero_integration.services.writeback_source.validate_source', return_value=NS()),
            patch('hero_integration.api._assert_mutation_execution_released'),
            patch('hero_integration.api._reserve_mutation', return_value=self.audit),
            patch('hero_integration.api._finalize_mutation')]
        self.mocks=[]
        for p in patches: self.mocks.append(p.start());self.addCleanup(p.stop)

    def send(self, **kwargs):
        return wb.send('C1', fingerprint='fingerprint', confirmed=True, client=Mock(), mutation_client=self.transport, **kwargs)

    def test_exactly_one_attempt_with_receipt_before_readback_and_two_commit_revalidations(self):
        result = self.send()
        self.assertEqual(result['status'], 'SENT')
        self.transport.execute_once.assert_called_once_with('approved mutation', {'entry': {}})
        self.assertEqual(self.events, ['marker', 'receipt', 'readback'])
        self.assertEqual(self.mocks[1].call_count, 4)
        self.assertEqual(self.mocks[-2].call_args.kwargs['request_fingerprint'], wb._comment_scope('source', 'C1'))
        self.assertEqual(self.mocks[-1].call_args.args[1], 'Succeeded')

    def test_unconfirmed_or_changed_preview_never_reserves_or_sends(self):
        for confirmed, fingerprint in ((False, 'fingerprint'), (True, 'stale')):
            with self.assertRaises(ValueError): wb.send('C1', fingerprint=fingerprint, confirmed=confirmed, client=Mock(), mutation_client=self.transport)
        self.mocks[-2].assert_not_called();self.transport.execute_once.assert_not_called()

    def test_post_reservation_authority_failure_stops_transport(self):
        self.mocks[1].side_effect = [self.ctx, ValueError('changed')]
        with self.assertRaises(ValueError): self.send()
        self.transport.execute_once.assert_not_called()
        self.assertEqual(self.mocks[-1].call_args.args[1], 'Failed')

    def test_timeout_unknown_response_or_failed_readback_never_retries_or_releases_marker(self):
        self.transport.execute_once.side_effect = TimeoutError()
        with self.assertRaises(ValueError): self.send()
        self.transport.execute_once.assert_called_once()
        self.database.set_value.assert_not_called()
        self.mocks[5].assert_not_called()
        self.transport.execute_once.reset_mock();self.transport.execute_once.side_effect = None
        self.mocks[6].side_effect = ValueError('readback changed')
        with self.assertRaises(ValueError): self.send()
        self.assertEqual(self.events[-2:], ['marker', 'receipt'])
        self.transport.execute_once.assert_called_once()
        self.database.set_value.assert_not_called()

    def test_preexisting_returned_identity_cannot_be_bound(self):
        self.transport.execute_once.return_value = {wb.OPERATION: {'id': 9}}
        with self.assertRaises(ValueError): self.send()
        self.mocks[5].assert_not_called()

    def test_prior_attempt_remains_blocked_for_every_status(self):
        fake = NS(local=NS(site='site'), get_all=Mock(return_value=[NS(correlation_id='previous')]))
        with patch.object(wb, 'frappe', fake):
            with self.assertRaises(ValueError): wb._assert_not_submitted('source', 'C1', exclude='own-audit')
            fake.get_all.return_value = [NS(correlation_id='own-audit')]
            wb._assert_not_submitted('source', 'C1', exclude='own-audit')

    def test_pending_project_marker_blocks_import_and_other_activity(self):
        sql = Mock(return_value=[NS(name='marker', sync_status='Pending Review', conflict_detail='another-audit')])
        with self.assertRaises(ValueError): wb.assert_project_import_clear('source', 'P1', frappe_api=NS(db=NS(sql=sql)))
        query, params = sql.call_args.args
        self.assertIn('FOR UPDATE', query)
        self.assertEqual(params, ('HERO', 'source', wb.PENDING_TYPE, 'Project', 'P1'))


class TestLogbookReadAndNativeEligibility(TestCase):
    def test_required_return_fields_must_be_selected_before_creation(self):
        read=NS(contract=NS(records_path='project_matches',adapter_options={'logbook':{'path':'histories','identity_path':'id'}}),
            service=NS(query_document='query{project_matches{id histories{id custom_text target target_id}}}'))
        wb._require_readback_paths(read)
        for field in ('custom_text','target_id','target'):
            original=read.service.query_document
            read.service.query_document=original.replace(' '+field+' ', ' ') if ' '+field+' ' in original else original.replace(' '+field+'}', '}')
            with self.subTest(field=field),self.assertRaises(ValueError):wb._require_readback_paths(read)
            read.service.query_document=original

    def test_only_local_comment_type_on_project_is_eligible(self):
        for kind,parent in (('Unknown','Project'),('Comment','HERO Integration Link'),('Comment','Task')):
            doc=NS(name='C1',comment_type=kind,reference_doctype=parent,reference_name='P1',content='Body')
            fake=NS(get_doc=Mock(return_value=doc),has_permission=Mock())
            with patch.object(wb,'frappe',fake),self.assertRaises(ValueError):wb._native_comment('C1')

    def test_imported_comment_is_not_sent_even_if_reclassified_as_local_comment(self):
        doc=NS(name='C1',comment_type='Comment',reference_doctype='Project',reference_name='P1',content='Body')
        fake=NS(get_doc=lambda dt,name,**kw:doc if dt=='Comment' else NS(name='P1'),has_permission=Mock(),
            get_all=Mock(return_value=['import-link']))
        with patch.object(wb,'frappe',fake),self.assertRaisesRegex(ValueError,'already linked'):wb._native_comment('C1')

    def test_legacy_raw_identity_importer_cannot_consume_outbound_projects(self):
        from hero_integration.services import project_logbook
        fake=NS(get_doc=Mock(),db=NS(sql=Mock(return_value=[NS(name='marker',sync_status='Synchronized')])) )
        with patch.object(project_logbook,'frappe',fake),self.assertRaisesRegex(ValueError,'approved mapped Project'):
            project_logbook._guard_legacy_outbound('P1','source')
        fake.get_doc.assert_called_once_with('Project','P1',for_update=True)
        self.assertIn('FOR UPDATE',fake.db.sql.call_args.args[0])


class TestAutomaticProjectActivities(TestCase):
    def test_change_summary_uses_native_labels_and_excludes_passwords_hidden_fields_and_nested_values(self):
        fields={'status':NS(label='Status',fieldtype='Select',hidden=0),
                'private':NS(label='Private',fieldtype='Password',hidden=0),
                'hidden':NS(label='Hidden',fieldtype='Data',hidden=1),
                'tasks':NS(label='Tasks',fieldtype='Table',hidden=0)}
        version=NS(doctype='Version',data=json.dumps({'changed':[['status','Open','Completed'],
            ['private','secret-old','secret-new'],['hidden','a','b'],['status',{},[]]],'added':[['tasks',{'password':'secret'}]]}))
        fake=NS(get_meta=lambda dt:NS(get_field=fields.get),_=lambda text:text)
        with patch.object(wb,'frappe',fake):text=wb._version_text(version)
        self.assertEqual(text,'Status: Open → Completed\nTasks: Added (1)')
        self.assertNotIn('secret',text)
        value=content();value['version_source']=deepcopy(wb.VERSION_SOURCE)
        with patch.object(wb,'frappe',fake):self.assertEqual(wb._activity_text(version,value),text)
        with self.assertRaises(ValueError):wb._activity_text(version,content())

    def test_automatic_comment_types_are_explicit_and_empty_event_uses_native_type_label(self):
        value=content();value['comment_types']=['Comment','Assigned','Created']
        document=NS(doctype='Comment',comment_type='Assigned',content='<p>Assigned to a team</p>')
        self.assertEqual(wb._activity_text(document,value),'Assigned to a team')
        document.comment_type='Created';document.content=''
        with patch.object(wb,'frappe',NS(_=lambda text:text)):self.assertEqual(wb._activity_text(document,value),'Created')
        with self.assertRaises(ValueError):wb._activity_text(document,content())
        with patch.object(wb,'frappe',NS(local=NS(site='site'))):
            self.assertNotEqual(wb._comment_scope('source','same','Comment'),wb._comment_scope('source','same','Version'))

    def test_no_duplicate_version_capture_or_import_echo(self):
        doc=NS(name='P1',meta=NS(track_changes=True),flags=NS(ignore_version=False))
        fake=NS(get_all=Mock(),flags=NS(in_install=False,in_migrate=False,in_patch=False))
        with patch.object(wb,'frappe',fake):wb.capture_project_activity(doc)
        fake.get_all.assert_not_called()
        doc.meta.track_changes=False
        with patch.object(wb,'frappe',fake),patch('hero_integration.services.change_detection.managed_target_update_active',return_value=True):
            wb.capture_project_activity(doc)
        fake.get_all.assert_not_called()

    def test_native_version_capture_only_for_opted_in_bound_project_and_meaningful_changes(self):
        doc=NS(name='P1',meta=NS(track_changes=False),flags=NS(ignore_version=False),get_doc_before_save=lambda:NS())
        value=content();value['version_source']=deepcopy(wb.VERSION_SOURCE)
        version=NS(update_version_info=Mock(return_value=True),insert=Mock())
        fake=NS(flags=NS(in_install=False,in_migrate=False,in_patch=False),new_doc=Mock(return_value=version),
            get_all=Mock(side_effect=[[NS(mapping_json=json.dumps(value))],['project-binding']]))
        with patch.object(wb,'frappe',fake),patch('hero_integration.api._configured_source_instance',return_value='source'), \
             patch('hero_integration.services.change_detection.managed_target_update_active',return_value=False), \
             patch.object(wb,'_version_text',return_value='Status changed'):
            wb.capture_project_activity(doc)
        version.insert.assert_called_once_with(ignore_permissions=True)
        fake.new_doc.assert_called_once_with('Version')


class TestAutomaticActivityGuards(TestCase):
    def test_rendered_change_text_and_language_are_bound_to_confirmation(self):
        ctx=TestLogbookPreparation().context()
        first=wb._fingerprint(ctx,text='Status: Open → Completed')
        self.assertNotEqual(first,wb._fingerprint(ctx,text='Status: Offen → Abgeschlossen'))
        self.assertNotEqual(first,wb._fingerprint(ctx,text=''))

    def test_managed_import_versions_retain_native_audit_but_are_excluded_from_outbound(self):
        from hero_integration.services.change_detection import managed_target_update
        version=NS(ref_doctype='Project',docname='P1',data='{"changed":[["status","Open","Completed"]]}')
        with managed_target_update('Project','P1','link'):
            wb.mark_imported_project_version(version)
        self.assertTrue(json.loads(version.data)['_hero_import'])
        self.assertEqual(json.loads(version.data)['changed'],[['status','Open','Completed']])
        self.assertEqual(wb._version_text(version),'')
        local=NS(ref_doctype='Project',docname='P1',data='{"changed":[]}')
        wb.mark_imported_project_version(local)
        self.assertNotIn('_hero_import',local.data)

    def test_no_version_read_permission_preserves_eligible_comment_choices(self):
        from frappe import _dict
        value=content();value['version_source']=deepcopy(wb.VERSION_SOURCE)
        row=_dict(name='C1',content='Comment body',comment_type='Comment',creation='now')
        query=Mock(return_value=[row])
        def get_all(doctype,**kwargs):
            if doctype=='HERO GraphQL Mapping':return [NS(mapping_json=json.dumps(value))]
            return []
        fake=NS(local=NS(site='site'),has_permission=lambda dt,*a,**kw:dt!='Version',get_doc=lambda *a:NS(),
            get_all=get_all,get_list=query)
        with patch.object(wb,'frappe',fake),patch('hero_integration.services.mapped_send._roles',return_value=True), \
             patch('hero_integration.client.settings.get_configuration',return_value=NS(source_instance='source')):
            result=wb.candidates('P1')
        self.assertEqual(result['comments'][0]['activity_doctype'],'Comment')
        self.assertEqual(query.call_count,1)
        self.assertEqual(query.call_args.args[0],'Comment')


class TestProjectNotesAreNotLogbookActivities(TestCase):
    def test_notes_only_version_is_not_an_outbound_activity(self):
        fields={'notes':NS(label='Notes',fieldtype='Text Editor',hidden=0,read_only=0)}
        version=NS(doctype='Version',data=json.dumps({'changed':[['notes','Old','Old<br>Test 04.11.2026']]}))
        with patch.object(wb,'frappe',NS(get_meta=lambda _:NS(get_field=fields.get),_=lambda text:text)):
            self.assertEqual(wb._version_text(version),'')
        # A genuine timeline comment keeps exactly the comment text.
        comment=NS(doctype='Comment',comment_type='Comment',content='<p>Test 04.11.2026</p>')
        self.assertEqual(wb._activity_text(comment,{'comment_types':['Comment']}),'Test 04.11.2026')

    def test_mixed_version_retains_status_but_omits_notes(self):
        fields={name:NS(label=name,fieldtype='Data',hidden=0,read_only=0) for name in ('notes','status')}
        version=NS(data=json.dumps({'changed':[['notes','Old','New'],['status','Open','Completed']]}))
        with patch.object(wb,'frappe',NS(get_meta=lambda _:NS(get_field=fields.get),_=lambda text:text)):
            self.assertEqual(wb._version_text(version),'status: Open → Completed')
