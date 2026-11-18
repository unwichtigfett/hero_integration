"""Offline Project read-boundary tests; no Frappe site or database is opened."""
from __future__ import annotations

import ast
import csv
from copy import deepcopy
import importlib.util
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SERVICE = Path(__file__).parents[1] / "services" / "project_workspace.py"


class PermissionDenied(Exception):
    pass


class MissingDocument(Exception):
    pass


class Document:
    def __init__(self, doctype, name, *, allowed=True, protected=(), **values):
        self.doctype, self.name = doctype, name
        self.values = {"modified": "2026-11-16 12:00:00", **values}
        self.allowed, self.protected = allowed, protected
        self.checked, self.filtered = False, False

    def check_permission(self, permission):
        assert permission == "read"
        self.checked = True
        if not self.allowed:
            raise PermissionDenied()

    def apply_fieldlevel_read_permissions(self):
        assert self.checked
        self.filtered = True
        for field in self.protected:
            self.values.pop(field, None)

    def get(self, field):
        return self.values.get(field)


class Frappe:
    PermissionError, DoesNotExistError = PermissionDenied, MissingDocument

    def __init__(self):
        self.session = SimpleNamespace(user="reader@example.invalid")
        self.documents, self.calls, self.reads, self.exposed = {}, [], [], []
        self.candidates = {}
        self.denied_lists = set()
        self.add(Document("Project", "PROJECT-1", customer="CUSTOMER-1"))

    def add(self, document):
        self.documents[document.doctype, document.name] = document
        return document

    def whitelist(self, **options):
        self.exposed.append(options)
        return lambda function: function

    @staticmethod
    def _(text):
        return text

    @staticmethod
    def throw(message, exception=ValueError):
        raise exception(message)

    def get_doc(self, doctype, name):
        self.reads.append((doctype, name))
        if (doctype, name) not in self.documents:
            raise MissingDocument()
        return self.documents[doctype, name]

    def get_list(self, doctype, **kwargs):
        assert self.documents["Project", "PROJECT-1"].checked
        self.calls.append((doctype, deepcopy(kwargs)))
        if doctype in self.denied_lists:
            raise PermissionDenied()
        rows = self.candidates.get(doctype, [])
        start, size = kwargs.get("limit_start", 0), kwargs["limit_page_length"]
        return [{"name": name} for name in rows[start:start + size]]


def load(fake):
    spec = importlib.util.spec_from_file_location("project_workspace_offline", SERVICE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"frappe": fake}):
        spec.loader.exec_module(module)
    return module


class ProjectWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fake = Frappe()
        self.service = load(self.fake)

    def panel(self, section, **kwargs):
        return self.service.get_panel("PROJECT-1", section, **kwargs)

    def candidate(self, doctype, name="ROW-1", **values):
        self.fake.add(Document(doctype, name, **values))
        self.fake.candidates.setdefault(doctype, []).append(name)

    def test_authenticated_get_only_contract(self):
        self.assertEqual(self.fake.exposed, [{"methods": ["GET"]}])
        self.fake.session.user = "Guest"
        with self.assertRaises(PermissionDenied):
            self.panel("details")
        self.assertEqual(self.fake.reads, [])

    def test_project_permission_precedes_any_related_query(self):
        self.fake.documents["Project", "PROJECT-1"].allowed = False
        with self.assertRaises(PermissionDenied):
            self.panel("tasks")
        self.assertEqual(self.fake.calls, [])

    def test_invalid_inputs_cannot_select_arbitrary_sources_or_unbounded_pages(self):
        cases = [
            {"section": "User"}, {"section": "tasks", "start": -1},
            {"section": "tasks", "start": True}, {"section": "tasks", "start": "1.5"},
            {"section": "tasks", "start": 100001}, {"section": "tasks", "start": "９"},
            {"section": "tasks", "start": "9" * 5000}, {"section": "tasks", "start": ""},
            {"section": "tasks", "search": "x" * 121},
            {"section": "tasks", "status": "Invented"}, {"section": "images", "status": "Open"},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.service.get_panel("PROJECT-1", **case)
        self.assertEqual(self.fake.reads, [])

    def test_customer_and_primary_contact_are_authorized_but_unused_address_is_not_read(self):
        self.fake.add(Document("Customer", "CUSTOMER-1", customer_name="Synthetic customer",
            customer_primary_address="ADDR-1", customer_primary_contact="CONTACT-1"))
        self.fake.add(Document("Address", "ADDR-1", address_line1="Example Street", city="Example City",
            protected=("address_line2",), address_line2="Protected"))
        self.fake.add(Document("Contact", "CONTACT-1", first_name="Example",
            protected=("email_id",), email_id="private@example.invalid"))
        context = self.panel("details")["context"]
        self.assertEqual(context["customer"]["customer_name"], "Synthetic customer")
        self.assertIsNone(context["address"])
        self.assertIsNone(context["contact"]["email_id"])
        self.assertTrue(self.fake.documents["Customer", "CUSTOMER-1"].filtered)
        self.assertTrue(self.fake.documents["Contact", "CONTACT-1"].filtered)
        self.assertFalse(self.fake.documents["Address", "ADDR-1"].filtered)
        self.assertNotIn(("Address", "ADDR-1"), self.fake.reads)
        self.assertNotIn("site_address", context)

    def test_protected_project_customer_link_is_not_followed(self):
        self.fake.documents["Project", "PROJECT-1"].protected = ("customer",)
        self.assertIsNone(self.panel("details")["context"]["customer"])
        self.assertEqual(self.fake.reads, [("Project", "PROJECT-1")])

    def test_inaccessible_or_missing_customer_is_not_exposed(self):
        for allowed in (False, None):
            with self.subTest(allowed=allowed):
                if allowed is False:
                    self.fake.add(Document("Customer", "CUSTOMER-1", allowed=False))
                else:
                    self.fake.documents.pop(("Customer", "CUSTOMER-1"), None)
                self.assertEqual(self.panel("details")["context"],
                    {"customer": None, "address": None, "contact": None})

    def test_task_query_is_bounded_permission_aware_and_project_scoped(self):
        self.candidate("Task", project="PROJECT-1", subject="Synthetic task", status="Working", password="secret")
        result = self.panel("tasks", search="task", status="Working")
        self.assertEqual(result["rows"][0]["subject"], "Synthetic task")
        self.assertNotIn("password", result["rows"][0])
        query = self.fake.calls[0][1]
        self.assertEqual(query["filters"], {"project": "PROJECT-1", "status": "Working"})
        self.assertEqual(query["limit_page_length"], 21)
        self.assertEqual(query["fields"], ["name"])
        self.assertNotIn("ignore_permissions", query)

    def test_record_and_field_permissions_can_suppress_individual_candidates(self):
        self.candidate("Task", "TASK-1", project="PROJECT-1", allowed=False)
        self.candidate("Task", "TASK-2", project="PROJECT-1", protected=("project",))
        self.candidate("Task", "TASK-3", project="OTHER")
        self.candidate("Task", "TASK-4", project="PROJECT-1", protected=("subject",), subject="Protected")
        rows = self.panel("tasks")["rows"]
        self.assertEqual([row["name"] for row in rows], ["TASK-4"])
        self.assertIsNone(rows[0]["subject"])

    def test_missing_candidate_is_skipped_without_retrying_or_failing_whole_panel(self):
        self.fake.candidates["Task"] = ["DELETED"]
        self.assertEqual(self.panel("tasks")["rows"], [])
        self.assertEqual(self.fake.reads.count(("Task", "DELETED")), 1)

    def test_subordinate_permission_denial_is_an_explicit_state(self):
        self.fake.denied_lists.add("Task")
        result = self.panel("tasks")
        self.assertEqual(result["state"], "permission_denied")
        self.assertEqual(result["rows"], [])

    def test_unexpected_query_failures_are_not_misreported_as_empty_or_denied(self):
        self.fake.get_list = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("query failed"))
        with self.assertRaisesRegex(RuntimeError, "query failed"):
            self.panel("tasks")

    def test_pagination_uses_candidate_offsets_even_when_permission_hides_rows(self):
        for index in range(43):
            self.candidate("Task", f"TASK-{index}", project="PROJECT-1", allowed=index % 3 != 0)
        first, second, third = (self.panel("tasks", start=start) for start in (0, 20, 40))
        self.assertEqual((first["next_start"], second["next_start"], third["next_start"]), (20, 40, None))
        names = [row["name"] for page in (first, second, third) for row in page["rows"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), sum(index % 3 != 0 for index in range(43)))

    def test_file_split_parent_scope_privacy_and_no_filename_category_guessing(self):
        for name, kind in (("Offer.pdf", "PDF"), ("roof.png", "PNG"), ("unknown", None), ("active.svg", "SVG")):
            self.candidate("File", name, attached_to_doctype="Project", attached_to_name="PROJECT-1",
                file_type=kind, file_name=name, is_private=1, file_url="/private/files/" + name)
        self.candidate("File", "OTHER", attached_to_doctype="Project", attached_to_name="OTHER", file_type="PDF")
        documents = self.panel("documents")["rows"]
        self.assertEqual([row["name"] for row in documents], ["Offer.pdf", "unknown", "active.svg"])
        self.assertEqual([row["name"] for row in self.panel("images")["rows"]], ["roof.png"])
        self.assertTrue(all(row["is_private"] for row in documents))
        self.assertFalse(any("category" in row for row in documents))
        self.assertEqual(self.fake.calls[0][1]["filters"]["attached_to_name"], "PROJECT-1")
        self.assertEqual(self.fake.calls[0][1]["or_filters"][1], ["File", "file_type", "is", "not set"])

    def test_unsafe_or_external_file_urls_are_not_exposed_for_preview(self):
        for value in ("https://example.invalid/a.pdf", "javascript:alert(1)", "//example.invalid/a",
                      "/files/../secret", "/files/%2e%2e/secret", "/files/a%00.pdf",
                      "/files/a%09.pdf", "/files/a\\b"):
            with self.subTest(value=value):
                self.assertFalse(self.service._local_file_url(value))
        for value in ("/files/safe.pdf", "/private/files/safe%20image.png"):
            self.assertTrue(self.service._local_file_url(value))

    def test_event_references_require_exact_doctype_and_name_after_field_filtering(self):
        self.candidate("Event", "DIRECT", reference_doctype="Project", reference_docname="PROJECT-1")
        self.candidate("Event", "LINKED", links=[{"link_doctype": "Project", "link_name": "PROJECT-1"}],
            event_participants=[{"reference_doctype": "User", "reference_docname": "worker@example.invalid"}])
        self.candidate("Event", "WRONG-TYPE", reference_doctype="Customer", reference_docname="PROJECT-1")
        self.candidate("Event", "WRONG-LINK", links=[{"link_doctype": "Customer", "link_name": "PROJECT-1"}])
        self.candidate("Event", "PROTECTED-LINK", protected=("links",),
            links=[{"link_doctype": "Project", "link_name": "PROJECT-1"}])
        rows = self.panel("appointments")["rows"]
        self.assertEqual([row["name"] for row in rows], ["DIRECT", "LINKED"])
        self.assertEqual(rows[1]["participants"][0]["name"], "worker@example.invalid")
        self.assertEqual(self.fake.calls[-1][1]["or_filters"], [
            ["Event", "reference_docname", "=", "PROJECT-1"],
            ["Dynamic Link", "link_name", "=", "PROJECT-1"],
        ])

    def test_todo_assignments_are_not_project_membership_or_task_assignments(self):
        self.candidate("ToDo", "DIRECT", reference_type="Project", reference_name="PROJECT-1", allocated_to="reader")
        self.candidate("ToDo", "TASK", reference_type="Task", reference_name="PROJECT-1")
        result = self.panel("assignments")
        self.assertEqual([row["name"] for row in result["rows"]], ["DIRECT"])
        self.assertEqual(self.fake.calls[0][1]["filters"],
            {"reference_type": "Project", "reference_name": "PROJECT-1"})

    def test_image_category_filter_and_visible_classification_are_native_file_fields(self):
        self.candidate("File", "IMAGE", attached_to_doctype="Project", attached_to_name="PROJECT-1",
            file_type="PNG", custom_project_image_category="Dach")
        row = self.panel("images", category="Dach")["rows"][0]
        self.assertEqual(row["category"], "Dach")
        self.assertEqual([call for kind, call in self.fake.calls if kind == "File"][-1]["filters"]["custom_project_image_category"], "Dach")
        self.panel("images", uncategorized=1)
        self.assertEqual([call for kind, call in self.fake.calls if kind == "File"][-1]["filters"]["custom_project_image_category"], ["is", "not set"])
        with self.assertRaises(ValueError):
            self.panel("images", category="Dach", uncategorized=1)
        with self.assertRaises(ValueError):
            self.panel("tasks", category="Dach")

    def test_new_literal_messages_and_labels_have_nonempty_german_translations(self):
        app = SERVICE.parents[1]
        source = (app / "public/js/project_workspace.js").read_text()
        keys = set(re.findall(r'(?:__|note|button)\("([^"\\]*)"', source))
        for line in source.splitlines():
            if re.match(r'\s*\["(?:__details|custom_project_documents_tab|custom_project_images_tab|'
                        r'more_info_tab|connections_tab|monitor_progress_tab|costing_tab)"', line):
                keys.add(re.findall(r'"([^"\\]*)"', line)[-1])
        for match in re.finditer(r'table\(\[([^\]]*)\]', source):
            keys.update(re.findall(r'"([^"\\]*)"', match.group(1)))
        for path in [SERVICE, SERVICE.parent / "project_image_categories.py", SERVICE.parent / "project_site_address.py",
                SERVICE.parent / "project_address.py",
                SERVICE.parent / "project_pdf_preview.py", SERVICE.parent / "project_responsibility.py",
                SERVICE.parent / "project_workspace_layout.py",
                SERVICE.parent / "design_mode.py",
                app / "hero_integration/doctype/project_image_category/project_image_category.py"]:
            for item in ast.walk(ast.parse(path.read_text())):
                if (isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                        and item.func.attr == "_" and item.args and isinstance(item.args[0], ast.Constant)):
                    keys.add(item.args[0].value)
        with (app / "translations/de.csv").open() as file:
            translations = {row[0]: row[1] for row in csv.reader(file) if len(row) >= 2}
        self.assertEqual(sorted(key for key in keys if not translations.get(key)), [])


if __name__ == "__main__":
    unittest.main()
