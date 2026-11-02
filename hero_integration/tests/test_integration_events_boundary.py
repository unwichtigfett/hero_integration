"""Offline coverage for the existing administrative output boundary."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hero_integration.services import integration_events


class TestIntegrationOutputBoundary(unittest.TestCase):
    def test_record_output_does_not_commit_or_publish_before_caller_transaction(self):
        document = SimpleNamespace(name="synthetic-result", insert=Mock())
        fake = SimpleNamespace(get_doc=Mock(return_value=document),
                               db=SimpleNamespace(commit=Mock()), publish_realtime=Mock())
        with patch.object(integration_events, "frappe", fake), \
             patch.object(integration_events, "now_datetime", return_value="synthetic-time"):
            name = integration_events.record_output(source_type="Contact", source_id="1", result="OBSERVED",
                                                    action="review", changed_fields=["status", "name"])
        self.assertEqual(name, "synthetic-result")
        self.assertEqual(fake.get_doc.call_args.args[0]["changed_fields"], "name, status")
        document.insert.assert_called_once_with(ignore_permissions=True)
        fake.db.commit.assert_not_called()
        fake.publish_realtime.assert_not_called()

    def test_failed_output_insert_does_not_commit_or_publish(self):
        document = SimpleNamespace(insert=Mock(side_effect=RuntimeError("synthetic insert failure")))
        fake = SimpleNamespace(get_doc=Mock(return_value=document),
                               db=SimpleNamespace(commit=Mock()), publish_realtime=Mock())
        with patch.object(integration_events, "frappe", fake), \
             patch.object(integration_events, "now_datetime", return_value="synthetic-time"):
            with self.assertRaisesRegex(RuntimeError, "synthetic insert failure"):
                integration_events.record_output(source_type="Contact", source_id="1", result="OBSERVED",
                                                 action="review")
        document.insert.assert_called_once_with(ignore_permissions=True)
        fake.db.commit.assert_not_called()
        fake.publish_realtime.assert_not_called()

    def test_non_manager_cannot_read_output_records(self):
        query = Mock()
        fake = SimpleNamespace(
            get_roles=lambda: ["Employee"], PermissionError=PermissionError, _=lambda message: message,
            throw=lambda message, error: (_ for _ in ()).throw(error(message)), get_all=query,
        )
        with patch.object(integration_events, "frappe", fake), self.assertRaises(PermissionError):
            integration_events.list_outputs()
        query.assert_not_called()

    def test_manager_receives_bounded_projection_without_detail_values(self):
        query = Mock(return_value=[{"name": "synthetic"}])
        fake = SimpleNamespace(get_roles=lambda: ["System Manager"], get_all=query)
        with patch.object(integration_events, "frappe", fake):
            result = integration_events.list_outputs(limit_start=-10, limit_page_length=500)
        self.assertEqual((result["limit_start"], result["limit_page_length"]), (0, 100))
        self.assertEqual(result["records"], [{"name": "synthetic"}])
        fields = query.call_args.kwargs["fields"]
        self.assertNotIn("detail", fields)
        self.assertNotIn("source_payload", fields)

    def test_supplied_changed_field_names_are_sorted_and_deduplicated(self):
        self.assertEqual(integration_events.redacted_changed_fields(["name", "status", "name", ""]),
                         "name, status")

    def test_changed_fields_formatter_does_not_validate_or_redact_input(self):
        self.assertEqual(integration_events.redacted_changed_fields(["synthetic value"]), "synthetic value")
