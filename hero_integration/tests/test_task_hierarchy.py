import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from graphql import build_schema, introspection_from_schema
from hero_integration.services import mapped_update_v2 as update, task_hierarchy as h
from hero_integration.tests import test_mapped_update_v2 as fixtures


def task(name, parent, title, status="Open"):
    return SimpleNamespace(name=name, parent_task=parent, subject=title, status=status)


class TaskTree(TestCase):
    def setUp(self):
        self.rows = [task("a", "root", "PV-Antrag", "Completed"), task("b", "root", "Lageplan", "Completed"),
                     task("c", "b", "Übersichtsplan")]
        self.db = Mock()
        self.db.get_all.side_effect = lambda dt, filters, **kw: sorted(
            (r for r in self.rows if r.parent_task in filters["parent_task"][1]), key=lambda r: r.name)
        self.db.db.get_value.side_effect = lambda dt, name, field: next(
            (r.parent_task for r in self.rows if r.name == name), None)

    def section(self):
        return h.section_for(self.db, "root")

    def test_deterministic_tree_and_lifecycle(self):
        expected = "Unteraufgaben (2/3 erledigt):\n- PV-Antrag: Erledigt\n- Lageplan: Erledigt\n  - Übersichtsplan: Ausstehend"
        self.assertEqual(self.section(), expected)
        self.rows.reverse()
        self.assertEqual(self.section(), expected)
        self.rows[0].subject = "Prüfplan"
        self.rows[0].status = "Completed"
        self.assertIn("3/3 erledigt", self.section())
        self.rows[0].status = "Working"
        self.assertIn("  - Prüfplan: In Bearbeitung", self.section())
        self.assertEqual(h.roots_for(self.db, {"b", "other"}), {"root", "other"})
        self.rows[0].parent_task = "other"
        self.assertNotIn("Prüfplan", self.section())
        self.assertIn("Prüfplan", h.section_for(self.db, "other"))
        self.rows.clear()
        self.assertEqual(self.section(), "")

    def test_split_requires_proven_suffix(self):
        section = self.section()
        proof = h.section_proof(section)
        original = 'Eigener Text\n\n' + section
        wire = h.compose(original, section)
        self.assertEqual(h.source_body(wire, proof), original)
        self.assertEqual(h.source_body(wire, None), wire)
        self.assertEqual(h.source_body(wire.replace("\n", "\r\n"), proof), original)
        self.assertEqual(h.source_body(h.compose("Geändert", section), proof), "Geändert")
        for bad in (None, "", "Beschreibung", wire[:-1], wire+"!", "Text\n"+section):
            with self.subTest(bad=bad), self.assertRaises(h.ProjectionConflict):
                h.source_body(bad, proof)

    def test_no_truncation_or_loss_of_native_markup(self):
        original = "<p>" + "Ä" * 40000 + "</p>"
        wire = h.compose(original, self.section())
        self.assertEqual(h.source_body(wire, h.section_proof(self.section())), original)

    def test_mapping_and_independent_simulated_readback(self):
        ctx = fixtures.TestMappedUpdatePrepare().context("HeroTasks")
        ctx.document.name = "root"
        ctx.raw_schema = introspection_from_schema(build_schema(fixtures.SDL.replace("TaskInput {id:Int", "TaskInput {comment:String id:Int")))
        item, = ctx.content["assignments"]
        item.update(source={"doctype": "Task", "field": "description"}, input_path="task.comment",
                    read_path="comment", resolver="PLAIN_TEXT")
        binding = SimpleNamespace(target_doctype="Task", target_field="description", source_field="comment",
                                  acknowledged_value_json="", identity_key="field", revision=1, acknowledged_version=1)
        ctx.fields = {"description": binding}
        ctx.source_links[item["id"]] = ctx.link
        ctx.source_documents[item["id"]] = ctx.document
        remote = {"id": 7, "comment": "Anfrage beim Netzbetreiber einreichen."}
        reader = Mock(side_effect=lambda: dict(remote))
        transport = Mock(side_effect=lambda variables: remote.update(comment=variables["input"]["comment"]))
        def prepare():
            return update._prepare(ctx, None)
        def confirm(result):
            proof = {"wire": {item["id"]: update._digest(result.checks[0]["value"])}, "copied": {},
                     "mutation_id": "7", "task_sections": {item["id"]: result.changes[0]["task_section"]}}
            update._verified_remote(ctx, proof, reader())
            binding.acknowledged_value_json = h.pack(body, result.changes[0]["task_section"])
        body = remote["comment"]
        with patch.object(update, "_read_record", side_effect=lambda *args: reader()), \
             patch.object(update, "_source", return_value=(ctx.document, body)), \
             patch.object(update, "_canonical", side_effect=lambda ctx,item,r:r["comment"]), \
             patch.object(update, "_registry_decision", return_value=("CONFIRM_EQUAL",binding)), \
             patch.object(update, "frappe", self.db):
            sent = prepare()
            self.assertEqual(sent.variables, {"input": {"id": 7, "comment": h.compose(body, self.section())}})
            transport(sent.variables)
            confirm(sent)
            self.assertEqual(h.clean_record(reader(),binding)["comment"],body)
            self.assertEqual(prepare().changes, [])
            remote["comment"] = h.compose("Eigene HERO-Änderung", self.section())
            self.assertEqual(h.clean_record(reader(),binding)["comment"],"Eigene HERO-Änderung")
            remote["comment"] = body
            self.assertTrue(prepare().conflicts)
            remote["comment"] = h.compose(body,self.section())
            self.rows.clear()
            removed = prepare()
            self.assertEqual(removed.variables["input"]["comment"], body)
            transport(removed.variables)
            confirm(removed)
            self.assertFalse(h.confirmed(binding))
            self.assertEqual(prepare().changes, [])
            self.assertEqual(transport.call_count, 2)
