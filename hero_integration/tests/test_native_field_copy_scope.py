"""One-time native field copies require exact source, rule and baseline evidence."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.integrations import native_field_moves as moves
from hero_integration.services.change_detection import field_hashes


class Row(dict):
    __getattr__ = dict.get


class TestNativeFieldCopyScope(unittest.TestCase):
    def setup_move(self, doctype='Customer', target='customer_type', raw='commercial', current='Individual'):
        config = {
            'customer_type': ('customer', 'custom_hero_customer_type', 'type', 'Customer Type From HERO Type'),
            'notes': ('project', 'custom_hero_partner_notes', 'partner_notes', 'Plain Text To HTML'),
            'color': ('CalendarEvent', 'custom_hero_color', 'color', 'Identity'),
            'status': ('CalendarEvent', 'custom_hero_is_done', 'is_done', 'Native Adapter'),
        }
        kind, self.legacy, path, transform = config[target]
        old = SimpleNamespace(target_doctype=doctype, target_field=self.legacy, source_path=path,
                              transformation='Scalar Text' if target == 'status' else 'Trim')
        new = SimpleNamespace(target_doctype=doctype, target_field=target, source_path=path, transformation=transform)
        self.before = dict(name='mapping', source='source-a', snapshot='snapshot', revision=1, version='old',
                           plan=SimpleNamespace(definitions=[old]))
        self.after = {**self.before, 'revision': 2, 'version': 'new', 'plan': SimpleNamespace(definitions=[new])}
        self.doc = Row(name='target', **{self.legacy: raw, target: current})
        self.link = Row(name='link', source_system='HERO', source_instance='source-a', source_type=kind,
                        target_doctype=doctype, target_name='target', sync_status='Synchronized', mapping_version='old',
                        target_field_hashes=json.dumps(field_hashes({self.legacy: raw, target: current})))
        self.owners = ['link']
        # Return the candidate even when filtered out: the locked-document
        # recheck must independently reject stale status/source observations.
        self.fake = SimpleNamespace(get_all=Mock(side_effect=lambda dt, **kw:
            self.owners if 'target_name' in kw['filters'] else ['link']),
            get_doc=Mock(side_effect=lambda dt, name, **kw: self.link if dt == 'HERO Integration Link' else self.doc),
            db=SimpleNamespace(has_column=lambda *a: True, exists=lambda *a: True, set_value=Mock()))

    def copy(self, current=None):
        with patch.object(moves, 'frappe', self.fake), patch.object(moves, '_copy_plan', return_value=current or self.after):
            return moves.copy_values([{'before': self.before, 'after': self.after}])

    def test_proven_unchanged_customer_value_is_copied_without_changing_legacy_data(self):
        self.setup_move()
        result = self.copy()
        self.assertEqual(result['customer_type'], 1)
        self.fake.db.set_value.assert_called_once_with('Customer', 'target', 'customer_type', 'Company', update_modified=False)
        self.assertEqual(self.doc[self.legacy], 'commercial')

    def test_rejected_pending_conflicted_deleted_or_foreign_records_are_never_copied(self):
        for key, value in [('sync_status', 'Rejected'), ('sync_status', 'Pending Review'),
                           ('sync_status', 'Conflict'), ('sync_status', 'Source Deleted'),
                           ('source_instance', 'source-b'), ('mapping_version', 'unrelated')]:
            with self.subTest(key=key, value=value):
                self.setup_move()
                self.link[key] = value
                self.copy()
                self.fake.db.set_value.assert_not_called()

    def test_local_native_or_source_mirror_edit_prevents_copy(self):
        for field, value in [('customer_type', 'Company'), ('custom_hero_customer_type', 'private')]:
            self.setup_move()
            self.doc[field] = value
            self.copy()
            self.fake.db.set_value.assert_not_called()

    def test_missing_native_baseline_or_multiple_bindings_are_not_inferred(self):
        self.setup_move()
        self.link['target_field_hashes'] = json.dumps(field_hashes({self.legacy: 'commercial'}))
        self.copy()
        self.fake.db.set_value.assert_not_called()
        self.setup_move()
        self.owners = ['link', 'foreign-link']
        self.copy()
        self.fake.db.set_value.assert_not_called()

    def test_all_other_copy_branches_obey_rejection_guard(self):
        for doctype, target, raw, current in [('Project', 'notes', 'Note', ''),
                                             ('Event', 'color', '#ffffff', ''),
                                             ('Event', 'status', 'true', 'Open')]:
            with self.subTest(target=target):
                self.setup_move(doctype, target, raw, current)
                self.link['sync_status'] = 'Rejected'
                self.copy()
                self.fake.db.set_value.assert_not_called()

    def test_proven_notes_color_and_status_conversions_are_preserved(self):
        for doctype, target, raw, current, wanted in [('Project', 'notes', 'A < B', '', '<p>A &lt; B</p>'),
                ('Event', 'color', '#ffffff', '', '#ffffff'), ('Event', 'status', 'true', 'Open', 'Completed')]:
            with self.subTest(target=target):
                self.setup_move(doctype, target, raw, current)
                self.assertEqual(self.copy()[target], 1)
                self.fake.db.set_value.assert_called_once_with(doctype, 'target', target, wanted, update_modified=False)

    def test_changed_mapping_or_wrong_selected_rule_cannot_authorize_copy(self):
        self.setup_move()
        self.copy(current={**self.after, 'revision': 3})
        self.fake.db.set_value.assert_not_called()
        self.after['plan'].definitions[0].source_path = 'unrelated'
        self.copy()
        self.fake.db.set_value.assert_not_called()

    def test_no_current_run_evidence_performs_no_queries_or_writes(self):
        fake = SimpleNamespace(get_all=Mock(), db=SimpleNamespace(set_value=Mock()))
        with patch.object(moves, 'frappe', fake):
            self.assertEqual(sum(moves.copy_values().values()), 0)
        fake.get_all.assert_not_called()
        fake.db.set_value.assert_not_called()

    def test_transformed_legacy_mirror_is_not_invented_source_evidence(self):
        self.setup_move()
        self.before['plan'].definitions[0].transformation = 'Value Map'
        self.copy()
        self.fake.db.set_value.assert_not_called()

    def test_inactive_or_mutation_mapping_cannot_grant_copy_scope(self):
        for status, direction in [('Retired', 'QUERY_TO_FRAPPE'), ('Active', 'FRAPPE_TO_MUTATION')]:
            with self.subTest(status=status, direction=direction):
                fake = SimpleNamespace(get_doc=Mock(return_value=Row(status=status, direction=direction)))
                with patch.object(moves, 'frappe', fake), \
                     patch('hero_integration.services.mapping_runtime.active_mapping_plan') as resolve:
                    self.assertIsNone(moves._copy_plan('mapping'))
                resolve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
