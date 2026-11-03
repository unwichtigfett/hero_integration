from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import mapping_deployment_repair as repair


class TestMappingDeploymentRepair(unittest.TestCase):
    def test_calendar_correction_preserves_collection_and_is_idempotent(self):
        old = {"mapping_definition_ids": ["color", "done", "partner", "partners"], "field_rules": {
            "color": {"source_path": "color", "target_field": "custom_hero_color"},
            "done": {"source_path": "is_done", "target_field": "custom_hero_is_done"},
            "partner": {"source_path": "partner_id", "target_field": "custom_hero_partner_id"},
            "partners": {"source_path": "partners", "target_field": "custom_hero_partners"}}}
        original = deepcopy(old)
        fixed = repair.calendar_content(old)
        self.assertEqual(old, original)
        self.assertEqual(fixed["mapping_definition_ids"], ["color", "done", "partners"])
        self.assertEqual(fixed["field_rules"]["done"]["transformation"], "Native Adapter")
        self.assertEqual(fixed["field_rules"]["color"]["target_field"], "color")
        self.assertEqual(repair.calendar_content(fixed), fixed)

    def test_receipt_does_not_renew_a_later_revoked_approval(self):
        fake = SimpleNamespace(local=SimpleNamespace(site="target"), db=SimpleNamespace(exists=lambda *a: True))
        with patch.object(repair, "frappe", fake), patch.object(repair, "repair_readers") as readers:
            self.assertEqual(repair.repair({"target_site": "target"})["status"], "ALREADY_REPAIRED")
        readers.assert_not_called()

    def test_repair_failure_rolls_back_cleanup_and_restores_actor(self):
        fake = SimpleNamespace(local=SimpleNamespace(site="target"), session=SimpleNamespace(user="operator"),
            db=SimpleNamespace(exists=lambda *a: False, savepoint=Mock(), rollback=Mock()), set_user=Mock())
        with patch.object(repair, "frappe", fake), \
             patch.object(repair, "repair_readers", side_effect=ValueError("invalid reader")):
            with self.assertRaisesRegex(ValueError, "invalid reader"):
                repair.repair({"target_site": "target"})
        fake.db.rollback.assert_called_once_with(save_point="hero_mapping_repair")
        self.assertEqual(fake.set_user.call_args.args, ("operator",))



if __name__ == "__main__":
    unittest.main()
