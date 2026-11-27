import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from hero_integration.services import source_history as history, file_transfer, project_workspace as workspace


def absence(identity=7, partner=3):
    row = dict.fromkeys(history.ABSENCE_PATHS)
    row.update(id=identity, partner_id=partner, start='2026-06-01', end='2026-06-03',
               status='approved', duration=3, budget=2.5, type='vacation', comment='Synthetic')
    return row


class SourceHistoryTests(unittest.TestCase):
    def read(self, pages):
        client = Mock()
        client.query.side_effect = [{'absences': page} for page in pages]
        req = Mock(document='query', variables={})
        with patch('hero_integration.services.direct_runtime.validated_snapshot'), patch(
                'hero_integration.services.mapping_contract_v1.QueryAdapter.materialize', return_value=req) as materialize:
            result = history.read_absences(client, Mock(), 'instance', [3])
        return result, [c.args[1].variables for c in materialize.call_args_list]

    def test_paging_and_employee_ownership(self):
        rows, calls = self.read([[absence()], [absence(9)], []])
        self.assertEqual([v['offset'] for v in calls], [0,1,2])
        self.assertTrue(all(v['partner_ids'] == [3] for v in calls))
        self.assertEqual([r['id'] for r in rows[3]], [7,9])

    def test_invalid_responses_do_not_form_a_partial_history(self):
        for pages in ([[absence()], [absence()]], [[absence(partner=4)]],
                      [[{**absence(), 'end':'2026-01-01'}]], [[{'id':7}]]):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                self.read(pages)

    def test_history_merge_preserves_identity_absent_rows_and_fractional_days(self):
        class Row(dict):
            __getattr__ = dict.__getitem__
        class Employee(dict):
            def append(self, field, values):
                self.setdefault(field, []).append(Row(values))
        doc = Employee()
        self.assertTrue(history.merge_absences(doc,[absence()], 'A'))
        self.assertFalse(history.merge_absences(doc,[absence()], 'A'))
        self.assertTrue(history.merge_absences(doc,[absence(8)], 'A'))
        self.assertTrue(history.merge_absences(doc,[{**absence(), 'comment':'Updated'}], 'A'))
        self.assertTrue(history.merge_absences(doc,[absence()], 'B'))
        rows=doc[history.ABSENCE_FIELD]
        self.assertEqual(len(rows),3)
        self.assertEqual((rows[0].source_comment, rows[0].duration_days, rows[0].budget_days),('Updated',3,2.5))

    def test_file_metadata_has_its_own_size_and_document_fields(self):
        read=SimpleNamespace(plan=SimpleNamespace(adapter_options={'source_history':'file_details'}))
        detail={'uuid':'abc','id':27,'size':200,'project_id':22,'customer_document':{'id':33,'value':12.5}}
        row={'uuid':'abc','id':27,'_hero_file_detail':detail}
        with patch.object(file_transfer.direct_runtime,'record_values',return_value={'file_name':'sample.jpeg'}):
            values=file_transfer.mapped_values(read,row)
            self.assertEqual(values['custom_hero_doc_value'],'12.5')
            self.assertEqual(values['custom_hero_doc_id'],'33')
            self.assertNotIn('file_size',values)
            for bad in ({},dict(detail,id=28),dict(detail,uuid='other')):
                with self.assertRaises(file_transfer.TransferError):
                    file_transfer.mapped_values(read,{**row,'_hero_file_detail':bad})
        self.assertFalse(any('url' in p for p in history.FILE_DETAIL_PATHS))

    def test_private_history_writes_only_changed_children(self):
        from frappe import _dict
        class Child(_dict):
            def db_update(self):
                self.writes = (self.writes or 0) + 1
        class Employee(_dict):
            def append(self, field, values):
                child = Child(values)
                self.setdefault(field, []).append(child)
                return child
        doc = Employee(name='E')
        fake = SimpleNamespace(db=Mock(), utils=Mock(), session=SimpleNamespace(user='worker'))
        with patch.dict('sys.modules', frappe=fake):
            self.assertTrue(history.store_absences(doc, [absence()], 'A'))
            self.assertFalse(history.store_absences(doc, [absence()], 'A'))
            self.assertTrue(history.store_absences(doc, [absence(8)], 'A'))
        self.assertEqual([child.writes for child in doc[history.ABSENCE_FIELD]], [1,1])
        self.assertEqual(fake.db.set_value.call_count, 2)
