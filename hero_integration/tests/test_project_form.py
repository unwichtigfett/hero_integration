"""HERO values on the Project form appear as ERPNext fields (owner decision 2026-11-01)."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.integrations import project_form as form

CURRENT = ["project_name", "status", "project_type", "is_active", "column_break_5", "expected_start_date",
           "section_break0", "notes", "custom_hero_source_id", "custom_hero_related_project_id",
           "custom_hero_display_id", "custom_hero_source_created", "custom_hero_source_modified",
           "custom_hero_partner_notes", "custom_hero_address_full_address", "custom_hero_observed_status_name",
           "custom_hero_project_type", "custom_hero_phase", "costing_tab"]


class TestProjectForm(unittest.TestCase):
    def test_field_order_puts_hero_values_first_and_keeps_everything(self):
        order = form.field_order(CURRENT)
        self.assertEqual(sorted(order), sorted(CURRENT))
        self.assertEqual(order[:6], ["project_name", "custom_hero_related_project_id", "custom_hero_display_id",
                                     "status", "project_type", "custom_hero_phase"])
        self.assertEqual(order[order.index("column_break_5") + 1:order.index("column_break_5") + 4],
                         ["custom_hero_source_created", "custom_hero_source_modified", "custom_hero_address_full_address"])
        self.assertEqual(order[order.index("notes") + 1], "custom_hero_partner_notes")

    def test_setup_relabels_hides_and_orders_without_touching_field_definitions(self):
        setters, inserted = [], []
        fake = SimpleNamespace(
            db=SimpleNamespace(exists=lambda doctype, name: name == "Photovoltaik" or doctype == "Custom Field"),
            get_doc=lambda values: SimpleNamespace(insert=lambda **k: inserted.append(values.get("project_type") or values["role_name"])),
            get_meta=lambda doctype: SimpleNamespace(fields=[SimpleNamespace(fieldname=f) for f in CURRENT]),
            clear_cache=lambda **k: None,
        )
        with patch.object(form, "frappe", fake), patch(
                "frappe.custom.doctype.property_setter.property_setter.make_property_setter",
                side_effect=lambda *args, **kwargs: setters.append(args)):
            form.setup_project_form()
        labels = {a[1]: a[3] for a in setters if a[2] == "label"}
        self.assertEqual(labels["custom_hero_display_id"], "Project Nr.")
        self.assertEqual(labels["custom_hero_partner_notes"], "Notes")
        self.assertIn(("Project", "custom_hero_source_id", "hidden", "1", "Check"), setters)
        self.assertIn(("Project", "custom_hero_observed_status_name", "hidden", "1", "Check"), setters)
        self.assertIn(("Project", "custom_hero_address_full_address", "read_only", "1", "Check"), setters)
        self.assertIn(("Project", "custom_hero_partner_notes", "read_only", "0", "Check"), setters)  # sent to HERO
        self.assertIn("HERO Writeback", inserted)
        order = json.loads(next(a[3] for a in setters if a[2] == "field_order"))
        self.assertEqual(len(order), len(CURRENT))  # Frappe applies field_order only when complete
        self.assertNotIn("Photovoltaik", inserted)
        self.assertIn("Elektrohandwerk", inserted)

    def test_project_type_is_the_trade(self):
        fake = SimpleNamespace(db=SimpleNamespace(exists=lambda doctype, name: name in {"Photovoltaik", "Dachsanierung"}))
        with patch.object(form, "frappe", fake):
            self.assertEqual(form.project_type_for_trade("PV"), "Photovoltaik")
            self.assertEqual(form.project_type_for_trade("ds"), "Dachsanierung")
            self.assertIsNone(form.project_type_for_trade("ELEK"))  # Project Type not created yet
            self.assertIsNone(form.project_type_for_trade(None))


class TestProjectPhases(unittest.TestCase):
    def test_phase_is_the_step_name_and_only_hero_projects_change(self):
        updates = []
        projects = [{"name": "PV-1-H", "custom_hero_observed_status_step_id": "4730", "custom_hero_phase": None},
                    {"name": "PV-2-H", "custom_hero_observed_status_step_id": "4727", "custom_hero_phase": "Neu Kontakt"},
                    {"name": "PV-3", "custom_hero_observed_status_step_id": "4730", "custom_hero_phase": None}]
        fake = SimpleNamespace(
            db=SimpleNamespace(has_column=lambda doctype, field: True,
                               set_value=lambda doctype, name, field, value, update_modified=True: updates.append((name, value))),
            get_all=lambda doctype, **kwargs: ["PV-1-H", "PV-2-H"] if doctype == "HERO Integration Link"
            else [SimpleNamespace(**p, get=p.get) for p in projects],
        )
        with patch.object(form, "frappe", fake):
            changed = form.refresh_project_phases({4730: "ANG Nachfassung", 4727: "Neu Kontakt"})
        self.assertEqual((changed, updates), (1, [("PV-1-H", "ANG Nachfassung")]))

    def test_source_reads_all_step_names_once(self):
        from hero_integration.sources.hero import GraphQLServiceSource
        documents = []
        client = SimpleNamespace(query=lambda document, variables: documents.append(document) or {
            "project_types": [{"id": 1, "project_status_steps": [{"id": 4730, "name": "ANG Nachfassung"}]},
                              {"id": 2, "project_status_steps": [{"id": 5, "name": "Neu"}, {"id": None}]}]})
        source = GraphQLServiceSource(client, SimpleNamespace(), SimpleNamespace(), "default")
        self.assertEqual(source.project_status_steps(), {4730: "ANG Nachfassung", 5: "Neu"})
        self.assertEqual(len(documents), 1)
        self.assertTrue(documents[0].startswith("query "))


class TestPhaseRefreshInRuns(unittest.TestCase):
    def test_refresh_runs_after_a_blocked_project_phase_and_never_breaks_the_run(self):
        from hero_integration.services import migration
        from hero_integration.integrations import project_form as pf
        calls, db = [], SimpleNamespace(commit=lambda: calls.append("commit"), rollback=lambda: calls.append("rollback"))
        fake = SimpleNamespace(db=db, log_error=lambda **k: calls.append("logged"))
        source = SimpleNamespace(project_status_steps=lambda: {1: "Neu"})
        with patch.object(migration, "frappe", fake), patch.object(pf, "refresh_project_phases",
                                                                     side_effect=lambda names: calls.append(names)):
            migration._refresh_project_phases(SimpleNamespace(mode="Execute", state="Blocked"), source)
            migration._refresh_project_phases(SimpleNamespace(mode="Preview"), source)
        self.assertEqual(calls, [{1: "Neu"}, "commit"])
        failing = SimpleNamespace(project_status_steps=lambda: (_ for _ in ()).throw(RuntimeError("HERO down")))
        calls.clear()
        with patch.object(migration, "frappe", fake):
            migration._refresh_project_phases(SimpleNamespace(mode="Execute"), failing)
        self.assertEqual(calls, ["rollback", "logged"])


if __name__ == "__main__":
    unittest.main()
