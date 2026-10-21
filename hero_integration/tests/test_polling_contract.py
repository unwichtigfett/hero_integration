from __future__ import annotations

import unittest
from types import SimpleNamespace

from hero_integration.services.polling import effective_mode, monitor
from hero_integration.sources.hero import SourcePage


class _Source:
    source_instance = "synthetic"

    @staticmethod
    def customers(offset):
        return SourcePage(({"id": 1}, {"id": 2}) if offset == 0 else ({"id": 3},), 2 if offset == 0 else None)

    @staticmethod
    def projects(offset):
        return SourcePage(({"id": 4},), None)


class TestPollingContract(unittest.TestCase):
    def test_fresh_mode_is_off_and_legacy_boolean_is_explicitly_compatible(self):
        self.assertEqual(effective_mode(SimpleNamespace(polling_mode="OFF", scheduled_sync_enabled=1)), "OFF")
        self.assertEqual(effective_mode(SimpleNamespace(polling_mode="", scheduled_sync_enabled=0)), "OFF")
        self.assertEqual(effective_mode(SimpleNamespace(polling_mode="", scheduled_sync_enabled=1)), "AUTO_SYNC")

    def test_monitor_reads_pages_without_claiming_watermarks_or_business_writes(self):
        result = monitor(_Source())
        self.assertEqual((result.customer_records, result.project_records, result.source_instance), (3, 1, "synthetic"))
