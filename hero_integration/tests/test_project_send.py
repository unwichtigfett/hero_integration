"""Send to HERO: Notes and Phase of an imported Project (owner decision 2026-11-01)."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import project_send as ps
from hero_integration.services.change_detection import decode, encode, field_hashes

SCHEMA = json.dumps({"data": {"__schema": {"types": [
    {"kind": "INPUT_OBJECT", "name": "ProjectMatchInput",
     "inputFields": [{"name": n} for n in ("id", "partner_notes", "current_project_match_status", "step_id")]}]}}})


class _Hero:
    """In-memory HERO project; phases are the steps of its project type, statuses its history."""

    # HERO orders by status code, then sort order, then ID.
    STEPS = [{"id": 2, "name": "Montage", "sort_order": 0, "status_code": 700, "is_active": True},
             {"id": 1, "name": "Angebot", "sort_order": 5, "status_code": 100, "is_active": True},
             {"id": 3, "name": "Alt", "sort_order": 0, "status_code": 50, "is_active": False}]

    def __init__(self, notes="Old note", step=1):
        self.row = {"id": 501, "partner_notes": notes}
        self.step = step
        self.mutations = []

    def _status(self):
        name = next(s["name"] for s in self.STEPS if s["id"] == self.step)
        return {"id": 700 + self.step, "name": "Status group", "step_id": self.step, "step": {"name": name}}

    def query(self, document, variables):
        status = self._status()
        return {"project_match": {**self.row, "current_project_match_status_id": status["id"],
                                  "current_project_match_status": status,
                                  "type": {"project_status_steps": self.STEPS}}}

    def execute_once(self, document, variables):
        self.mutations.append(variables)
        values = variables["project_match"]
        if "partner_notes" in values:
            self.row["partner_notes"] = values["partner_notes"]
        if "current_project_match_status" in values:
            self.step = values["current_project_match_status"]["step_id"]
        # The client returns the GraphQL "data" object itself.
        return {"update_project_match": {"id": 501, "partner_notes": self.row["partner_notes"],
                                         "current_project_match_status": {"step_id": self.step,
                                                                          "step": {"name": self._status()["step"]["name"]}}}}


class _Frappe:
    def __init__(self, local_notes="Old note", baseline_notes="Old note", roles=("HERO Writeback",)):
        self.roles = roles
        self.project = {"name": "PV-1-H", ps.NOTES_FIELD: local_notes}
        hashes = encode(field_hashes({ps.NOTES_FIELD: baseline_notes}))
        self.link = SimpleNamespace(name="LINK-1", source_id="501", source_instance="default",
                                    sync_status="Synchronized", source_field_hashes=hashes, target_field_hashes=hashes)
        self.set_values = []
        self.db = SimpleNamespace(set_value=lambda doctype, name, values, update_modified=True:
                                  self.set_values.append((doctype, values)))

    def get_roles(self):
        return list(self.roles)

    def get_doc(self, doctype, name):
        if doctype == "Project":
            return SimpleNamespace(get=self.project.get)
        return SimpleNamespace(snapshot_json=SCHEMA, schema_fingerprint="fp")

    def has_permission(self, *args, **kwargs):
        return True

    def get_all(self, doctype, **kwargs):
        return [self.link] if doctype == "HERO Integration Link" else ["SNAP-1"]


class TestProjectSend(unittest.TestCase):
    def _env(self, fake, enabled=True):
        import hero_integration.api as api
        audits = []
        patches = [
            patch.object(ps, "frappe", fake),
            patch.object(api, "_mutation_execution_state", return_value={"mutation_execution_enabled": enabled}),
            patch.object(api, "_assert_mutation_execution_released",
                         side_effect=None if enabled else RuntimeError("HERO_MUTATION_EXECUTION_DISABLED")),
            patch.object(api, "_reserve_mutation", side_effect=lambda **kw: audits.append(["reserved"]) or
                         SimpleNamespace(correlation_id="C-1")),
            patch.object(api, "_finalize_mutation", side_effect=lambda audit, status, detail: audits.append([status])),
            patch("hero_integration.client.settings.get_configuration",
                  return_value=SimpleNamespace(source_instance="default")),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return audits

    def test_preview_shows_local_notes_and_a_chosen_phase(self):
        fake, hero = _Frappe(local_notes="New note"), _Hero()
        self._env(fake)
        result = ps.preview("PV-1-H", phase_id=2, client=hero)
        self.assertEqual([p["name"] for p in result["phases"]], ["Angebot", "Montage"])  # active, HERO order
        self.assertEqual([c["label"] for c in result["changes"]], ["Notes", "Phase"])
        self.assertEqual((result["changes"][1]["hero"], result["changes"][1]["new"]), ("Angebot", "Montage"))
        self.assertTrue(result["can_send"])
        self.assertEqual(hero.mutations, [])

    def test_hero_change_since_import_is_a_conflict_not_overwritten(self):
        fake, hero = _Frappe(local_notes="Local", baseline_notes="Old note"), _Hero(notes="Changed in HERO")
        self._env(fake)
        result = ps.preview("PV-1-H", client=hero)
        self.assertEqual(result["conflicts"][0]["label"], "Notes")
        self.assertFalse(result["can_send"])

    def test_only_allowed_roles_and_known_phases(self):
        self._env(_Frappe(roles=("Projects User",)))
        with self.assertRaises(ps.ProjectSendError):
            ps.preview("PV-1-H", client=_Hero())
        self._env(_Frappe())
        with self.assertRaises(ps.ProjectSendError):
            ps.preview("PV-1-H", phase_id=3, client=_Hero())  # inactive step

    def test_send_once_verifies_and_advances_the_baseline(self):
        fake, hero = _Frappe(local_notes="New note"), _Hero()
        audits = self._env(fake)
        fingerprint = ps.preview("PV-1-H", phase_id=2, client=hero)["fingerprint"]
        result = ps.send("PV-1-H", 2, fingerprint, 1, client=hero, mutation_client=hero)
        self.assertEqual(result["changes"], ["Notes", "Phase"])
        self.assertEqual(hero.mutations, [{"project_match": {"id": 501, "partner_notes": "New note",
                                                             "current_project_match_status": {"step_id": 2}}}])
        self.assertEqual(audits, [["reserved"], ["Succeeded"]])
        project_update = next(values for doctype, values in fake.set_values if doctype == "Project")
        self.assertEqual(project_update["custom_hero_phase"], "Montage")  # the step, as HERO shows it
        link_update = next(values for doctype, values in fake.set_values if doctype == "HERO Integration Link")
        self.assertEqual(decode(link_update["source_field_hashes"])[ps.NOTES_FIELD],
                         field_hashes({ps.NOTES_FIELD: "New note"})[ps.NOTES_FIELD])

    def test_nothing_is_sent_when_switched_off_or_preview_is_stale(self):
        hero = _Hero()
        self._env(_Frappe(local_notes="New note"), enabled=False)
        with self.assertRaises(RuntimeError):
            ps.send("PV-1-H", None, "x", 1, client=hero, mutation_client=hero)
        self._env(_Frappe(local_notes="New note"))
        with self.assertRaises(ps.ProjectSendError):
            ps.send("PV-1-H", None, "stale", 1, client=hero, mutation_client=hero)
        self.assertEqual(hero.mutations, [])

    def test_unconfirmed_result_is_reported_without_retry(self):
        fake, hero = _Frappe(local_notes="New note"), _Hero()
        audits = self._env(fake)
        fingerprint = ps.preview("PV-1-H", client=hero)["fingerprint"]
        broken = SimpleNamespace(execute_once=lambda document, variables: {"update_project_match": {"id": 999}})
        with self.assertRaises(ps.ProjectSendError):
            ps.send("PV-1-H", None, fingerprint, 1, client=hero, mutation_client=broken)
        self.assertEqual(audits, [["reserved"], ["Failed"]])
        self.assertFalse(any(doctype == "Project" for doctype, _ in fake.set_values))


if __name__ == "__main__":
    unittest.main()
