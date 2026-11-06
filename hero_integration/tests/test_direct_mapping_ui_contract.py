"""Read-only UI/documentation contracts; no Frappe database or service imports."""
import csv
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class TestDirectMappingUIContract(unittest.TestCase):
    def test_german_direct_mapping_messages(self):
        path = ROOT / "hero_integration/translations/de.csv"
        data = path.read_bytes()
        self.assertNotIn(b"\r", data)
        rows = list(csv.reader(data.decode().splitlines()))
        self.assertTrue(all(len(row) == 2 for row in rows))
        translations = dict(rows)
        messages = [
            "Price list", "Choose a price list", "Standard Buying", "Standard Selling",
            "Query rule crosses an unsupported native relationship.",
            "Source identity may only bind its technical identity field.",
            "Query target is protected or outside the supported adapter fields.",
            "Record Name must bind the native record title.",
            "An Item Price rule must name its price list (Standard Buying or Standard Selling).",
            "Only Item Price rules name a relation.",
            "Native intake requires a record-name binding.",
            "Each price list may be fed by one rule only.",
            "Unknown HERO resolver: {0}", "Unknown direct operation: {0}",
        ]
        targets = {
            "HERO Item Group": "Item.item_group", "HERO UOM": "Item.stock_uom",
            "HERO Brand": "Item.brand", "HERO EAN Barcode": "Item.barcodes",
            "HERO Price": "Item Price.price_list_rate",
            "HERO Default Sales Price": "Item Price.price_list_rate",
        }
        for resolver, target in targets.items():
            messages += [resolver, f"{resolver} may only feed {target}.",
                         f"{target} requires its coded HERO resolver."]
        for identity in ["id", "product_id"]:
            for suffix in [".", " → HERO Integration Link.source_id."]:
                messages.append(f"The identity of this HERO object is fixed: {identity}{suffix}")
        for operation in ["HeroProducts", "HeroDocuments", "HeroPictures"]:
            messages.append(f"HERO returned an invalid record list for {operation}.")
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(translations.get(message))
                self.assertNotEqual(message, translations[message])

    def test_direct_mapping_guide_is_linked_and_covers_setup_contract(self):
        guide = ROOT / "docs/complete_json_mapping.md"
        content = guide.read_text()
        for term in ["HeroProducts", "HeroDocuments", "HeroPictures", "supply_product_versions",
                     "FileUpload_FileUploads.edges.node", "first", "offset", "target_operation",
                     "read_service", "Source Identity", "Record Name", "Standard Buying",
                     "Standard Selling", "relation", "images", "approve", "built-in",
                     "sales_prices.supply_sales_price.is_default", "list_price"]:
            with self.subTest(term=term):
                self.assertIn(term, content)
