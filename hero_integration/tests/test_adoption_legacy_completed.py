import copy
import json
import unittest
from unittest.mock import patch

from hero_integration.services import adoption, schema_provisioning
from hero_integration.tests.test_schema_provisioning import _Frappe


class TestLegacyCompletedRead(unittest.TestCase):
    def setUp(self):
        self.f = _Frappe()
        p = patch.object(adoption, "frappe", self.f)
        p.start()
        self.addCleanup(p.stop)
        self.row = {"name": "legacy", "doctype": "HERO Mapping Adoption", "owner": self.f.session.user,
                    "selected_ids_json": json.dumps(["mapping:old"]), "status": "Completed",
                    "outcome_json": json.dumps({"status": "COMPLETED", "created": {"mappings": ["old"]}})}
        self.f.adoptions["legacy"] = self.row

    def test_owner_reads_historical_outcome_without_mutation_or_execution(self):
        before = copy.deepcopy(self.row)
        with patch.object(adoption, "adopt_package", side_effect=AssertionError("mutation")), \
             patch.object(adoption, "revalidate_adoption_preview", side_effect=AssertionError("preview")):
            result = adoption.execute("legacy")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["confirmation_binding"], "LEGACY_UNVERIFIED")
        self.assertEqual(result["adoption_name"], "legacy")
        self.assertEqual(self.row, before)
        self.assertEqual(self.f.mutations, [])

    def test_foreign_and_non_manager_rejected_before_read(self):
        self.row["owner"] = "someone-else"
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "Only the adoption owner"):
            adoption.execute("legacy")
        self.row["owner"] = self.f.session.user
        with patch.object(self.f, "only_for", side_effect=PermissionError("role")):
            with self.assertRaises(PermissionError):
                adoption.execute("legacy")
        self.assertEqual(self.f.mutations, [])

    def test_queued_legacy_still_fails_closed_and_remains_unchanged(self):
        self.row["status"] = "Queued"
        before = copy.deepcopy(self.row)
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "Legacy adoption requires a new preview"):
            adoption.execute("legacy")
        self.assertEqual(self.row, before)
        self.assertEqual(self.f.mutations, [])

    def test_forged_new_format_is_not_treated_as_legacy(self):
        self.row["selected_ids_json"] = json.dumps({"ids": [], "preview": {}, "binding": "bad"})
        before = copy.deepcopy(self.row)
        self.row.update(schema_snapshot="SYNTHETIC", package_json="{}")
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "confirmation is invalid"):
            adoption.execute("legacy")
        self.assertEqual(self.row["status"], before["status"])
        self.assertEqual(self.f.mutations, [])
