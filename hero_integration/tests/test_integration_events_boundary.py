"""Offline coverage for the existing administrative output boundary."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hero_integration.services import integration_events


class TestIntegrationOutputBoundary(unittest.TestCase):
    def test_non_manager_cannot_read_output_records(self):
        query = Mock()
        fake = SimpleNamespace(
            get_roles=lambda: ["Employee"], PermissionError=PermissionError,
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
