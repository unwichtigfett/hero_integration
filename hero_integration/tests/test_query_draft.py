from __future__ import annotations

import copy
import unittest

from hero_integration.services.query_draft import QueryDraftError, validate, validate_variables


def _named(kind, name):
    return {"kind": kind, "name": name, "ofType": None}


def _non_null(ref):
    return {"kind": "NON_NULL", "name": None, "ofType": ref}


def _list(ref):
    return {"kind": "LIST", "name": None, "ofType": ref}


SCHEMA = {"__schema": {
    "queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"},
    "types": [
        {"kind": "OBJECT", "name": "Query", "fields": [
            {"name": "contacts", "args": [
                {"name": "first", "type": _named("SCALAR", "Int"), "defaultValue": None},
                {"name": "filter", "type": _named("INPUT_OBJECT", "ContactFilter"), "defaultValue": None},
            ], "type": _list(_named("OBJECT", "Contact"))},
            {"name": "contact", "args": [{"name": "id", "type": _non_null(_named("SCALAR", "ID")), "defaultValue": None}],
             "type": _named("OBJECT", "Contact")},
            {"name": "search", "args": [], "type": _list(_named("UNION", "SearchResult"))},
            {"name": "version", "args": [], "type": _named("SCALAR", "String")},
        ]},
        {"kind": "OBJECT", "name": "Mutation", "fields": [
            {"name": "changeContact", "args": [], "type": _named("OBJECT", "Contact")}]},
        {"kind": "OBJECT", "name": "Contact", "fields": [
            {"name": "id", "args": [], "type": _non_null(_named("SCALAR", "ID"))},
            {"name": "email", "args": [], "type": _named("SCALAR", "String")},
            {"name": "status", "args": [], "type": _named("ENUM", "Status")},
        ]},
        {"kind": "OBJECT", "name": "Project", "fields": [{"name": "title", "args": [], "type": _named("SCALAR", "String")}]},
        {"kind": "UNION", "name": "SearchResult", "possibleTypes": [_named("OBJECT", "Contact"), _named("OBJECT", "Project")]},
        {"kind": "ENUM", "name": "Status", "enumValues": [{"name": "ACTIVE"}, {"name": "ARCHIVED"}]},
        {"kind": "INPUT_OBJECT", "name": "ContactFilter", "inputFields": [
            {"name": "status", "type": _named("ENUM", "Status"), "defaultValue": None},
            {"name": "email", "type": _non_null(_named("SCALAR", "String")), "defaultValue": None},
        ]},
        {"kind": "SCALAR", "name": "Int"}, {"kind": "SCALAR", "name": "ID"},
        {"kind": "SCALAR", "name": "String"}, {"kind": "SCALAR", "name": "Boolean"},
    ],
}}


class TestQueryDraft(unittest.TestCase):
    def test_valid_query_with_variables_alias_fragment_and_directive(self):
        document = """
        query Contacts($first: Int, $filter: ContactFilter, $withMail: Boolean!) {
          contacts(first: $first, filter: $filter) { key: id email @include(if: $withMail) status }
          search { __typename ... on Project { title } ... on Contact { id } }
        }"""
        detail = validate(SCHEMA, document)
        self.assertEqual(detail["operation_name"], "Contacts")
        self.assertEqual(detail["root_fields"], ["contacts", "search"])
        self.assertEqual(detail["required_variables"], ["withMail"])
        self.assertEqual(detail["variables"], {"first": "Int", "filter": "ContactFilter", "withMail": "Boolean!"})

    def test_anonymous_query_and_literals_are_checked_against_schema(self):
        validate(SCHEMA, '{ contacts(first: 5, filter: {status: ACTIVE, email: "a@b"}) { id } }')
        for document, message in (
            ('{ contacts(first: "five") { id } }', "Int"),
            ('{ contacts(filter: {status: GONE, email: "x"}) { id } }', "enum"),
            ("{ contacts(filter: {status: ACTIVE}) { id } }", "Required input field"),
            ("{ contacts(filter: {unknown: 1, email: \"x\"}) { id } }", "unknown"),
        ):
            with self.subTest(document=document), self.assertRaisesRegex(QueryDraftError, message):
                validate(SCHEMA, document)

    def test_schema_structure_violations_are_rejected(self):
        cases = {
            "{ contacts { phone } }": "does not exist",
            "{ contact { id } }": "Required argument",
            "{ contacts }": "needs a selection set",
            "{ version { length } }": "leaf type",
            "{ contacts(page: 1) { id } }": "Argument page does not exist",
            "{ search { id } }": "union",
            "{ __schema { types { name } } }": "Introspection",
            "query Q($id: Contact) { contact(id: $id) { id } }": "input type",
            "query Q($id: String!) { contact(id: $id) { id } }": "cannot be used",
            "query Q($id: ID!, $unused: Int) { contact(id: $id) { id } }": "Unused",
            "query Q { contact(id: $id) { id } }": "not defined",
            "{ contacts { ...ContactFields } }": "Named fragment",
            "{ contacts { id @deprecated } }": "not supported",
        }
        for document, message in cases.items():
            with self.subTest(document=document), self.assertRaisesRegex(QueryDraftError, message):
                validate(SCHEMA, document)

    def test_mutations_subscriptions_and_multiple_operations_are_rejected(self):
        for document in ("mutation { changeContact { id } }", "subscription { contacts { id } }",
                         "{ version } { version }", "query A { version } query B { version }",
                         "fragment F on Contact { id }"):
            with self.subTest(document=document), self.assertRaises(QueryDraftError):
                validate(SCHEMA, document)

    def test_depth_and_field_limits(self):
        nested = "{ " + "contacts { " * 1 + "id" + " }" * 1 + " }"
        validate(SCHEMA, nested)
        many = "{ contacts { " + " ".join(f"a{i}: id" for i in range(501)) + " } }"
        with self.assertRaisesRegex(QueryDraftError, "more than"):
            validate(SCHEMA, many)

    def test_recursive_selection_depth_is_bounded_at_twelve_levels(self):
        schema = copy.deepcopy(SCHEMA)
        contact = next(item for item in schema["__schema"]["types"] if item["name"] == "Contact")
        contact["fields"].append({"name": "friend", "args": [], "type": _named("OBJECT", "Contact")})

        def document(depth):
            return "{ contacts { " + "friend { " * depth + "id" + " }" * depth + " } }"

        validate(schema, document(10))
        with self.assertRaisesRegex(QueryDraftError, "nested deeper than 12"):
            validate(schema, document(11))

    def test_runtime_variables_are_validated_and_coerced(self):
        document = "query Q($id: ID!, $first: Int, $filter: ContactFilter) { contact(id: $id) { id } contacts(first: $first, filter: $filter) { id } }"
        self.assertEqual(validate_variables(SCHEMA, document, {"id": "7", "first": 2}), {"id": "7", "first": 2})
        for values, message in (
            ({}, "Required query variable"),
            ({"id": "7", "first": "2"}, "Int"),
            ({"id": "7", "other": 1}, "Unknown query variable"),
            ({"id": "7", "filter": {"status": "ACTIVE"}}, "Required input field"),
            ([], "JSON object"),
        ):
            with self.subTest(values=values), self.assertRaisesRegex(QueryDraftError, message):
                validate_variables(SCHEMA, document, values)

    def test_default_values_make_variables_optional(self):
        document = "query Q($first: Int = 10, $id: ID! = \"1\") { contacts(first: $first) { id } contact(id: $id) { id } }"
        self.assertEqual(validate(SCHEMA, document)["required_variables"], [])
        self.assertEqual(validate_variables(SCHEMA, document, {}), {})


if __name__ == "__main__":
    unittest.main()
