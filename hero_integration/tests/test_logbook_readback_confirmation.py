"""HERO nl2br readback confirms existing receipts without repeated creates."""
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import project_logbook_writeback as wb, project_logbook as inbound


class TestReadbackRepresentations(TestCase):
    def test_php_nl2br_preserves_literal_tags_entities_and_deliberate_empty_lines(self):
        for text in ('Notes: old → new\nSecond line', 'A\n\nB', '<foo>\n&amp;', 'A B', 'A\r\nB'):
            with self.subTest(text=text):
                stored=text.replace('\r\n','<br />\r\n') if '\r\n' in text else text.replace('\n','<br />\n')
                self.assertIn(text,wb.readback_texts(stored))
                self.assertIn(text,wb.readback_texts(text))
        self.assertNotIn('AB',wb.readback_texts('A B'))
        self.assertNotIn('A\n\nB',wb.readback_texts('A<br />\nB'))

    def test_verify_response_accepts_exact_plain_text_before_html_interpretation(self):
        ctx=NS(read=NS(contract=NS(adapter_options={'logbook':{'path':'histories','identity_path':'id'}})),hero_id='7')
        for text in ('Notes: A → <foo>', 'Notes: A → &amp;', 'A\nB'):
            for stored in (text,text.replace('\n','<br />\n')):
                entry={'id':11,'target':'project_match','target_id':7,'custom_text':stored}
                with patch.object(wb,'_read_project',return_value={'histories':[entry]}):
                    wb._verify_response(ctx,NS(text=text),11,Mock())
        with patch.object(wb,'_read_project',return_value={'histories':[{'id':11,'target':'project_match','target_id':7,'custom_text':'Changed'}]}):
            with self.assertRaises(ValueError):wb._verify_response(ctx,NS(text='Expected'),11,Mock())

    def test_inbound_echo_uses_original_remote_body_not_generated_display_headings(self):
        text='Notes: old → new\nSecond line'
        activity=NS(doctype='Version',data='original immutable version')
        link=NS(name='receipt',sync_status='Synchronized',source_payload_hash=wb._hash(text),target_payload_hash=wb._hash(activity.data))
        writer=Mock();fake=NS(db=NS(set_value=writer),utils=NS(now_datetime=lambda:'now'))
        with patch.object(inbound,'frappe',fake):
            inbound._observe_outbound_activity(link,activity,{'content':'<b>Author and date</b><br>different display'},[],outbound_text=text.replace('\n','<br />\n'))
        self.assertEqual(writer.call_args.args[2]['sync_status'],'Synchronized')
        self.assertEqual(writer.call_args.args[2]['observed_source_hash'],link.source_payload_hash)


class TestConfirmedReceiptReconciliation(TestCase):
    def setUp(self):
        from hero_integration.services.project_logbook import _digest
        self.text='Notes: old → new\nSecond line'
        self.document=NS(doctype='Version',name='V1',data='immutable version data')
        self.receipt=NS(name='receipt',source_system='HERO',source_instance='source',source_type='project_history',
            mapping_version=wb.OUTBOUND_VERSION,sync_status='Pending Review',target_doctype='Version',target_name='V1',
            source_id=_digest(['source','7','11']),source_payload_hash=wb._hash(self.text),target_payload_hash=wb._hash(self.document.data))
        self.audit=NS(name='audit',correlation_id='correlation',status='Failed',operation_name=wb.OPERATION,
            request_fingerprint=wb._hash(['hero-project-logbook-create-v1','site','source','Version:V1']))
        self.ctx=NS(document=self.document,proof=NS(source_instance='source'),project=NS(name='P1'),hero_id='7',
            marker=NS(name='marker',sync_status='Pending Review',conflict_detail='correlation'),
            read=NS(contract=NS(adapter_options={'logbook':{'path':'histories','identity_path':'id'}})))
        self.entry={'id':11,'target':'project_match','target_id':7,'custom_text':self.text.replace('\n','<br />\n')}
        self.writer=Mock();self.fake=NS(only_for=Mock(),local=NS(site='site'),get_all=Mock(return_value=['audit']),
            get_doc=lambda doctype,name,**kwargs:self.receipt if doctype=='HERO Integration Link' else self.audit,
            utils=NS(now_datetime=lambda:'now'),db=NS(set_value=self.writer,commit=Mock()))
        patches=[patch.object(wb,'frappe',self.fake),patch.object(wb,'_load_context',return_value=self.ctx),
                 patch.object(wb,'_read_project',side_effect=lambda *args:{'histories':[self.entry]}),
                 patch('hero_integration.services.writeback_source.validate_source',return_value=NS()),
                 patch('hero_integration.client.graphql_mutation.HeroMutationClient.execute_once',side_effect=AssertionError('No create permitted'))]
        for item in patches:item.start();self.addCleanup(item.stop)

    def test_verified_existing_identity_updates_receipt_marker_and_audit_without_commit_or_create(self):
        result=wb.reconcile_receipt('receipt',client=Mock())
        self.assertEqual(result['status'],'RECONCILED');self.assertEqual(result['hero_mutations'],0)
        self.assertEqual(self.writer.call_count,3)
        self.fake.db.commit.assert_not_called()
        self.assertEqual(self.writer.call_args_list[-1].args[2]['status'],'Succeeded')

    def test_missing_or_changed_identity_text_parent_or_local_activity_never_clears_pending_state(self):
        original=deepcopy(self.entry)
        for field,value in (('id',12),('target_id',8),('target','contact'),('custom_text','Changed remotely')):
            with self.subTest(field=field):
                self.entry={**original,field:value}
                with self.assertRaises(ValueError):wb.reconcile_receipt('receipt',client=Mock())
                self.writer.assert_not_called()
        self.entry=original;self.document.data='Changed locally'
        with self.assertRaises(ValueError):wb.reconcile_receipt('receipt',client=Mock())
        self.writer.assert_not_called()

    def test_ambiguous_audit_wrong_source_and_foreign_marker_are_not_reconciled(self):
        self.fake.get_all.return_value=['a1','a2']
        with self.assertRaises(ValueError):wb.reconcile_receipt('receipt',client=Mock())
        self.fake.get_all.return_value=['audit'];self.ctx.marker.conflict_detail='other'
        with self.assertRaises(ValueError):wb.reconcile_receipt('receipt',client=Mock())
        self.ctx.marker.conflict_detail='correlation';self.receipt.source_instance='other'
        with self.assertRaises(ValueError):wb.reconcile_receipt('receipt',client=Mock())
        self.writer.assert_not_called()

    def test_inflight_audit_is_not_reconciled_or_changed(self):
        self.audit.status='Reserved'
        with self.assertRaises(ValueError):wb.reconcile_receipt('receipt',client=Mock())
        self.writer.assert_not_called()
