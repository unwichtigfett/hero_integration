import copy
import unittest

from hero_integration.integrations import native_field_moves as moves
from hero_integration.services import direct_mapping
from hero_integration.services.direct_mapping import MappingSpec, transformed_value


def _content():
    return {
        "target_operation": "HeroCustomers",
        "mapping_definition_ids": ["customer-type", "customer-source-type", "customer-contact-person", "customer-number"],
        "field_rules": {
            "customer-type": {"purpose": "Customer Type", "source_path": "company_name", "source_type": "string|null",
                              "target_doctype": "Customer", "target_field": "customer_type",
                              "transformation": "Customer Type From Company Name"},
            "customer-source-type": {"purpose": "Field", "source_path": "type", "target_doctype": "Customer",
                                     "target_field": "custom_hero_customer_type", "transformation": "Trim"},
            "customer-contact-person": {"purpose": "Field", "source_path": "is_contact_person",
                                        "target_doctype": "Customer", "target_field": "custom_hero_is_contact_person"},
            "customer-number": {"purpose": "Field", "source_path": "nr", "target_doctype": "Customer",
                                "target_field": "custom_hero_customer_number", "transformation": "Trim"},
        },
        "field_rule_bases": {"customer-type": {"name": "HERO-MAP-1", "revision": 1}},
    }


class TestNativeFieldMoves(unittest.TestCase):
    def test_customer_rules_move_to_the_native_type_and_unlink_duplicates(self):
        content = _content()
        changes = moves.revise_content(content, "HeroCustomers")
        self.assertEqual(len(changes), 3)
        self.assertEqual(content["mapping_definition_ids"], ["customer-type", "customer-number"])
        rule = content["field_rules"]["customer-type"]
        self.assertEqual((rule["source_path"], rule["transformation"]), ("type", "Customer Type From HERO Type"))
        # Snapshots and server-managed provenance stay in the draft.
        self.assertIn("customer-source-type", content["field_rules"])
        self.assertEqual(content["field_rule_bases"], _content()["field_rule_bases"])
        again = copy.deepcopy(content)
        self.assertEqual(moves.revise_content(again, "HeroCustomers"), [])
        self.assertEqual(again, content)

    def test_project_and_event_rules_target_native_fields(self):
        project = {"mapping_definition_ids": ["n"], "field_rules": {"n": {
            "source_path": "partner_notes", "target_doctype": "Project", "target_field": "custom_hero_partner_notes",
            "transformation": "Trim", "purpose": "Field"}}}
        moves.revise_content(project, "HeroProjectMatches")
        self.assertEqual((project["field_rules"]["n"]["target_field"], project["field_rules"]["n"]["transformation"]),
                         ("notes", "Plain Text To HTML"))
        event = {"mapping_definition_ids": ["c", "d"], "field_rules": {
            "c": {"source_path": "color", "target_field": "custom_hero_color", "purpose": "Field", "transformation": "Identity"},
            "d": {"source_path": "is_done", "target_field": "custom_hero_is_done", "purpose": "Field", "transformation": "Scalar Text"}}}
        moves.revise_content(event, "HeroCalendarEvents")
        self.assertEqual(event["field_rules"]["c"]["target_field"], "color")
        self.assertEqual((event["field_rules"]["d"]["target_field"], event["field_rules"]["d"]["purpose"],
                          event["field_rules"]["d"]["transformation"]), ("status", "Native Binding", "Native Adapter"))

    def test_hero_type_and_notes_conversions(self):
        spec = MappingSpec("HeroCustomers", "type", "Customer", "customer_type", "string|null",
                           "Customer Type", "Customer Type From HERO Type")
        self.assertEqual(transformed_value(spec, "commercial"), "Company")
        self.assertEqual(transformed_value(spec, " Private "), "Individual")
        self.assertEqual(transformed_value(spec, None), "Individual")
        notes = MappingSpec("HeroProjectMatches", "partner_notes", "Project", "notes", "string|null",
                            "Field", "Plain Text To HTML")
        self.assertEqual(transformed_value(notes, "a <b>\nc"), "<p>a &lt;b&gt;<br>c</p>")
        self.assertIsNone(transformed_value(notes, "  "))
        from hero_integration.services.task_send import plain_text
        self.assertEqual(plain_text(transformed_value(notes, "Zeile 1\nZeile & 2")), "Zeile 1\nZeile & 2")

    def test_notes_may_only_bind_the_native_editor_field(self):
        class Field:
            fieldtype, read_only = "Text Editor", 0

        class Metadata:
            def field(self, doctype, fieldname):
                return Field() if (doctype, fieldname) == ("Project", "notes") else None

        ok = MappingSpec("HeroProjectMatches", "partner_notes", "Project", "notes", "string|null",
                         "Field", "Plain Text To HTML")
        direct_mapping._validate_custom_field(ok, Metadata())
        for bad in (MappingSpec("HeroProjectMatches", "partner_notes", "Project", "notes", "string|null", "Field", "Trim"),
                    MappingSpec("HeroProjectMatches", "project_nr", "Project", "notes", "string|null",
                                "Field", "Plain Text To HTML")):
            with self.assertRaises(direct_mapping.DirectMappingError):
                direct_mapping._validate_custom_field(bad, Metadata())
        Field.read_only = 1
        with self.assertRaises(direct_mapping.DirectMappingError):
            direct_mapping._validate_custom_field(ok, Metadata())


class TestProductOrderFields(unittest.TestCase):
    def test_builtin_product_mapping_fills_native_order_fields(self):
        from hero_integration.services import direct_operations as direct
        rules = {(r["source_path"], r["target_field"], r["transformation"]) for r in direct.DEFAULT_RULES["HeroProducts"]}
        self.assertIn(("base_data.quantity_min", "min_order_qty", "Number"), rules)
        self.assertIn(("base_data.delivery_time", "lead_time_days", "Integer"), rules)
        self.assertTrue({"min_order_qty", "lead_time_days"} <= direct.FIELDS["Item"])


class TestPeopleAndSourceFields(unittest.TestCase):
    def test_project_v4_and_customer_v5_read_the_new_fields(self):
        from hero_integration.sources.hero import customer_query, project_query
        query = project_query("v4")
        for part in ("name partner_source", "partner { email }", "project_match_assignments { user { email } }", "volume"):
            self.assertIn(part, query)
        self.assertIn(" type source ", customer_query("v5"))
        self.assertNotIn("source", customer_query("v4").replace("source_", ""))

    def test_assignee_emails_and_sync(self):
        from types import SimpleNamespace
        from unittest import mock
        from hero_integration.integrations import project_assignments as pa
        record = {"project_match_assignments": [{"user": {"email": "Anna@Example.invalid "}}, {"user": None},
                                                {"user": {"email": "bob@example.invalid"}}]}
        self.assertEqual(pa.assignee_emails(record), {"anna@example.invalid", "bob@example.invalid"})
        self.assertIsNone(pa.assignee_emails({}))
        inserted, saved = [], []

        class Todo(dict):
            __getattr__ = dict.get

        open_rows = [Todo(name="T1", allocated_to="old@x", description=pa.MARKER + ": P"),
                     Todo(name="T2", allocated_to="manual@x", description="by hand")]
        api = SimpleNamespace(
            db=SimpleNamespace(get_value=lambda *a: "Project title"),
            get_all=lambda *a, **k: open_rows,
            get_doc=lambda value, name=None: SimpleNamespace(insert=lambda **k: inserted.append(value)) if isinstance(value, dict)
            else SimpleNamespace(status="Open", save=lambda **k: saved.append(name)),
        )
        with mock.patch("hero_integration.services.task_lifecycle._user_for",
                        side_effect=lambda email, _api: "anna" if email.startswith("anna") else None):
            result = pa.sync("P-1", record, api)
        self.assertEqual(result, {"added": 1, "closed": 0, "unmatched": 1})
        self.assertEqual(inserted[0]["allocated_to"], "anna")
        self.assertEqual(saved, [])  # incomplete resolution cannot prove any removal

    def test_unresolved_hero_id_preserves_owned_assignment_until_removal_is_proven(self):
        from types import SimpleNamespace as NS
        from unittest import mock
        from hero_integration.integrations import project_assignments as pa
        owned = NS(name='owned', allocated_to='old@example.invalid', description=pa.MARKER + ': Project')
        manual = NS(name='manual', allocated_to='manual@example.invalid', description='Created manually')
        documents = {row.name: NS(status='Open', save=mock.Mock()) for row in (owned, manual)}
        api = NS(db=NS(get_value=lambda *args: 'Project title'), get_all=lambda *args, **kwargs: [owned, manual],
                 get_doc=mock.Mock(side_effect=lambda doctype, name: documents[name]))
        record = {'_hero_source_instance': 'source-a', 'project_match_assignments': [{'user_id': 7}]}
        with mock.patch('hero_integration.integrations.hero_people.user_by_hero_user', return_value=None) as resolve:
            self.assertEqual(pa.sync('P-1', record, api), {'added': 0, 'closed': 0, 'unmatched': 1})
            resolve.assert_called_once_with(7, source_instance='source-a')
            api.get_doc.assert_not_called()
            resolve.return_value = owned.allocated_to
            self.assertEqual(pa.sync('P-1', record, api), {'added': 0, 'closed': 0, 'unmatched': 0})
            api.get_doc.assert_not_called()
            record['project_match_assignments'] = []
            self.assertEqual(pa.sync('P-1', record, api), {'added': 0, 'closed': 1, 'unmatched': 0})
        self.assertEqual(documents['owned'].status, 'Cancelled')
        documents['owned'].save.assert_called_once_with(ignore_permissions=True)
        self.assertEqual(documents['manual'].status, 'Open')
        documents['manual'].save.assert_not_called()

    def test_fully_resolved_replacement_closes_only_removed_imported_assignment(self):
        from types import SimpleNamespace as NS
        from unittest import mock
        from hero_integration.integrations import project_assignments as pa
        rows = [NS(name='owned', allocated_to='old@example.invalid', description=pa.MARKER + ': Project'),
                NS(name='manual', allocated_to='manual@example.invalid', description='Manual')]
        inserted, closed = [], []
        api = NS(db=NS(get_value=lambda *args: 'Project'), get_all=lambda *args, **kwargs: rows,
                 get_doc=lambda value, name=None: NS(insert=lambda **kwargs: inserted.append(value))
                 if isinstance(value, dict) else NS(status='Open', save=lambda **kwargs: closed.append(name)))
        with mock.patch('hero_integration.integrations.hero_people.user_by_hero_user', return_value='new@example.invalid'):
            result = pa.sync('P-1', {'project_match_assignments': [{'user_id': 8}]}, api)
        self.assertEqual(result, {'added': 1, 'closed': 1, 'unmatched': 0})
        self.assertEqual(inserted[0]['allocated_to'], 'new@example.invalid')
        self.assertEqual(closed, ['owned'])

    def test_task_author_is_resolved_only_with_a_resolver(self):
        from datetime import date
        from hero_integration.services import task_lifecycle as life
        record = {"author_user_id": 7, "title": "x"}
        local = lambda value: None
        self.assertNotIn(life.AUTHOR, life.source_values(record, local, {7: "a@b.c"}, date(2026, 1, 1)))
        values = life.source_values(record, local, {7: " A@B.c"}, date(2026, 1, 1),
                                    user_for=lambda uid, email: f"{uid}:{email}")
        self.assertEqual(values[life.AUTHOR], "7:a@b.c")


class TestPeopleById(unittest.TestCase):
    def test_project_v5_reads_people_by_id(self):
        from hero_integration.sources.hero import project_query
        query = project_query("v5")
        self.assertIn("name partner_source partner_id", query)
        self.assertIn("project_match_assignments { user_id }", query)
        self.assertNotIn("email", query)

    def test_responsible_binds_only_by_partner_id_to_a_user_link(self):
        class Field:
            fieldtype, options, read_only = "Link", "User", 1

        class Metadata:
            def field(self, doctype, fieldname):
                return Field()

        ok = MappingSpec("HeroProjectMatches", "partner_id", "Project", "custom_hero_project_manager", "int|null",
                         "Field", "User By Partner ID")
        direct_mapping._validate_custom_field(ok, Metadata())
        wrong = MappingSpec("HeroProjectMatches", "partner_id", "Project", "custom_hero_project_manager", "int|null",
                            "Field", "User By Email")
        with self.assertRaises(direct_mapping.DirectMappingError):
            direct_mapping._validate_custom_field(wrong, Metadata())

    def test_assignees_prefer_the_hero_user_id(self):
        from hero_integration.integrations.project_assignments import assignees
        self.assertEqual(assignees({"project_match_assignments": [{"user_id": 5}, {"user_id": 5},
                                                                  {"user": {"email": "A@b.de"}}]}),
                         [("email", "a@b.de"), ("id", 5)])
