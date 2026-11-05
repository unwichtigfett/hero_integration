"""Unresolved assignees surface for review after the other Project import work."""
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import migration


class TestProjectAssignmentReview(TestCase):
    def run_project(self, unmatched, code='UPDATED'):
        plan = NS(source_identity=lambda record: '7', mapping_version='version', adapter_options={'version': 2},
                  values=lambda *args: {'project_name': 'Project', 'customer': 'Customer'})
        original = migration.Result(code, 'Native fields synchronized.', 'Project', 'P1')
        steps = []
        pending = Mock()
        relations = Mock(side_effect=lambda plan, record, result, run: steps.append('relations') or result)
        logbook = Mock(side_effect=lambda *args, **kwargs: steps.append('logbook'))
        with ExitStack() as stack:
            for owner in (
                patch.object(migration, 'frappe', NS(_=lambda text: text)),
                patch.object(migration, '_target_available', return_value=True),
                patch.object(migration, '_mapping_plan_for_run', return_value=plan),
                patch.object(migration, '_link', return_value=None),
                patch.object(migration, '_document_name', return_value=('P1', '')),
                patch.object(migration, '_hold_customer_only_title_change', return_value={}),
                patch.object(migration, '_business_number_drift', return_value=None),
                patch.object(migration, '_upsert', return_value=original),
                patch.object(migration, '_sync_mapped_relations', relations),
                patch.object(migration, 'record_output', pending),
                patch('hero_integration.integrations.project_assignments.sync',
                      return_value={'added': 1, 'closed': 0, 'unmatched': unmatched}),
                patch('hero_integration.services.project_logbook.sync_mapped_entries', logbook),
            ):
                stack.enter_context(owner)
            result = migration._migrate_project({'id': 7, '_hero_source_instance': 'source-a',
                                                'project_match_assignments': [{'user_id': 70}]}, NS(mode='Execute', name='RUN'))
        self.assertEqual(steps, ['logbook', 'relations'])
        self.assertEqual(relations.call_args.args[2].code, code)
        logbook.assert_called_once()
        self.assertEqual(result.target_name, 'P1')
        return result, pending

    def test_unresolved_assignees_are_reported_separately_from_project_import(self):
        for code in ('CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED'):
            with self.subTest(code=code):
                result, pending = self.run_project(2, code)
                self.assertEqual(result.code, code)
                self.assertEqual(pending.call_args.kwargs['result'], 'PENDING_REVIEW')
                self.assertIn('2 project assignee(s)', pending.call_args.kwargs['detail'])
                self.assertIn('existing assignments were preserved and require review', pending.call_args.kwargs['detail'])
                self.assertIn('Native fields synchronized.', result.detail)

    def test_resolved_assignees_preserve_normal_project_result(self):
        result, pending = self.run_project(0)
        pending.assert_not_called()
        self.assertEqual(result.code, 'UPDATED')
        self.assertEqual(result.detail, 'Native fields synchronized.')
