"""Conflict report of the Synchronization page: fields, HERO variables and values."""
from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import conflict_report as cr


class _Row(dict):
    __getattr__ = dict.get


class _Frappe:
    PermissionError = type("PermissionError", (Exception,), {})
    ValidationError = type("ValidationError", (Exception,), {})

    def __init__(self, links=None, docs=None, rules=(), records=None, labels=None):
        self.links, self.docs, self.rules, self.records = links or {}, docs or {}, list(rules), records or {}
        self.labels = labels or {}
        self.db = SimpleNamespace(exists=self.exists, get_value=self.get_value)
        self.utils = SimpleNamespace(strip_html_tags=lambda text: text.replace("<p>", "").replace("</p>", ""))

    def get_doc(self, doctype, name):
        if doctype == "HERO Integration Link":
            return _Row(self.links[name])
        if doctype == "HERO Integration Record":
            return _Row(self.records[name])
        return _Row(self.docs[(doctype, name)])

    def exists(self, doctype, name=None):
        if doctype == "DocType":
            return name in {key[0] for key in self.docs} | set(self.labels)
        return (doctype, name) in self.docs

    def get_value(self, doctype, name, field):
        if doctype == "HERO Integration Run":
            return "default"
        return self.docs[(doctype, name)].get(field)

    def get_meta(self, doctype):
        labels = self.labels.get(doctype, {})
        return SimpleNamespace(get_field=lambda field: SimpleNamespace(label=labels[field]) if field in labels else None)

    def get_all(self, doctype, filters, fields, order_by=None, limit_page_length=0):
        return [_Row(rule) for rule in self.rules
                if all(rule.get(key) == value for key, value in filters.items() if key in rule)]


def _product_version():
    from hero_integration.services import direct_runtime, product_migration

    return f"{product_migration.MAPPING_VERSION}:{direct_runtime.builtin_read('HeroProducts').mapping_version}"


PRODUCT_VERSION = _product_version()


def _link(**values):
    return {"name": "LINK-1", "source_instance": "default", "source_type": "Task", "source_id": "2293879",
            "target_doctype": "Task", "target_name": "TASK-1", "sync_status": "Conflict", **values}


def _run_rules(fake):
    """The rules frozen onto the processing run (here: the fixture's rule list)."""
    return tuple(((rule["target_doctype"], rule["target_field"]), rule["source_path"]) for rule in fake.rules) or None


class TestConflictReport(unittest.TestCase):
    def report(self, fake, name="LINK-1", instance="default"):
        from hero_integration.services import direct_runtime

        builtin = direct_runtime.builtin_read("HeroProducts")
        with patch.object(cr, "frappe", fake), patch.object(cr, "_run_rules", return_value=_run_rules(fake)), \
                patch.object(direct_runtime, "resolve_read", return_value=builtin):
            return cr.report(name, instance)

    def test_field_conflict_names_hero_variable_doctype_field_and_erpnext_value(self):
        fake = _Frappe(links={"LINK-1": _link(conflict_detail="Recheck requested: Task fields require review: description")},
                       docs={("Task", "TASK-1"): {"description": "<p>Dach prüfen</p>"}},
                       rules=[{"operation": "HeroTasks", "status": "Active", "target_field": "description",
                               "target_doctype": "Task", "source_path": "comment"}],
                       labels={"Task": {"description": "Task Description"}})
        report = self.report(fake)
        self.assertEqual((report["kind"], report["hero_id"], report["doctype"], report["record"]),
                         ("field", "2293879", "Task", "TASK-1"))
        self.assertEqual(report["status"], "Recheck requested")
        self.assertEqual(report["problem"], "Task fields require review: description")
        self.assertEqual(report["fields"], [{"doctype": "Task", "field": "description", "label": "Task Description",
                                             "hero_variable": "comment", "value": "Dach prüfen"}])
        self.assertTrue(report["decidable"])

    def test_changed_business_number_shows_old_and_new_hero_value(self):
        detail = ("HERO customer number changed from '1001' to '1002'. Customer KD-1001 keeps its name and number "
                  "until reviewed; set custom_hero_customer_number to '1002' to accept the new number.")
        fake = _Frappe(links={"LINK-1": _link(source_type="customer", target_doctype="Customer", target_name="KD-1001",
                                              conflict_detail=detail)},
                       docs={("Customer", "KD-1001"): {"custom_hero_customer_number": "1001"}},
                       rules=[{"operation": "HeroCustomers", "status": "Active", "target_field": "custom_hero_customer_number",
                               "target_doctype": "Customer", "source_path": "nr"}],
                       labels={"Customer": {"custom_hero_customer_number": "HERO Customer Number"}})
        field = self.report(fake)["fields"][0]
        self.assertEqual((field["hero_variable"], field["value"], field["hero_value"], field["previous_hero_value"]),
                         ("nr", "1001", "1002", "1001"))

    def test_pending_review_names_the_project_customer(self):
        fake = _Frappe(links={"LINK-1": _link(source_type="project", target_doctype="Project", target_name="PV-2628",
                                              sync_status="Pending Review",
                                              conflict_detail="The HERO customer is deleted in HERO; the Project has no customer.")},
                       docs={("Project", "PV-2628"): {"customer": None}}, labels={"Project": {"customer": "Customer"}},
                       rules=[{"operation": "HeroProjectMatches", "status": "Active", "target_field": "customer",
                               "target_doctype": "Project", "source_path": "customer_id"}])
        report = self.report(fake)
        self.assertEqual((report["kind"], report["status"]), ("review", "Confirmation pending"))
        self.assertEqual([(f["hero_variable"], f["field"], f["value"]) for f in report["fields"]],
                         [("customer_id", "customer", None)])
        self.assertIn("released", report["approve_effect"])

    def test_product_conflict_lists_only_the_fields_changed_in_erpnext(self):
        from hero_integration.services.product_migration import digest

        item = {"item_name": "Modul", "description": "Neu in ERPNext", "item_group": "Eigene Artikel",
                "stock_uom": "Stk", "brand": None, "disabled": 0}
        baseline = {field: digest(value or None) for field, value in {**item, "description": "Alt"}.items()}
        fake = _Frappe(links={"LINK-1": _link(source_type="supply_product", target_doctype="Item", target_name="PV-1001", mapping_version=PRODUCT_VERSION,
                                              conflict_detail="The Item was changed in ERPNext; the newer HERO article was not applied.",
                                              target_field_hashes=json.dumps({"prices": {}, "fields": baseline}))},
                       docs={("Item", "PV-1001"): item}, labels={"Item": {"description": "Description"}})
        report = self.report(fake)
        self.assertEqual(report["kind"], "product")
        self.assertEqual([(f["field"], f["hero_variable"], f["value"]) for f in report["fields"]],
                         [("description", "base_data.description", "Neu in ERPNext")])

    def test_price_conflict_names_the_price(self):
        fake = _Frappe(links={"LINK-1": _link(source_type="supply_product", target_doctype="Item", target_name="PV-1001", mapping_version=PRODUCT_VERSION,
                                              conflict_detail="The Standard Selling price PRICE-7 was changed in ERPNext. "
                                                              "The newer HERO article was not applied.")},
                       docs={("Item Price", "PRICE-7"): {"price_list_rate": 120}},
                       labels={"Item Price": {"price_list_rate": "Rate"}})
        field = self.report(fake)["fields"][0]
        self.assertEqual((field["doctype"], field["field"], field["hero_variable"], field["value"]),
                         ("Item Price", "price_list_rate", "sales_prices", "120"))

    def test_product_variable_is_unknown_when_the_mapping_changed_since_the_import(self):
        fake = _Frappe(links={"LINK-1": _link(source_type="supply_product", target_doctype="Item", target_name="PV-1001",
                                              mapping_version="product-migration-v3:older-rules",
                                              conflict_detail="The Standard Buying price PRICE-7 was changed in ERPNext. "
                                                              "The newer HERO article was not applied.")},
                       docs={("Item Price", "PRICE-7"): {"price_list_rate": 120}},
                       labels={"Item Price": {"price_list_rate": "Rate"}})
        self.assertIsNone(self.report(fake)["fields"][0]["hero_variable"])

    def test_other_source_and_closed_links_are_refused(self):
        fake = _Frappe(links={"LINK-1": _link(conflict_detail="x"), "LINK-2": _link(sync_status="Synchronized")})
        with self.assertRaises(fake.PermissionError):
            self.report(fake, instance="source-b")
        with self.assertRaises(fake.ValidationError):
            self.report(fake, name="LINK-2")

    def test_record_that_was_not_imported_has_nothing_to_decide(self):
        fake = _Frappe(records={"REC-1": {"name": "REC-1", "source_type": "customer", "source_id": "301", "run": "RUN-1",
                                          "result": "BLOCKED_RECORD", "detail": "HERO response omits the source of: customer_type."}},
                       rules=[{"operation": "HeroCustomers", "status": "Active", "target_field": "customer_type",
                               "target_doctype": "Customer", "source_path": "type"}],
                       labels={"Customer": {"customer_type": "Customer Type"}})
        with patch.object(cr, "frappe", fake), patch.object(cr, "_run_rules", return_value=_run_rules(fake)) as rules:
            report = cr.record_report("REC-1", "default")
        rules.assert_called_once_with("customer", "301", "default", run="RUN-1")
        self.assertFalse(report["decidable"])
        self.assertEqual((report["status"], report["doctype"]), ("Not imported", "Customer"))
        self.assertEqual([(f["hero_variable"], f["label"], f["value"]) for f in report["fields"]],
                         [("type", "Customer Type", None)])


class TestHeroVariableComesFromTheProcessingRun(unittest.TestCase):
    """Runs of source A (rule title) and a newer run of source B (rule foreign_note)
    processed the same HERO Task id 42."""

    def setUp(self):
        from hero_integration.services.direct_mapping import MappingSpec

        self.runs = {"RUN-A": SimpleNamespace(name="RUN-A", source_instance="source-a", graphql_mapping="MAP-A"),
                     "RUN-B": SimpleNamespace(name="RUN-B", source_instance="source-b", graphql_mapping="MAP-B")}
        self.plans = {"RUN-A": ("title", "Task", "description"), "RUN-B": ("foreign_note", "Task", "description")}
        self.records = [("RUN-A", "2026-11-01"), ("RUN-B", "2026-11-02")]  # B is newer
        self.spec = MappingSpec

    def rules(self, instance, run=None, error=None):
        def sql(query, params):
            source_type, source_id, wanted = params
            matching = [(name, created) for name, created in self.records
                        if self.runs[name].source_instance == wanted and self.runs[name].graphql_mapping]
            return [(max(matching, key=lambda item: item[1])[0],)] if matching else []

        def plan(run_doc, operation):
            if error:
                raise error
            path, doctype, field = self.plans[run_doc.name]
            return SimpleNamespace(definitions=(self.spec("HeroTasks", path, doctype, field, "string|null"),))

        fake = SimpleNamespace(db=SimpleNamespace(sql=sql, exists=lambda doctype, name: name in self.runs),
                               get_doc=lambda doctype, name: self.runs[name])
        with patch.object(cr, "frappe", fake), \
                patch("hero_integration.services.migration._mapping_plan_for_run", side_effect=plan):
            return cr._run_rules("Task", "42", instance, run=run)

    def test_a_newer_run_of_another_source_is_never_used(self):
        self.assertEqual(cr._hero_variable("Task", "Task", "description", self.rules("source-a")), "title")
        self.assertEqual(cr._hero_variable("Task", "Task", "description", self.rules("source-b")), "foreign_note")

    def test_an_explicit_run_of_another_source_is_unknown(self):
        self.assertIsNone(self.rules("source-a", run="RUN-B"))
        self.assertIsNotNone(self.rules("source-a", run="RUN-A"))

    def test_unknown_when_the_run_cannot_be_reconstructed(self):
        self.assertIsNone(self.rules("source-a", error=RuntimeError("revision changed")))
        self.runs["RUN-A"].graphql_mapping = ""
        self.assertIsNone(self.rules("source-a"))  # without a frozen mapping no run qualifies
        self.assertIsNone(self.rules("source-c"))
        self.assertIsNone(cr._hero_variable("Task", "Task", "description", None))

    def test_report_passes_the_link_source_to_the_run_selection(self):
        fake = _Frappe(links={"LINK-1": _link(source_instance="source-a", conflict_detail="Task fields require review: description")},
                       docs={("Task", "TASK-1"): {"description": "x"}}, labels={"Task": {"description": "Description"}})
        with patch.object(cr, "frappe", fake), patch.object(cr, "_run_rules", return_value=None) as rules:
            cr.report("LINK-1", "source-a")
        rules.assert_called_once_with("Task", "2293879", "source-a")


class TestReportPage(unittest.TestCase):
    def test_conflict_rows_end_with_a_report_button_and_the_dialog_can_decide_or_close(self):
        from pathlib import Path

        source = (Path(cr.__file__).parents[1] / "public/js/hero_migration.js").read_text()
        self.assertIn('"Approve / Decline", "Report"]', source)
        self.assertIn('call("sync_page_conflict_report"', source)
        for contract in ('decide("approve")', 'decide("reject")', 'tr("Close")', 'dialog.showModal()',
                         '"HERO variable", "Doctype field", "Value in ERPNext"'):
            self.assertIn(contract, source)


if __name__ == "__main__":
    unittest.main()
