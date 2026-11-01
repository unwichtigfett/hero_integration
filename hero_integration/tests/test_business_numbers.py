"""HERO business numbers name new ERPNext Customers and Projects; existing records keep theirs."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from hero_integration.services.business_numbers import (
    BusinessNumberError,
    business_number,
    customer_document_name,
    project_reference,
)
from hero_integration.services.project_naming import ProjectTitleValidationError
from hero_integration.tests.test_project_company_migration import FakeFrappeMigrationHarness, _project_title_inputs

EXECUTE = SimpleNamespace(mode="Execute")
PREVIEW = SimpleNamespace(mode="Preview")


class TestBusinessNumber(unittest.TestCase):
    def test_a_hero_number_is_kept_as_text(self):
        for value, expected in (("2222", "2222"), ("0022", "0022"), (2222, "2222"), ("C-401", "C-401")):
            with self.subTest(value=value):
                self.assertEqual(business_number(value), expected)

    def test_a_missing_number_gives_no_name(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(business_number(value))

    def test_a_number_that_cannot_be_a_document_name_is_refused(self):
        for value in (" 2222 ", "22 22", "22/22", "-22", "22-", "x" * 141, True, 22.5, {"nr": 1}):
            with self.subTest(value=value), self.assertRaises(BusinessNumberError):
                business_number(value)

    def test_maximum_length_preserves_leading_zeros(self):
        number = "0" * 139 + "2"
        self.assertEqual(business_number(number), number)
        with self.assertRaises(BusinessNumberError):
            business_number("0" + number)

    def test_the_customer_name_is_the_hero_nr(self):
        self.assertEqual(customer_document_name({"id": 7, "nr": "0022"}), "0022")
        self.assertIsNone(customer_document_name({"id": 7}))


class TestProjectReference(unittest.TestCase):
    def test_trade_and_display_id_form_the_reference(self):
        self.assertEqual(project_reference({"display_id": "3333", "measure": {"short": "PV"}}), "PV-3333")
        self.assertEqual(project_reference({"display_id": "1769", "measure": {"name": "Elektrohandwerk"}}), "ELEK-1769")
        self.assertEqual(project_reference({"display_id": "0042", "project_type": {"code": "KLIM"}}), "KLIM-0042")

    def test_project_reference_length_includes_the_trade_prefix(self):
        display_id = "0" * 136 + "2"
        self.assertEqual(project_reference({"display_id": display_id, "measure": {"short": "PV"}}),
                         "PV-" + display_id)
        with self.assertRaises(BusinessNumberError):
            project_reference({"display_id": "0" + display_id, "measure": {"short": "PV"}})

    def test_project_nr_is_not_the_reference(self):
        self.assertEqual(
            project_reference({"display_id": "3333", "project_nr": "999", "measure": {"short": "PV"}}), "PV-3333",
        )

    def test_a_missing_or_unknown_trade_invents_no_prefix(self):
        for record in ({"display_id": "3333"}, {"display_id": "3333", "measure": {"short": "XYZ"}}):
            with self.subTest(record=record), self.assertRaises(ProjectTitleValidationError):
                project_reference(record)

    def test_conflicting_measure_code_and_name_are_ambiguous(self):
        with self.assertRaises(ProjectTitleValidationError):
            project_reference({"display_id": "3333", "measure": {"short": "PV", "name": "Elektrohandwerk"}})

    def test_combined_reference_must_fit_the_native_name_limit(self):
        with self.assertRaises(BusinessNumberError):
            project_reference({"display_id": "1" * 138, "measure": {"short": "PV"}})

    def test_without_display_id_there_is_no_reference(self):
        self.assertIsNone(project_reference({"measure": {"short": "PV"}}))


class TestBusinessNumberMigration(FakeFrappeMigrationHarness, unittest.TestCase):
    def customer(self, **overrides):
        record = {"id": 301, "nr": "2222", "full_name": "Synthetic Ada", "company_name": None}
        record.update(overrides)
        return record

    def migrate_customer(self, record, run=EXECUTE):
        return self.migration._migrate_customer(record, run)

    def migrate_project(self, record, run=EXECUTE):
        return self.migration._migrate_project(record, run)

    # Customers

    def test_a_new_customer_is_named_by_its_hero_number(self):
        result = self.migrate_customer(self.customer())
        self.assertEqual((result.code, result.target_name), ("CREATED", "2222"))
        customer, = self.frappe.customers
        self.assertEqual(customer["name"], "2222")
        self.assertEqual(customer["customer_name"], "Synthetic Ada")
        self.assertEqual(customer["custom_hero_customer_number"], "2222")
        self.assertEqual(self.frappe.links[("customer", "301")].target_name, "2222")

    def test_leading_zeros_survive(self):
        self.migrate_customer(self.customer(nr="0022"))
        self.assertEqual(self.frappe.customers[0]["name"], "0022")
        self.assertEqual(self.frappe.customers[0]["custom_hero_customer_number"], "0022")

    def test_a_customer_without_number_gets_an_internal_id_and_a_note(self):
        result = self.migrate_customer(self.customer(nr=None))
        self.assertEqual(result.code, "CREATED")
        self.assertTrue(result.target_name.startswith("HERO-CUSTOMER-"))
        self.assertIn("HERO has no customer number", result.detail)

    def test_an_unusable_number_stays_visible_and_the_reason_is_reported(self):
        result = self.migrate_customer(self.customer(nr="22/22"))
        self.assertEqual(result.code, "CREATED")
        self.assertTrue(result.target_name.startswith("HERO-CUSTOMER-"))
        self.assertEqual(self.frappe.customers[0]["custom_hero_customer_number"], "22/22")
        self.assertIn("'22/22'", result.detail)

    def test_whitespace_number_is_retained_without_fabricating_a_business_number(self):
        result = self.migrate_customer(self.customer(nr=" 0022 "))
        self.assertEqual(result.code, "CREATED")
        self.assertTrue(result.target_name.startswith("HERO-CUSTOMER-"))
        self.assertEqual(self.frappe.customers[0]["custom_hero_customer_number"], " 0022 ")

    def test_explicitly_cleared_business_number_is_a_drift_conflict(self):
        self.migrate_customer(self.customer())
        result = self.migrate_customer(self.customer(nr=""))
        self.assertEqual(result.code, "CONFLICT")
        self.assertEqual(self.frappe.customers[0]["custom_hero_customer_number"], "2222")

    def test_a_repeated_import_is_idempotent(self):
        self.migrate_customer(self.customer())
        result = self.migrate_customer(self.customer())
        self.assertEqual((result.code, result.target_name), ("SKIPPED", "2222"))
        self.assertEqual(len(self.frappe.customers), 1)

    def test_an_omitted_customer_number_preserves_the_linked_number(self):
        self.migrate_customer(self.customer(nr="0022"))
        record = self.customer()
        del record["nr"]
        result = self.migrate_customer(record)
        self.assertEqual(result.target_name, "0022")
        self.assertEqual(len(self.frappe.customers), 1)
        self.assertEqual(self.frappe.customers[0]["custom_hero_customer_number"], "0022")
        self.assertEqual(self.frappe.links[("customer", "301")].target_name, "0022")

    def test_an_existing_kd_customer_is_reused_and_keeps_its_name(self):
        self.migrate_customer(self.customer())
        # As imported by the previous version, under the site naming series.
        self.frappe.customers[0]["name"] = "KD-00007"
        self.frappe.links[("customer", "301")].target_name = "KD-00007"
        result = self.migrate_customer(self.customer())
        self.assertEqual((result.code, result.target_name), ("SKIPPED", "KD-00007"))
        self.assertEqual([c["name"] for c in self.frappe.customers], ["KD-00007"])

    def test_a_changed_hero_number_is_reported_and_not_applied(self):
        self.migrate_customer(self.customer())
        preview = self.migrate_customer(self.customer(nr="2223"), PREVIEW)
        self.assertEqual(preview.code, "CONFLICT")
        self.assertNotEqual(getattr(self.frappe.links[("customer", "301")], "sync_status", ""), "Conflict")

        result = self.migrate_customer(self.customer(nr="2223"))
        self.assertEqual((result.code, result.target_name), ("CONFLICT", "2222"))
        self.assertIn("'2222'", result.detail)
        self.assertIn("'2223'", result.detail)
        customer, = self.frappe.customers
        self.assertEqual((customer["name"], customer["custom_hero_customer_number"]), ("2222", "2222"))
        self.assertEqual(self.frappe.links[("customer", "301")].sync_status, "Conflict")

    def test_a_name_held_by_another_customer_is_a_conflict(self):
        self.frappe.customers.append({"name": "2222", "customer_name": "Manual Customer"})
        for run in (PREVIEW, EXECUTE):
            with self.subTest(mode=run.mode):
                result = self.migrate_customer(self.customer(), run)
                self.assertEqual(result.code, "CONFLICT")
                self.assertIn("'2222'", result.detail)
        self.assertEqual([c["customer_name"] for c in self.frappe.customers], ["Manual Customer"])
        self.assertNotIn(("customer", "301"), self.frappe.links)

    def test_a_name_taken_by_a_concurrent_import_is_a_conflict(self):
        self.frappe.taken_on_insert = "2222"
        result = self.migrate_customer(self.customer())
        self.assertEqual(result.code, "CONFLICT")
        self.assertIn("'2222'", result.detail)
        self.assertFalse(self.frappe.customers)
        self.assertNotIn(("customer", "301"), self.frappe.links)

    def test_another_source_instance_with_the_same_number_does_not_cross_link(self):
        self.migrate_customer(self.customer())
        result = self.migrate_customer(self.customer(id=302, _hero_source_instance="account-b"))
        self.assertEqual(result.code, "CONFLICT")
        self.assertEqual(len(self.frappe.customers), 1)
        self.assertNotIn(("customer", "302", "account-b"), self.frappe.links)
        self.assertEqual(self.frappe.links[("customer", "301")].target_name, "2222")

    def test_same_technical_id_on_two_sources_keeps_separate_customers(self):
        first = self.migrate_customer(self.customer())
        second = self.migrate_customer(self.customer(
            nr="3333", full_name="Synthetic other customer", _hero_source_instance="account-b",
        ))
        self.assertEqual((first.code, first.target_name), ("CREATED", "2222"))
        self.assertEqual((second.code, second.target_name), ("CREATED", "3333"))
        self.assertEqual(self.frappe.links[("customer", "301")].target_name, "2222")
        self.assertEqual(self.frappe.links[("customer", "301", "account-b")].target_name, "3333")
        self.assertEqual({row["name"]: row["customer_name"] for row in self.frappe.customers},
                         {"2222": "Synthetic Ada", "3333": "Synthetic other customer"})

    def test_same_technical_and_business_numbers_on_two_sources_conflict_without_merge(self):
        self.migrate_customer(self.customer())
        result = self.migrate_customer(self.customer(full_name="Synthetic other customer",
                                                     _hero_source_instance="account-b"))
        self.assertEqual(result.code, "CONFLICT")
        self.assertEqual(len(self.frappe.customers), 1)
        self.assertEqual(self.frappe.customers[0]["customer_name"], "Synthetic Ada")
        self.assertEqual(self.frappe.links[("customer", "301")].target_name, "2222")
        self.assertNotIn(("customer", "301", "account-b"), self.frappe.links)

    def test_two_source_identities_with_the_same_customer_number_conflict(self):
        self.migrate_customer(self.customer())
        result = self.migrate_customer(self.customer(id=302, full_name="Synthetic other customer"))
        self.assertEqual(result.code, "CONFLICT")
        self.assertEqual(len(self.frappe.customers), 1)
        self.assertNotIn(("customer", "302"), self.frappe.links)

    def test_concurrent_source_identity_claim_rolls_back_only_new_target(self):
        self.migrate_customer(self.customer())
        original_get_doc = self.migration.frappe.get_doc

        class UniqueClaimError(Exception):
            pass

        def get_doc(*args, **kwargs):
            if args and isinstance(args[0], dict) and args[0].get("doctype") == "HERO Integration Link":
                return SimpleNamespace(insert=mock.Mock(side_effect=UniqueClaimError("HERO Integration Link")))
            return original_get_doc(*args, **kwargs)

        with mock.patch.object(self.migration.frappe, "UniqueValidationError", UniqueClaimError, create=True), \
             mock.patch.object(self.migration.frappe, "get_doc", side_effect=get_doc):
            result = self.migrate_customer(self.customer(id=302, nr="3333", full_name="Synthetic other customer"))

        self.assertEqual(result.code, "CONFLICT")
        self.assertIn("claimed concurrently", result.detail)
        self.assertEqual([row["name"] for row in self.frappe.customers], ["2222"])
        self.assertEqual(self.frappe.links[("customer", "301")].target_name, "2222")
        self.assertNotIn(("customer", "302"), self.frappe.links)

    def test_preview_names_the_new_document_and_writes_nothing(self):
        result = self.migrate_customer(self.customer(), PREVIEW)
        self.assertEqual(result.code, "PREVIEW_OK")
        self.assertIn("'2222'", result.detail)
        self.assertFalse(self.frappe.customers)

    # Projects

    def project(self, **overrides):
        values = {"id": 201, "customer_id": 101, "project_nr": "3333", "display_id": "3333"}
        values.update(overrides)
        return _project_title_inputs(**values)

    def test_a_new_project_is_named_by_trade_and_display_id(self):
        result = self.migrate_project(self.project())
        self.assertEqual((result.code, result.target_name), ("CREATED", "PV-3333"))
        project, = self.frappe.projects
        self.assertTrue(project["project_name"].startswith("PV-3333 | Photovoltaik,"))
        self.assertEqual(project["custom_hero_display_id"], "3333")
        self.assertEqual(project["custom_hero_source_id"], "default:201")
        self.assertEqual(self.frappe.links[("project", "201")].target_name, "PV-3333")

    def test_legacy_project_id_and_historical_title_are_preserved_on_reimport(self):
        record = self.project()
        self.migrate_project(record)
        project, = self.frappe.projects
        historical_title = project["project_name"]
        project["name"] = "PROJ-LEGACY-0007"
        self.frappe.links[("project", "201")].target_name = "PROJ-LEGACY-0007"
        result = self.migrate_project(record)
        self.assertEqual((result.code, result.target_name), ("SKIPPED", "PROJ-LEGACY-0007"))
        self.assertEqual(len(self.frappe.projects), 1)
        self.assertEqual(project["project_name"], historical_title)

    def test_distinct_projects_with_the_same_trade_and_display_id_conflict(self):
        self.migrate_project(self.project())
        result = self.migrate_project(self.project(id=202))
        self.assertEqual(result.code, "CONFLICT")
        self.assertEqual(len(self.frappe.projects), 1)

    def test_the_hero_measure_decides_the_prefix(self):
        record = self.project(project_nr="1769", display_id="1769", measure={"short": "ELEK"})
        self.assertEqual(self.migrate_project(record).target_name, "ELEK-1769")

    def test_an_unknown_trade_blocks_the_project(self):
        result = self.migrate_project(self.project(project_type={"code": "XYZ"}))
        self.assertEqual(result.code, "BLOCKED_RECORD")
        self.assertFalse(self.frappe.projects)

    def test_a_project_without_display_id_gets_the_site_series_and_a_note(self):
        result = self.migrate_project(self.project(display_id=None))
        self.assertEqual((result.code, result.target_name), ("CREATED", "PROJ-0001"))
        self.assertIn("HERO has no project display_id", result.detail)

    def test_a_reference_held_by_another_project_is_a_conflict(self):
        self.frappe.projects.append({"name": "PV-3333", "project_name": "Manual project"})
        result = self.migrate_project(self.project())
        self.assertEqual(result.code, "CONFLICT")
        self.assertEqual(len(self.frappe.projects), 1)

    def test_identical_project_reference_never_cross_links_source_instances(self):
        self.migrate_project(self.project())
        result = self.migrate_project(self.project(id=202, _hero_source_instance="account-b"))
        self.assertEqual(result.code, "CONFLICT")
        self.assertEqual(len(self.frappe.projects), 1)
        self.assertNotIn(("project", "202", "account-b"), self.frappe.links)

    def test_a_changed_display_id_is_reported_and_not_applied(self):
        self.migrate_project(self.project())
        result = self.migrate_project(self.project(display_id="3334"))
        self.assertEqual((result.code, result.target_name), ("CONFLICT", "PV-3333"))
        self.assertEqual(self.frappe.projects[0]["custom_hero_display_id"], "3333")

    def test_an_omitted_display_id_preserves_the_linked_reference(self):
        self.migrate_project(self.project(display_id="0033"))
        record = self.project()
        del record["display_id"]
        result = self.migrate_project(record)
        self.assertEqual(result.target_name, "PV-0033")
        self.assertEqual(len(self.frappe.projects), 1)
        self.assertEqual(self.frappe.projects[0]["custom_hero_display_id"], "0033")
        self.assertEqual(self.frappe.links[("project", "201")].target_name, "PV-0033")

    def test_a_project_links_to_its_customer_by_the_customer_number(self):
        self.migrate_customer(self.customer(id=401, nr="2222"))
        self.migrate_project(self.project(customer_id=401))
        self.assertEqual(self.frappe.projects[0]["customer"], "2222")

    def test_equal_source_ids_keep_project_customer_relations_source_scoped(self):
        self.migrate_customer(self.customer(id=401, nr="2222"))
        self.migrate_customer(self.customer(id=401, nr="4444", full_name="Synthetic other customer",
                                            _hero_source_instance="account-b"))
        first = self.migrate_project(self.project(customer_id=401))
        second = self.migrate_project(self.project(customer_id=401, display_id="5555", project_nr="5555",
                                                   _hero_source_instance="account-b"))
        self.assertEqual((first.code, second.code), ("CREATED", "CREATED"))
        self.assertEqual({row["name"]: row["customer"] for row in self.frappe.projects},
                         {"PV-3333": "2222", "PV-5555": "4444"})
        self.assertEqual(self.frappe.links[("project", "201")].target_name, "PV-3333")
        self.assertEqual(self.frappe.links[("project", "201", "account-b")].target_name, "PV-5555")


class TestRenamePreview(unittest.TestCase):
    def setUp(self):
        from hero_integration.services import business_number_preview
        self.preview = business_number_preview

    def row(self, current, proposed, status="Synchronized", links=1, doctype="Customer"):
        return {"doctype": doctype, "current_name": current, "proposed_name": proposed, "link_status": status,
                "links_to_target": links, "collision": "", "safe_rename": False, "reason": ""}

    def test_project_preview_never_guesses_trade_from_a_title(self):
        propose = self.preview._project_proposal
        for title in ("ELEK-1769 | Elektrohandwerk, Herr X", "PV-1769 | Historical title"):
            self.assertIsNone(propose({"project_name": title}, "1769")[0])
        self.assertEqual(propose({}, "0017", trade="ELEK"), ("ELEK-0017", ""))
        self.assertEqual(propose({"custom_hero_project_type": "Photovoltaik"}, "0033"), ("PV-0033", ""))

    def test_only_unambiguous_synchronized_records_are_safe_to_rename(self):
        rows = [
            self.row("KD-00001", "2222"),
            self.row("KD-00002", "3333"),            # held by a manual customer
            self.row("KD-00003", "4444"), self.row("KD-00004", "4444"),  # same number twice
            self.row("KD-00005", "5555", status="Conflict"),
            self.row("KD-00006", "6666", links=2),
            self.row("7777", "7777"),
        ]
        with mock.patch.object(self.preview, "frappe", SimpleNamespace(db=SimpleNamespace(
                exists=lambda doctype, name: name == "3333"),
                get_meta=lambda doctype: SimpleNamespace(allow_rename=True))):
            self.preview._judge(rows)
        self.assertEqual([row["safe_rename"] for row in rows], [True, False, False, False, False, False, False])
        self.assertEqual(rows[1]["collision"], "name used by Customer 3333")
        self.assertEqual(rows[2]["collision"], "proposed by several HERO records")
        self.assertEqual(rows[4]["reason"], "link status Conflict")
        self.assertEqual(rows[6]["reason"], "already named by its HERO number")


if __name__ == "__main__":
    unittest.main()
