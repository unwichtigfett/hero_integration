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


from graphql import build_schema, introspection_from_schema
SCHEMA = introspection_from_schema(build_schema('type Query { contacts(first:Int, filter:ContactFilter):[Contact] contact(id:ID!):Contact search:[SearchResult] version:String } type Mutation { changeContact:Contact } type Contact { id:ID! email:String status:Status } type Project { title:String } union SearchResult = Contact | Project enum Status { ACTIVE ARCHIVED } input ContactFilter { status:Status email:String! }'))


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
            ("{ contacts(filter: {status: ACTIVE}) { id } }", "required type"),
            ("{ contacts(filter: {unknown: 1, email: \"x\"}) { id } }", "unknown"),
        ):
            with self.subTest(document=document), self.assertRaisesRegex(QueryDraftError, message):
                validate(SCHEMA, document)

    def test_schema_structure_violations_are_rejected(self):
        cases = {
            "{ contacts { phone } }": "Cannot query field",
            "{ contact { id } }": "is required",
            "{ contacts }": "selection of subfields",
            "{ version { length } }": "has no subfields",
            "{ contacts(page: 1) { id } }": "Unknown argument",
            "{ search { id } }": "SearchResult",
            "query Q($id: Contact) { contact(id: $id) { id } }": "input type",
            "query Q($id: String!) { contact(id: $id) { id } }": "used in position expecting",
            "query Q($id: ID!, $unused: Int) { contact(id: $id) { id } }": "never used",
            "query Q { contact(id: $id) { id } }": "not defined",
            "{ contacts { ...ContactFields } }": "Unknown fragment",
            "{ contacts { id @deprecated } }": "may not be used",
        }
        for document, message in cases.items():
            with self.subTest(document=document), self.assertRaisesRegex(QueryDraftError, message):
                validate(SCHEMA, document)

    def test_named_root_fragments_and_nested_fragments_are_described(self):
        document = ('query Q($show:Boolean=true) { ...Root } '
            'fragment Root on Query { selected:contacts { ...Fields email @include(if:$show) } } '
            'fragment Fields on Contact { id }')
        detail = validate(SCHEMA, document)
        self.assertEqual(detail['root_fields'], ['contacts'])
        self.assertEqual(detail['variables'], {'show':'Boolean'})
        self.assertEqual(validate_variables(SCHEMA, document, {}), {'show':True})
        validate(SCHEMA, '{ __schema { types { name } } }')

    def test_schema_defined_repeatable_directives_and_interface_fragments(self):
        schema = introspection_from_schema(build_schema(
            'directive @review(if:Boolean=true) repeatable on FIELD '
            'interface Named { name:String } type Person implements Named { name:String } '
            'type Query { nodes:[Named] }'))
        document = ('query Q($show:Boolean=true) { nodes @review(if:$show) @review '
            '{ ...Names ... on Person { name } } } fragment Names on Named { name }')
        self.assertEqual(validate(schema, document)['root_fields'], ['nodes'])
        self.assertEqual(validate_variables(schema, document, {}), {'show':True})

    def test_fragment_overlap_cycles_and_undefined_fragments_are_rejected(self):
        for document in ('{ contacts { ... on Project { title } } }',
                         '{ contacts { ...A } } fragment A on Contact { ...A }',
                         '{ contacts { ...Missing } }'):
            with self.subTest(document=document), self.assertRaises(QueryDraftError):
                validate(SCHEMA, document)

    def test_mutations_subscriptions_and_multiple_operations_are_rejected(self):
        for document in ("mutation { changeContact { id } }", "subscription { contacts { id } }",
                         "{ version } { version }", "query A { version } query B { version }",
                         "fragment F on Contact { id }"):
            with self.subTest(document=document), self.assertRaises(QueryDraftError):
                validate(SCHEMA, document)

    def test_large_finite_selections_are_supported(self):
        nested = "{ " + "contacts { " * 1 + "id" + " }" * 1 + " }"
        validate(SCHEMA, nested)
        many = "{ contacts { " + " ".join(f"a{i}: id" for i in range(501)) + " } }"
        self.assertEqual(validate(SCHEMA, many)['field_count'], 502)

    def test_recursive_selection_is_not_bounded_by_the_retired_editor_depth_limit(self):
        schema = copy.deepcopy(SCHEMA)
        contact = next(item for item in schema["__schema"]["types"] if item["name"] == "Contact")
        contact["fields"].append({"name": "friend", "args": [], "type": _named("OBJECT", "Contact")})

        def document(depth):
            return "{ contacts { " + "friend { " * depth + "id" + " }" * depth + " } }"

        validate(schema, document(10))
        validate(schema, document(20))

    def test_runtime_variables_are_validated_and_coerced(self):
        document = "query Q($id: ID!, $first: Int, $filter: ContactFilter) { contact(id: $id) { id } contacts(first: $first, filter: $filter) { id } }"
        self.assertEqual(validate_variables(SCHEMA, document, {"id": "7", "first": 2}), {"id": "7", "first": 2})
        for values, message in (
            ({}, "declared types"),
            ({"id": "7", "first": "2"}, "Int"),
            ({"id": "7", "other": 1}, "undeclared names"),
            ({"id": "7", "filter": {"status": "ACTIVE"}}, "ContactFilter"),
            ([], "JSON object"),
        ):
            with self.subTest(values=values), self.assertRaisesRegex(QueryDraftError, message):
                validate_variables(SCHEMA, document, values)

    def test_default_values_make_variables_optional(self):
        document = "query Q($first: Int = 10, $id: ID! = \"1\") { contacts(first: $first) { id } contact(id: $id) { id } }"
        self.assertEqual(validate(SCHEMA, document)["required_variables"], [])
        self.assertEqual(validate_variables(SCHEMA, document, {}), {"first":10, "id":"1"})


if __name__ == "__main__":
    unittest.main()
