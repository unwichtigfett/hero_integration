"""Remember for Mapping: shared node bookmarks of API Analyzer and Graph Viewer."""
from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import frappe  # noqa: F401
except ModuleNotFoundError:
    frappe = types.ModuleType("frappe")
    frappe.whitelist = lambda *args, **kwargs: (lambda function: function)
    sys.modules["frappe"] = frappe

from hero_integration import api
from hero_integration.services.node_bookmark import NodeBookmarkError, mapping_context, normalize_alias


def _named(kind, name):
    return {"kind": kind, "name": name, "ofType": None}


def _nn(ref):
    return {"kind": "NON_NULL", "name": None, "ofType": ref}


def _list(ref):
    return {"kind": "LIST", "name": None, "ofType": ref}


SCHEMA = {"__schema": {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "types": [
    {"kind": "OBJECT", "name": "Query", "fields": [
        {"name": "projects", "args": [{"name": "first", "type": _named("SCALAR", "Int")}], "type": _list(_named("OBJECT", "Project"))},
        {"name": "archived_projects", "args": [], "type": _list(_named("OBJECT", "Project"))},
        {"name": "contact", "args": [], "type": _named("OBJECT", "Contact")},
    ]},
    {"kind": "OBJECT", "name": "Mutation", "fields": [
        {"name": "update_project", "args": [{"name": "input", "type": _nn(_named("INPUT_OBJECT", "ProjectInput"))}],
         "type": _named("OBJECT", "Project")}]},
    {"kind": "OBJECT", "name": "Project", "fields": [{"name": "title", "args": [], "type": _named("SCALAR", "String")}]},
    {"kind": "OBJECT", "name": "Contact", "fields": [{"name": "email", "args": [], "type": _named("SCALAR", "String")}]},
    {"kind": "OBJECT", "name": "Orphan", "fields": [{"name": "note", "args": [], "type": _named("SCALAR", "String")}]},
    {"kind": "INPUT_OBJECT", "name": "ProjectInput", "inputFields": [{"name": "title", "type": _named("SCALAR", "String")}]},
    {"kind": "SCALAR", "name": "String"}, {"kind": "SCALAR", "name": "Int"},
]}}


class TestNodeBookmarkHelpers(unittest.TestCase):
    def test_alias_is_required_trimmed_bounded_and_text_only(self):
        self.assertEqual(normalize_alias("  HERO   Customer Reference "), ("HERO Customer Reference", "hero customer reference"))
        for value in ("", "   ", None, "x" * 81, "bad\nalias"):
            with self.subTest(value=value), self.assertRaises(NodeBookmarkError):
                normalize_alias(value)
        self.assertEqual(normalize_alias("<b>x</b>")[0], "<b>x</b>", "kept as text; the UI renders it with textContent")

    def test_mapping_context_comes_from_schema_edges_only(self):
        operation = mapping_context(SCHEMA, "operation:query:contact")
        self.assertEqual((operation["mappable"], operation["operation"], operation["path"]), (True, "contact", ""))
        root_field = mapping_context(SCHEMA, "field:Query.projects")
        self.assertEqual((root_field["operation_kind"], root_field["operation"]), ("QUERY", "projects"))
        single = mapping_context(SCHEMA, "field:Contact.email")
        self.assertEqual((single["mappable"], single["operation"], single["path"]), (True, "contact", "email"))
        ambiguous = mapping_context(SCHEMA, "field:Project.title")
        self.assertFalse(ambiguous["mappable"])
        self.assertEqual({item["operation"] for item in ambiguous["candidates"]}, {"projects", "archived_projects"})
        none = mapping_context(SCHEMA, "field:Orphan.note")
        self.assertEqual((none["mappable"], none["candidates"]), (False, []))
        argument = mapping_context(SCHEMA, "argument:Mutation.update_project.input")
        self.assertEqual((argument["mappable"], argument["operation_kind"], argument["operation"], argument["path"]),
                         (True, "MUTATION", "update_project", "input"))
        query_argument = mapping_context(SCHEMA, "argument:Query.projects.first")
        self.assertEqual((query_argument["mappable"], query_argument["operation"], query_argument["path"]), (False, "projects", ""))
        input_field = mapping_context(SCHEMA, "input_field:ProjectInput.title")
        self.assertEqual((input_field["mappable"], input_field["operation"], input_field["path"]), (True, "update_project", "input.title"))
        for node in ("type:Project", "type:String", "root:query"):
            with self.subTest(node=node):
                self.assertFalse(mapping_context(SCHEMA, node)["mappable"])
        with self.assertRaises(NodeBookmarkError):
            mapping_context(SCHEMA, "field:Project.unknown")


@unittest.skipUnless(hasattr(frappe, "__path__"), "Frappe controller requires the framework package")
class TestNodeBookmarkController(unittest.TestCase):
    def test_owner_alias_and_existing_identity_validation(self):
        from hero_integration.hero_integration.doctype.hero_graphql_node_bookmark import hero_graphql_node_bookmark as controller

        fake = _Frappe()
        doc = SimpleNamespace(owner=fake.session.user, alias="  Project   title  ", is_new=lambda: False,
                              has_value_changed=lambda field: False)
        with patch.object(controller, "frappe", fake):
            controller.HEROGraphQLNodeBookmark.validate(doc)
            self.assertEqual((doc.alias, doc.alias_key), ("Project title", "project title"))
            doc.has_value_changed = lambda field: field == "schema_fingerprint"
            with self.assertRaisesRegex(ValueError, "identity is immutable"):
                controller.HEROGraphQLNodeBookmark.validate(doc)
            doc.owner = "someone-else@example.com"
            with self.assertRaises(PermissionError):
                controller.HEROGraphQLNodeBookmark.validate(doc)


class _Doc(SimpleNamespace):
    def __init__(self, store, values):
        super().__init__(**values)
        self._store = store

    def insert(self):
        self.name = self.bookmark_key
        self.owner = self._store.session.user
        if self.name in self._store.rows:
            raise ValueError("duplicate key")
        self._store.rows[self.name] = self
        return self

    def save(self):
        self._store.saves += 1

    def as_dict(self):
        return {key: value for key, value in vars(self).items() if not key.startswith("_")}


class _Frappe:
    PermissionError = PermissionError

    def __init__(self):
        self.session = SimpleNamespace(user="admin@example.com")
        self.rows, self.saves, self.touched = {}, 0, set()
        self.snapshots = [{"name": "SNAP-2", "source_instance": "hero"}, {"name": "SNAP-1", "source_instance": "hero"}]

    def only_for(self, role):
        assert role == "System Manager"

    def throw(self, message, exc=None):
        raise (exc or ValueError)(message)

    def get_all(self, doctype, filters=None, fields=None, order_by=None):
        self.touched.add(doctype)
        if doctype == "HERO GraphQL Schema Snapshot":
            return list(self.snapshots)
        rows = [doc.as_dict() for doc in self.rows.values()]
        return [row for row in rows if all(row.get(key) == value for key, value in (filters or {}).items())]

    def get_doc(self, doctype, name=None):
        if isinstance(doctype, dict):
            self.touched.add(doctype["doctype"])
            return _Doc(self, {key: value for key, value in doctype.items() if key != "doctype"})
        self.touched.add(doctype)
        return self.rows[name]

    def delete_doc(self, doctype, name):
        self.touched.add(doctype)
        del self.rows[name]


class TestNodeBookmarkApi(unittest.TestCase):
    def setUp(self):
        self.frappe = _Frappe()
        self.snapshots = {"SNAP-1": SimpleNamespace(name="SNAP-1", schema_fingerprint="fp1", source_instance="hero"),
                          "SNAP-2": SimpleNamespace(name="SNAP-2", schema_fingerprint="fp2", source_instance="hero")}

        def validated(name, **_kwargs):
            if name not in self.snapshots:
                raise ValueError("The GraphQL schema snapshot is not valid.")
            return self.snapshots[name], SCHEMA

        for patcher in (patch.object(api, "frappe", self.frappe), patch.object(api, "_validated_snapshot", validated)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def remember(self, node="field:Contact.email", alias="HERO Customer Mail", snapshot="SNAP-2", fingerprint="fp2"):
        return api.workspace_node_bookmark_save(snapshot, node, alias, fingerprint)

    def test_analyzer_and_graph_viewer_store_the_same_server_validated_identity(self):
        first = self.remember()
        second = self.remember()  # same node selected in the other tool
        self.assertEqual((first["existing"], second["existing"]), (False, True))
        self.assertEqual(len(self.frappe.rows), 1)
        stored = self.frappe.rows[first["bookmark"]]
        self.assertEqual((stored.node_id, stored.node_kind, stored.node_name, stored.schema_fingerprint),
                         ("field:Contact.email", "FIELD", "Contact.email", "fp2"))
        self.assertEqual(self.frappe.touched - {"HERO GraphQL Node Bookmark"}, set(), "no mapping, service or draft is touched")

    def test_stale_selection_unknown_nodes_and_conflicts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "schema changed"):
            self.remember(fingerprint="fp-old")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.remember(node="field:Contact.phone")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.remember(node="OBJECT")  # an overview category card is not a node
        self.remember()
        with self.assertRaisesRegex(ValueError, "already remembered"):
            self.remember(alias="Another alias")
        with self.assertRaisesRegex(ValueError, "alias is already used"):
            self.remember(node="field:Project.title", alias="hero customer mail")
        self.assertEqual(len(self.frappe.rows), 1)

    def test_list_rename_remove_and_owner_isolation(self):
        name = self.remember()["bookmark"]
        self.remember(node="operation:query:contact", alias="Contacts")
        listed = api.workspace_node_bookmark_list()["bookmarks"]
        self.assertEqual([row["alias"] for row in listed], ["Contacts", "HERO Customer Mail"])
        self.assertEqual(api.workspace_node_bookmark_list(search="contact.email")["bookmarks"][0]["name"], name)
        api.workspace_node_bookmark_rename(name, "Customer e-mail")
        self.assertEqual(self.frappe.rows[name].alias, "Customer e-mail")
        self.frappe.session.user = "other@example.com"
        self.assertEqual(api.workspace_node_bookmark_list()["bookmarks"], [])
        for action in (lambda: api.workspace_node_bookmark_rename(name, "Hijack"), lambda: api.workspace_node_bookmark_delete(name),
                       lambda: api.workspace_node_bookmark_resolve(name)):
            with self.assertRaises(PermissionError):
                action()
        self.frappe.session.user = "admin@example.com"
        api.workspace_node_bookmark_delete(name)
        self.assertNotIn(name, self.frappe.rows)

    def test_validity_is_current_outdated_or_unresolvable_without_remapping(self):
        current = self.remember()["bookmark"]
        outdated = self.remember(snapshot="SNAP-1", fingerprint="fp1", alias="Old mail")["bookmark"]
        states = {row["name"]: row["status"] for row in api.workspace_node_bookmark_list()["bookmarks"]}
        self.assertEqual((states[current], states[outdated]), ("CURRENT", "OUTDATED"))
        self.assertEqual(self.frappe.rows[outdated].schema_snapshot, "SNAP-1", "never silently remapped")
        del self.snapshots["SNAP-1"]
        states = {row["name"]: row["status"] for row in api.workspace_node_bookmark_list()["bookmarks"]}
        self.assertEqual(states[outdated], "UNRESOLVABLE")
        self.assertIsNone(api.workspace_node_bookmark_resolve(outdated)["context"])

    def test_resolve_returns_preselection_only_for_unambiguous_paths(self):
        mail = self.remember()["bookmark"]
        title = self.remember(node="field:Project.title", alias="Project title")["bookmark"]
        mail_context = api.workspace_node_bookmark_resolve(mail)["context"]
        self.assertEqual((mail_context["mappable"], mail_context["operation"], mail_context["path"]), (True, "contact", "email"))
        title_context = api.workspace_node_bookmark_resolve(title)["context"]
        self.assertFalse(title_context["mappable"])
        self.assertEqual(len(title_context["candidates"]), 2)
        self.assertEqual(self.frappe.saves, 0)


if __name__ == "__main__":
    unittest.main()
