"""Products, Documents and Pictures as configurable query-adapter v2 operations."""
from __future__ import annotations

from dataclasses import replace
import unittest

from hero_integration.services import direct_operations as direct
from hero_integration.services import query_mapping as qm
from hero_integration.services.direct_mapping import MappingSpec
from hero_integration.services.mapping_runtime import MappingPlanError, plan_from_definitions


def specs(operation):
    return [MappingSpec(**rule, definition_id=f"{operation}-{index}")
            for index, rule in enumerate(direct.DEFAULT_RULES[operation])]


def plan(rules, operation="HeroProducts"):
    return plan_from_definitions(operation, rules, target_operation=operation, adapter_options={"version": 2})


class TestRegistry(unittest.TestCase):
    def test_the_three_operations_are_v2_roots(self):
        self.assertEqual(qm.ROOTS["HeroProducts"], ("supply_product_versions", "Item"))
        self.assertEqual(qm.ROOTS["HeroDocuments"], ("FileUpload_FileUploads", "File"))
        self.assertEqual(qm.ROOTS["HeroPictures"], ("FileUpload_FileUploads", "File"))
        self.assertEqual(direct.records_path("HeroDocuments"), "FileUpload_FileUploads.edges.node")
        self.assertTrue(direct.TRANSFORMS <= qm.TRANSFORMS)

    def test_the_built_in_mappings_are_valid_v2_plans(self):
        for operation in direct.OPERATIONS:
            with self.subTest(operation=operation):
                self.assertEqual(plan(specs(operation), operation).target_doctype, direct.owner(operation))

    def test_direct_operations_need_the_v2_adapter(self):
        with self.assertRaisesRegex(MappingPlanError, "versioned Query adapter"):
            plan_from_definitions("HeroProducts", specs("HeroProducts"), target_operation="HeroProducts")


class TestRuleValidation(unittest.TestCase):
    def check(self, spec):
        qm.validate_v2_spec(spec, None, {"version": 2})

    def rule(self, **changes):
        base = MappingSpec("HeroProducts", "base_data.name", "Item", "item_name", "string|null", "Field", "Trim",
                           definition_id="x")
        return replace(base, **changes)

    def test_free_fields_and_custom_fields_are_allowed(self):
        self.check(self.rule())
        self.check(self.rule(source_path="base_data.manufacturer_nr", target_field="custom_hero_manufacturer_nr"))
        self.check(self.rule(source_path="filename", operation="HeroDocuments", target_doctype="File",
                             target_field="custom_hero_category"))

    def test_protected_fields_other_doctypes_and_fixed_identity_are_rejected(self):
        for spec in (self.rule(target_field="item_code"),                        # naming stays coded
                     self.rule(target_doctype="Customer", target_field="customer_name"),
                     self.rule(target_doctype="File", target_field="file_name"),  # File is not a product target
                     self.rule(target_doctype="HERO Integration Link", target_field="source_id",
                               purpose="Source Identity", source_path="id", transformation="Positive Int To Text"),
                     self.rule(purpose="Source Identity")):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.check(spec)

    def test_coded_resolvers_feed_only_their_field(self):
        self.check(self.rule(target_field="item_group", transformation="HERO Item Group"))
        for spec in (self.rule(target_field="item_name", transformation="HERO Item Group"),
                     self.rule(target_field="item_group", transformation="Trim"),     # link needs its resolver
                     self.rule(target_field="barcodes", transformation="Trim")):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.check(spec)

    def test_price_rules_name_one_price_list(self):
        price = self.rule(source_path="list_price", source_type="float|null", target_doctype="Item Price",
                          target_field="price_list_rate", transformation="HERO Price", relation="Standard Selling")
        self.check(price)
        for spec in (replace(price, relation=""), replace(price, relation="Customer Price")):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.check(spec)
        with self.assertRaisesRegex(ValueError, "HERO Price requires"):  # a price needs a numeric source
            qm._validate_output_compatibility(replace(price, source_type="string|null"), {}, "Currency")

    def test_plan_rejects_a_second_rule_for_the_same_price_list_and_a_moved_identity(self):
        rules = specs("HeroProducts")
        duplicate = replace(rules[-1], definition_id="dup", source_path="list_price", source_type="float|null",
                            transformation="HERO Price")
        with self.assertRaises(ValueError):
            qm.validate_plan(plan(rules + [duplicate]))
        moved = [replace(rules[0], source_path="id", source_type="int", transformation="Positive Int To Text")] + rules[1:]
        with self.assertRaises(ValueError):
            qm.validate_plan(plan(moved))

    def test_documents_and_pictures_select_what_the_import_needs(self):
        required = direct.required_paths(plan(specs("HeroDocuments"), "HeroDocuments"))
        self.assertTrue({"downloadUrl", "category", "uuid", "size", "modified"} <= required)
        product = direct.required_paths(plan(specs("HeroProducts")))
        self.assertTrue({"product_id", "is_deleted", "list_price", "sales_prices.net_price_per_unit"} <= product)


class _Meta:
    """Frappe metadata double: every field read-only, like HERO Integration Link.source_id and File.file_name."""

    writes = []

    def ensure_write(self, doctype):
        self.writes.append(doctype)

    def field(self, doctype, fieldname):
        from types import SimpleNamespace
        return SimpleNamespace(fieldtype="Data", read_only=1, hidden=0, permlevel=0, is_virtual=0, fetch_from=None)


class TestMetadataChecks(unittest.TestCase):
    def test_technical_identity_and_listed_direct_fields_pass_the_read_only_check(self):
        meta = _Meta()
        identity = MappingSpec("HeroTasks", "id", "HERO Integration Link", "source_id", "int", "Source Identity",
                               "Positive Int To Text", definition_id="t")
        qm.validate_v2_spec(identity, meta, {"version": 2})
        self.assertNotIn("HERO Integration Link", meta.writes)  # no form permission is asked for the identity
        name = MappingSpec("HeroDocuments", "filename", "File", "file_name", "string|null", "Record Name", "Trim",
                           definition_id="f")
        qm.validate_v2_spec(name, meta, {"version": 2})

    def test_other_read_only_fields_stay_protected(self):
        task = MappingSpec("HeroTasks", "title", "Task", "subject", "string|null", "Record Name", "Trim",
                           definition_id="s")
        with self.assertRaisesRegex(ValueError, "read-only"):
            qm.validate_v2_spec(task, _Meta(), {"version": 2})


class TestResolvers(unittest.TestCase):
    def test_masters_and_barcode(self):
        self.assertEqual(direct.resolve("HERO Item Group", " Eigene Artikel "), "Eigene Artikel")
        self.assertEqual(direct.resolve("HERO Item Group", None), "Ohne Katalog")
        self.assertEqual(direct.resolve("HERO UOM", ""), "Stk")
        self.assertIsNone(direct.resolve("HERO Brand", "  "))
        self.assertEqual(direct.resolve("HERO EAN Barcode", " 4260 4630 2005 8 "), "4260463020058")

    def test_prices(self):
        self.assertEqual(direct.resolve("HERO Price", 145.554), 145.55)
        for value in (0, -3, None, True, "x", float("nan")):
            self.assertIsNone(direct.resolve("HERO Price", value), value)
        sales = [{"net_price_per_unit": 94.25, "supply_sales_price": {"is_default": False}},
                 {"net_price_per_unit": 98, "supply_sales_price": {"is_default": True}}]
        self.assertEqual(direct.resolve("HERO Default Sales Price", sales, {"list_price": 199}), 98.0)
        self.assertEqual(direct.resolve("HERO Default Sales Price", [], {"list_price": 12.5}), 12.5)

    def test_built_in_rows_are_marked_read_only(self):
        rows = direct.default_rows()
        self.assertEqual({row["operation"] for row in rows}, set(direct.OPERATIONS))
        self.assertTrue(all(row["builtin"] == 1 and row["status"] == "Built-in" for row in rows))


if __name__ == "__main__":
    unittest.main()
