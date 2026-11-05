"""A bounded Project import must never trigger the legacy all-project logbook pass."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import migration
from hero_integration.services.migration_testing import RecordLimitedSource


class TestProjectLogbookTestLimit(unittest.TestCase):
    def setUp(self):
        self.source = SimpleNamespace(client=Mock(), source_instance='source-a',
                                      user_emails=Mock(return_value={7: 'synthetic@example.invalid'}),
                                      project_status_steps=Mock())

    def test_bounded_execute_wrapper_does_not_expose_global_logbook_writes(self):
        for maximum in (1, 5):
            with self.subTest(maximum=maximum):
                run = SimpleNamespace(mode='Execute', testing_enabled=1, testing_max_actions=maximum)
                source = RecordLimitedSource(self.source, maximum)
                self.assertIs(source.client, self.source.client)
                with patch('hero_integration.services.project_logbook.sync_logbook') as sync:
                    migration._refresh_project_logbook(run, source)
                sync.assert_not_called()
        self.source.user_emails.assert_not_called()
        self.source.client.query.assert_not_called()

    def test_preview_keeps_its_no_read_no_write_boundary(self):
        for testing in (0, 1):
            with self.subTest(testing=testing), patch('hero_integration.services.project_logbook.sync_logbook') as sync:
                migration._refresh_project_logbook(SimpleNamespace(mode='Preview', testing_enabled=testing), self.source)
                sync.assert_not_called()
        self.source.user_emails.assert_not_called()

    def test_unrestricted_execute_retains_legacy_logbook_import(self):
        with patch('hero_integration.services.project_logbook.sync_logbook') as sync:
            migration._refresh_project_logbook(SimpleNamespace(mode='Execute', testing_enabled=0), self.source)
        sync.assert_called_once_with(self.source.client, {7: 'synthetic@example.invalid'}, source_instance='source-a')

    def test_mapped_project_histories_do_not_also_run_global_refresh(self):
        self.source.query_adapter = {'version': 2}
        with patch('hero_integration.services.project_logbook.sync_logbook') as sync:
            migration._refresh_project_logbook(SimpleNamespace(mode='Execute', testing_enabled=0), self.source)
        sync.assert_not_called()
        self.source.user_emails.assert_not_called()


if __name__ == '__main__':
    unittest.main()
