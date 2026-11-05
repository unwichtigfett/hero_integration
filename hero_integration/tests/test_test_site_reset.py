"""Exercise the host updater's reset, revision and failure boundaries offline."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("test_site_updater", ROOT / "ci/frappe-test-update.py")
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)
RESET_SPEC = importlib.util.spec_from_file_location("reset_test_site", ROOT / "ci/reset-test-site.py")
resetter = importlib.util.module_from_spec(RESET_SPEC)
RESET_SPEC.loader.exec_module(resetter)
REVISION = "a" * 40


class TestTestSiteReset(unittest.TestCase):
    def run_update(self, *, command=None, reset_error=None, revision=REVISION):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site = root / "sites/frappe-test"
            site.mkdir(parents=True)
            config = site / "site_config.json"
            config.write_text(json.dumps({"db_name": "test_database"}))
            common = root / "sites/common_site_config.json"
            common.write_text("{}")
            apps = root / "sites/apps.txt"
            apps.write_text("frappe\nerpnext\nhero_integration\n")
            envfile = root / ".env"
            envfile.write_text("FRAPPE_TEST_IMAGE=frappe-test-apps:" + REVISION[:20] + "\n")
            env = dict(updater.ENV, SSH_ORIGINAL_COMMAND=(
                f"update:blank:{REVISION}" if command is None else command
            ))
            bench = Mock(side_effect=lambda *args, **kwargs:
                         "frappe 16\nerpnext 16\nhero_integration 1\n" if args == ("list-apps",) else "{}")
            git_reads = iter((REVISION, revision))

            def run(args, **kwargs):
                if args[-2:] == ["rev-parse", "HEAD"]:
                    return next(git_reads)
                if "--format" in args:
                    return "sha256:installed-image"

            reset = Mock(side_effect=reset_error)
            with patch.multiple(updater, ROOT=root, SITE=site, ENVFILE=envfile, APPSFILE=apps,
                                CONFIG=config, COMMON=common, ENV=env,
                                FAILURE_STATE=root / "retry.json", LOCKFILE=root / "lock", log=None, backup=None,
                                maintenance=False, stopped=False, switched=False,
                                migration_started=False, old_env=None), \
                 patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                 patch.object(updater, "head", return_value=revision), \
                 patch.object(updater, "run", side_effect=run), \
                 patch.object(updater, "bench", bench), \
                 patch.object(updater, "dc") as compose, \
                 patch.object(updater, "backup_site") as backup_site, \
                 patch.object(updater, "backup_data") as backup_data, \
                 patch.object(updater, "build_image") as build, \
                 patch.object(updater, "reset_database", reset), \
                 patch.object(updater, "atomic_write"), \
                 patch.object(updater, "say"):
                status = 0
                try:
                    updater.main()
                except SystemExit as exc:
                    status = exc.code
                return status, bench, compose, backup_site, backup_data, build, reset

    def test_repeated_ci_commit_still_resets_without_any_backup_or_image_build(self):
        status, bench, compose, backup_site, backup_data, build, reset = self.run_update()
        self.assertEqual(status, 0)
        reset.assert_called_once_with()
        backup_site.assert_not_called()
        backup_data.assert_not_called()
        build.assert_not_called()
        self.assertNotIn(unittest.mock.call("migrate", timeout=1200), bench.call_args_list)
        bench.assert_any_call("set-maintenance-mode", "off")
        compose.assert_any_call("stop", *updater.SERVICES, timeout=180)

    def test_new_revision_builds_the_app_and_resets_without_backups(self):
        revision = "b" * 40
        status, bench, compose, backup_site, backup_data, build, reset = self.run_update(
            command=f"update:blank:{revision}", revision=revision
        )
        self.assertEqual(status, 0)
        build.assert_called_once_with("frappe-test-apps:" + REVISION[:20], revision,
                                      "frappe-test-apps:" + revision[:20])
        reset.assert_called_once_with()
        backup_site.assert_not_called()
        backup_data.assert_not_called()

    def test_timer_skips_an_unchanged_commit_and_does_not_reset(self):
        status, bench, compose, backup_site, backup_data, build, reset = self.run_update(command="")
        self.assertEqual(status, 0)
        reset.assert_not_called()
        backup_site.assert_not_called()
        backup_data.assert_not_called()
        compose.assert_not_called()

    def test_legacy_deployed_pipeline_also_resets_without_backups(self):
        status, bench, compose, backup_site, backup_data, build, reset = self.run_update(command="update")
        self.assertEqual(status, 0)
        reset.assert_called_once_with()
        backup_site.assert_not_called()
        backup_data.assert_not_called()

    def test_old_pipeline_cannot_reset_a_newer_revision(self):
        status, bench, compose, backup_site, backup_data, build, reset = self.run_update(revision="b" * 40)
        self.assertEqual(status, 1)
        reset.assert_not_called()
        compose.assert_not_called()
        backup_site.assert_not_called()
        backup_data.assert_not_called()

    def test_failed_reset_keeps_maintenance_and_workers_stopped(self):
        status, bench, compose, backup_site, backup_data, build, reset = self.run_update(
            reset_error=RuntimeError("synthetic install failure")
        )
        self.assertEqual(status, 1)
        self.assertNotIn(unittest.mock.call("set-maintenance-mode", "off"), bench.call_args_list)
        self.assertFalse(any(call.args[:3] == ("up", "-d", "--no-deps") and
                             "frontend" in call.args for call in compose.call_args_list))
        backup_site.assert_not_called()
        backup_data.assert_not_called()

    def test_only_an_exact_ci_revision_can_request_a_reset(self):
        for command in ("update:blank", "update:blank:" + "a" * 39, "update:blank:" + REVISION + ";id"):
            with self.subTest(command=command), self.assertRaises(RuntimeError):
                updater.update_request(command)

    def test_pipeline_passes_its_exact_commit_to_the_restricted_updater(self):
        self.assertIn('"update:blank:$$expected"', (ROOT / ".woodpecker.yml").read_text())

    def test_identity_snapshot_excludes_api_keys_and_secrets(self):
        frappe = Mock()
        frappe.get_meta.return_value.get_table_fields.return_value = []

        def sql(query, **kwargs):
            if "tabUser" in query:
                return [{"name": name, "api_key": "synthetic-api-key", "api_secret": "masked"}
                        for name in ("Administrator", "Guest")]
            if "__Auth" in query:
                self.assertIn("fieldname = 'password' AND encrypted = 0", query)
                return [{"name": "Administrator", "fieldname": "password", "password": "synthetic-hash"}]
            return []

        frappe.db.sql.side_effect = sql
        rows, auth = resetter.capture_identities(frappe)
        for user in rows["User"]:
            self.assertIsNone(user["api_key"])
            self.assertIsNone(user["api_secret"])
        self.assertEqual([row["fieldname"] for row in auth], ["password"])

    def test_reset_clears_only_the_test_queue_and_sends_db_credentials_on_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            envfile = Path(tmp) / ".env"
            envfile.write_text("DB_ROOT_PASSWORD=synthetic-db-password\n")
            with patch.object(updater, "ENVFILE", envfile), patch.object(updater, "run") as run:
                updater.reset_database()
            self.assertEqual(run.call_args_list[0].args[0], [
                "docker", "exec", "frappe-test-redis-queue-1", "redis-cli", "FLUSHDB"
            ])
            invocation = run.call_args_list[1]
            self.assertNotIn("synthetic-db-password", " ".join(invocation.args[0]))
            self.assertEqual(json.loads(invocation.kwargs["input_text"]), {
                "site": "frappe-test", "db_root_password": "synthetic-db-password"
            })

    def test_restore_retains_new_installer_roles_and_does_not_reintroduce_api_keys(self):
        frappe = Mock()
        frappe.db.exists.side_effect = lambda doctype, name: doctype == "Role"
        frappe.get_all.return_value = ["Administrator", "Guest"]
        frappe.db.sql.return_value = []
        rows = {"Role": [{"name": "Existing Role"}], "Role Profile": [], "Module Profile": [],
                "User": [{"name": name, "api_key": "discard", "api_secret": "discard"}
                         for name in ("Administrator", "Guest")],
                "Has Role": [{"name": "user-role", "parenttype": "User", "parent": "Administrator",
                              "role": "Existing Role"}]}
        resetter.restore_identities(frappe, rows, [])
        deleted_parents = [call.args[0] for call in frappe.db.delete.call_args_list]
        self.assertNotIn("Role", deleted_parents)
        self.assertNotIn("Role Profile", deleted_parents)
        self.assertNotIn("Module Profile", deleted_parents)
        restored_users = [call.args[0] for call in frappe.get_doc.call_args_list
                          if call.args[0]["doctype"] == "User"]
        self.assertEqual(len(restored_users), 2)
        self.assertTrue(all(user["api_key"] is None and user["api_secret"] is None
                            for user in restored_users))


if __name__ == "__main__":
    unittest.main()
