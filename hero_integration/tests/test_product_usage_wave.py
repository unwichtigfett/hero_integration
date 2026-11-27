import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import direct_runtime, file_migration, migration, product_usage


class TestUsagePage(TestCase):
    def test_allowlist_is_pinned_to_source_revision_and_bytes(self):
        raw = b"product_id;Genutzt\r\n001;9\r\n"
        digest = hashlib.sha256(raw).hexdigest()
        config = dict(sha256=digest, source_instance='dev', source_binding_revision=3)
        with TemporaryDirectory() as folder:
            csv_path = Path(folder) / (digest + '.csv')
            csv_path.write_bytes(raw)
            site = NS(conf={'hero_product_usage': config}, get_site_path=lambda *parts: str(csv_path))
            with patch.object(product_usage, 'frappe', site), patch(
                    'hero_integration.client.settings.get_configuration',
                    return_value=NS(source_instance='dev', source_binding_revision=3)):
                self.assertEqual(product_usage.load('dev'), (digest, {'001': 9}))
                with self.assertRaises(ValueError):
                    product_usage.load('other')
                csv_path.write_bytes(raw + b'002;1\n')
                with self.assertRaises(ValueError):
                    product_usage.load('dev')

    def test_reorders_complete_page_and_rejects_incomplete_or_duplicate_answers(self):
        read = direct_runtime.builtin_read('HeroProducts')
        with patch.object(direct_runtime, 'fetch_page') as fetch:
            fetch.return_value = [{'product_id': 'b'}, {'product_id': 'a'}]
            rows = product_usage.page(None, read, ['a', 'b'], 0, 2)
            self.assertEqual([r['product_id'] for r in rows], ['a', 'b'])
            self.assertIn('product_ids:', fetch.call_args.args[1].document)
            for rows in [[{'product_id': 'b'}], [{'product_id': 'b'}]*2,
                         [{'product_id': 'b'}, {'product_id': 'x'}]]:
                fetch.return_value = rows
                with self.assertRaises(ValueError):
                    product_usage.page(None, read, ['a', 'b'], 0, 2)

    def test_customer_checkpoint_requires_same_full_run_and_reader(self):
        scope = dict(version=1, site='test', actor='a', mode='Execute', source_instance='s', source_binding_revision=2)
        reader = dict(service='client', reader='proof', field_rules='v1')
        run = NS(name='project', action='Project Migration', mode='Execute', testing_enabled=False,
                 execution_budget_run=None, resume_offset=0, resume_record_index=0, customer_phase_state='Queued',
                 customer_service='client', checkpoint_contract_json=json.dumps(dict(scope, readers=[reader, {}])))
        prior = NS(action='Customer Migration', state='Completed With Conflicts', testing_enabled=False,
                   execution_budget_run=None, customer_phase_state='Completed',
                   checkpoint_contract_json=json.dumps(dict(scope, readers=[reader])))
        with patch.object(file_migration, '_parent_run', return_value='root') as parent, patch.object(
                file_migration, '_full', return_value=dict(wave_children={'Project Migration':'project'},
                                                          completed_children=['customers'])), patch.object(
                migration, 'frappe', NS(get_doc=Mock(return_value=prior))):
            self.assertEqual(migration._completed_customer_sibling(run), 'customers')
            parent.side_effect = lambda n: 'other' if n=='customers' else 'root'
            self.assertIsNone(migration._completed_customer_sibling(run))
            parent.side_effect = None
            prior.checkpoint_contract_json = json.dumps(dict(scope, readers=[dict(reader, field_rules='v2')]))
            self.assertIsNone(migration._completed_customer_sibling(run))
