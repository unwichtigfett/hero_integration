import unittest
from types import SimpleNamespace

from hero_integration.services import simple_mapping as sm


class TestSimpleMapping(unittest.TestCase):
    def test_query_paths_lists_leaf_fields(self):
        document = """query HeroCustomers($first: Int!) { contacts(first: $first) {
            id nr title address { id street country { code } } } }"""
        self.assertEqual(sm.query_paths(document), ["id", "nr", "title", "address.id", "address.street", "address.country.code"])
        self.assertEqual(sm.query_paths("not graphql"), [])

    def test_transformation_follows_the_field_types(self):
        field = lambda fieldtype: SimpleNamespace(fieldtype=fieldtype)
        self.assertEqual(sm.transformation_for("string|null", field("Link")), "Existing Record")
        self.assertEqual(sm.transformation_for("string|null", field("Text Editor")), "Plain Text To HTML")
        self.assertEqual(sm.transformation_for("string|null", field("Data")), "Trim")
        self.assertEqual(sm.transformation_for("int|null", field("Data")), "Positive Int To Text")
        self.assertEqual(sm.transformation_for("boolean|null", field("Small Text")), "Scalar Text")
        self.assertEqual(sm.transformation_for("float|null", field("Currency")), "Identity")
        self.assertEqual(sm.transformation_for("boolean|null", field("Check")), "Identity")

    def test_only_customers_and_projects_are_editable(self):
        self.assertEqual(set(sm.EDITABLE), {"HeroCustomers", "HeroProjectMatches"})
