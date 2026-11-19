"""Employee provisioning belongs to manual migration, including old queued hook paths."""
import unittest
from unittest.mock import patch

from hero_integration import api
from hero_integration.integrations import hero_people
from hero_integration.patches.v0_3 import hero_people_users


class TestPeopleEntrypoints(unittest.TestCase):
    def test_old_daily_job_and_upgrade_do_not_provision_users(self):
        with patch.object(api, "sync_area_start") as start, patch.object(hero_people, "frappe") as framework:
            hero_people.sync_daily()
            hero_people_users.execute()
        start.assert_not_called()
        self.assertEqual(framework.mock_calls, [])

    def test_manual_rpc_returns_the_canonical_employee_run(self):
        with patch.object(api, "sync_area_start", return_value={"status": "QUEUED", "run":"EMP-1"}) as start:
            self.assertEqual(hero_people.sync_hero_people()["run"], "EMP-1")
        start.assert_called_once_with("employees")
