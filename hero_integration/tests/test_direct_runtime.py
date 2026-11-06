"""Runtime basis of the configurable direct migrations: reads, paging, values and query seals."""
from __future__ import annotations

from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from graphql import build_schema, introspection_from_schema

from hero_integration.services import direct_runtime as rt
from hero_integration.services import query_mapping as qm
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.tests.saved_reader_fixture import product_paging_read


class TestRequiredApprovedRead(unittest.TestCase):
    def test_missing_or_unapproved_mapping_never_uses_template(self):
        for operation in rt.direct.OPERATIONS:
            with self.subTest(operation=operation), patch.object(rt, "approved_read", return_value=None), \
                    patch.object(rt, "builtin_read") as template:
                with self.assertRaisesRegex(ValueError, "create and approve"):
                    rt.resolve_read(operation, "source-a")
                template.assert_not_called()

    def test_sealed_mapping_errors_are_not_hidden_by_template(self):
        with patch.object(rt, "approved_read", side_effect=ValueError("stale seal")), \
                patch.object(rt, "builtin_read") as template:
            with self.assertRaisesRegex(ValueError, "stale seal"):
                rt.resolve_read("HeroProducts", "source-a")
            template.assert_not_called()

    def test_approved_read_is_used_unchanged(self):
        read = object()
        with patch.object(rt, "approved_read", return_value=read) as approved:
            self.assertIs(rt.resolve_read("HeroPictures", "source-a"), read)
        approved.assert_called_once_with("HeroPictures", "source-a")


class _Client:
    def __init__(self, pages, reject_above=None):
        self.pages, self.reject_above, self.calls = pages, reject_above, []

    def query(self, document, variables):
        self.calls.append(dict(variables))
        if self.reject_above and variables["first"] > self.reject_above:
            raise RuntimeError("Internal server error")
        return self.pages(variables["offset"], variables["first"])


class TestBuiltinReads(unittest.TestCase):
    def test_selection_nests_paths(self):
        self.assertEqual(rt.selection({"a.b", "a.c.d", "e"}), "a { b c { d } } e")

    def test_products_page_with_offset_and_values(self):
        read = rt.builtin_read("HeroProducts")
        self.assertIn('supply_product_versions(first: $first, offset: $offset, orderBy: "product_id")', read.document)
        row = {"product_id": "P1", "is_deleted": False, "base_price": 145.554, "list_price": 199,
               "base_data": {"name": " Modul ", "description": "<p>x</p><script>y</script>", "ean": "4006 3813 3393 1",
                             "manufacturer": "", "unit_type": None, "supply_catalog": {"name": "Eigene Artikel"}},
               "sales_prices": [{"net_price_per_unit": 98, "supply_sales_price": {"is_default": True}}]}
        values = rt.record_values(read.plan, row, "Item")
        self.assertEqual((values["item_name"], values["item_group"], values["stock_uom"], values["brand"]),
                         ("Modul", "Eigene Artikel", "Stk", None))
        self.assertEqual((values["barcodes"], values["disabled"]), ("4006381333931", False))
        self.assertNotIn("script", values["description"])
        self.assertEqual(rt.price_values(read.plan, row), {"Standard Buying": 145.55, "Standard Selling": 98.0})
        self.assertEqual(rt.identity(read, row), "P1")

    def test_files_read_the_connection_and_split_by_category(self):
        documents, pictures = rt.builtin_read("HeroDocuments"), rt.builtin_read("HeroPictures")
        self.assertIn("FileUpload_FileUploads(first: $first, offset: $offset, sortings: [ID_ASC])", documents.document)
        self.assertIn('filters: { category: { equals: "images" } }', pictures.document)
        self.assertNotIn("filters", documents.document)
        data = {"FileUpload_FileUploads": {"edges": [{"node": {"id": 1, "category": "offer", "filename": " a.pdf "}},
                                                     {"node": {"id": 2, "category": "images", "filename": "b.jpg"}}]}}
        rows = rt.records(documents, data)
        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertEqual([row["id"] for row in rows if rt.belongs("HeroDocuments", row)], [1])
        self.assertEqual([row["id"] for row in rows if rt.belongs("HeroPictures", row)], [2])
        self.assertEqual(rt.record_values(documents.plan, rows[0], "File"), {"file_name": "a.pdf"})
        self.assertEqual(rt.identity(documents, rows[0]), "1")

    def test_a_rejected_page_is_read_in_halves_and_invalid_lists_fail(self):
        read = product_paging_read()
        client = _Client(lambda offset, first: {"supply_product_versions": [{"product_id": f"P{i}"}
                                                                            for i in range(offset, offset + first)]},
                         reject_above=250)
        rows = rt.fetch_page(client, read, 0)
        self.assertEqual((len(rows), rows[0]["product_id"], rows[-1]["product_id"]), (500, "P0", "P499"))
        with self.assertRaises(ValueError):
            rt.records(read, {"supply_product_versions": [1, 2]})

    def test_only_an_explicit_empty_collection_finishes_a_direct_page(self):
        for operation, path in (("HeroProducts", "supply_product_versions"),
                                ("HeroServices", "supply_services"),
                                ("HeroEmployees", "company.partners")):
            read = rt.DirectRead(operation, None, "query", path)
            valid = {"company": {"partners": []}} if "." in path else {path: []}
            self.assertEqual(rt.records(read, valid), [])
            invalid = [None, {}, [], "", 0, False, {"data": valid}, {"errors": []}]
            invalid += [{"company": value} for value in (None, {}, [], "bad")]
            invalid += [{path: value} for value in (None, {}, "bad", [None])]
            if "." in path:
                invalid += [{"company": {"partners": value}} for value in (None, {}, "bad", [None])]
            for response in invalid:
                with self.subTest(operation=operation, response=response), self.assertRaisesRegex(ValueError, "invalid record list"):
                    rt.records(read, response)

    def test_connection_requires_explicit_edges_and_object_nodes(self):
        read = rt.DirectRead("HeroDocuments", None, "query", "files.edges.node")
        self.assertEqual(rt.records(read, {"files": {"edges": []}}), [])
        self.assertEqual(rt.records(read, {"files": {"edges": [{"node": {"id": 1}}]}}), [{"id": 1}])
        responses = [None, {}, {"files": None}, {"files": {}}, {"files": []},
                     {"files": {"edges": None}}, {"files": {"edges": {}}}]
        responses += [{"files": {"edges": [edge]}} for edge in (None, {}, [], {"node": None}, {"node": []}, {"node": "bad"})]
        for response in responses:
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, "invalid record list"):
                rt.records(read, response)

    def test_invalid_page_does_not_retry_as_smaller_or_complete_empty(self):
        read = product_paging_read()
        client = _Client(lambda offset, first: {"supply_product_versions": None})
        with self.assertRaisesRegex(ValueError, "invalid record list"):
            rt.fetch_page(client, read, 0)
        self.assertEqual(len(client.calls), 1)

    def test_read_without_snapshot_never_bypasses_materialization(self):
        client = _Client(lambda offset, first: {'supply_product_versions':[]})
        with self.assertRaisesRegex(ValueError,'schema-bound read'):
            rt.fetch_page(client,rt.builtin_read('HeroProducts'),0)
        self.assertEqual(client.calls, [])

    def test_approved_variable_names_are_respected(self):
        read = rt.DirectRead("HeroProducts", None, "q", "supply_product_versions", {"n": "$page_size", "o": "$offset",
                                                                                     "orderBy": "product_id"}, 100)
        self.assertEqual(rt.page_variables(read, 300), {"n": 100, "o": 300, "orderBy": "product_id"})


SCHEMA = build_schema("""
  scalar DateTime
  type Query { FileUpload_FileUploads(first: Int, offset: Int, sortings: [Sort!]): Connection! }
  enum Sort { ID_ASC }
  type Connection { edges: [Edge!] }
  type Edge { node: Node! }
  type Node { id: Int! uuid: String! filename: String! category: String downloadUrl: String! size: Int
              modified: DateTime! created: DateTime! section: String }
""")


class TestNestedQuerySeal(unittest.TestCase):
    def seal(self, document, records_path="FileUpload_FileUploads.edges.node"):
        raw = introspection_from_schema(SCHEMA)
        snapshot = NS(name="snap", snapshot_json=raw, schema_fingerprint=analyze_snapshot(raw).fingerprint,
                      source_instance="default", analysis_status="Validated")
        service = NS(name="svc", query_document=document, operation_name="FileUpload_FileUploads")
        config = {"target_operation": "HeroDocuments", "records_path": records_path, "identity_path": "id",
                  "variables": {"first": "$page_size", "offset": "$offset"}, "page_size": 200}
        plan = rt.builtin_plan("HeroDocuments")
        from dataclasses import replace
        section = replace(plan.definitions[0], definition_id="section", source_path="section",
                          source_type="string|null", target_doctype="File", target_field="custom_hero_section",
                          purpose="Field", transformation="Trim")
        from hero_integration.services.mapping_runtime import plan_from_definitions
        plan = plan_from_definitions("HeroDocuments", [*plan.definitions, section], target_operation="HeroDocuments",
                                     adapter_options={"version": 2})
        return qm.seal_query(service, snapshot, config, plan)

    QUERY = ("query Files($first: Int!, $offset: Int!) { FileUpload_FileUploads(first: $first, offset: $offset, "
             "sortings: [ID_ASC]) { edges { node { id uuid filename category downloadUrl size modified created %s } } } }")

    def test_an_own_documents_mapping_seals_on_the_record_level(self):
        self.assertEqual(self.seal(self.QUERY % "section")["version"], 2)

    def test_missing_selections_or_a_flat_records_path_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "omits"):
            self.seal(self.QUERY % "")   # the configured section field is not read
        with self.assertRaisesRegex(ValueError, "root"):
            self.seal(self.QUERY % "section", records_path="FileUpload_FileUploads")


if __name__ == "__main__":
    unittest.main()
