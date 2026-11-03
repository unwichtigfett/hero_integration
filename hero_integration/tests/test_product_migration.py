"""HERO article → ERPNext Item mapping and paging."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import product_migration as pm

ROW = {
    "product_id": "FSRmFpZSMAA", "nr": "PV-1001", "base_price": 145.55, "list_price": 199, "is_deleted": False,
    "base_data": {"name": "MODUL: AXIpremium", "description": "<p>Modul</p>", "ean": " 4260 4630 2005 8 ",
                  "manufacturer": "AXITEC", "unit_type": "Stk", "supply_catalog": {"name": "Eigene Artikel"}},
    "sales_prices": [{"net_price_per_unit": 94.25, "supply_sales_price": {"is_default": False}},
                     {"net_price_per_unit": 98, "supply_sales_price": {"is_default": True}}],
}


class TestMapping(unittest.TestCase):
    def test_article_fields(self):
        values = pm.item_values(ROW)
        self.assertEqual((values["item_code"], values["item_name"], values["item_group"], values["stock_uom"]),
                         ("PV-1001", "MODUL: AXIpremium", "Eigene Artikel", "Stk"))
        self.assertEqual((values["brand"], values["barcode"], values["disabled"]), ("AXITEC", "4260463020058", 0))
        self.assertEqual((values["buying"], values["selling"]), (145.55, 98.0))

    def test_fallbacks(self):
        values = pm.item_values({"product_id": "X1", "nr": "", "is_deleted": True, "base_price": 0, "list_price": 12.5,
                                 "base_data": {"name": "", "unit_type": ""}})
        self.assertEqual((values["item_code"], values["item_name"], values["stock_uom"]), ("X1", "X1", "Stk"))
        self.assertEqual((values["item_group"], values["brand"], values["disabled"]), ("Ohne Katalog", None, 1))
        self.assertEqual((values["buying"], values["selling"]), (None, 12.5))

    def test_ean_type_only_for_valid_check_digit(self):
        self.assertEqual(pm.barcode_type("4006381333931"), "EAN")
        self.assertEqual(pm.barcode_type("4006381333932"), "")
        self.assertEqual(pm.barcode_type("96385074"), "EAN")
        self.assertEqual(pm.barcode_type("ABC123"), "")


class _Client:
    def __init__(self, total, reject_above=500):
        self.total, self.reject_above, self.calls = total, reject_above, []

    def query(self, document, variables):
        self.calls.append((variables["first"], variables["offset"]))
        if variables["first"] > self.reject_above:
            raise RuntimeError("too large")
        start, end = variables["offset"], min(variables["offset"] + variables["first"], self.total)
        return {"supply_product_versions": [{"product_id": f"P{i}", "nr": str(i)} for i in range(start, end)]}


class TestPaging(unittest.TestCase):
    def test_rejected_page_is_read_in_halves(self):
        rows = pm.fetch_page(_Client(1000, reject_above=250), 0)
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[0]["product_id"], "P0")
        self.assertEqual(rows[-1]["product_id"], "P499")

    def test_sync_walks_all_pages_and_finishes(self):
        saved, seen = {}, []
        fake = SimpleNamespace(
            db=SimpleNamespace(get_default=lambda k: saved.get(k), set_default=lambda k, v: saved.__setitem__(k, v),
                               commit=lambda: None, rollback=lambda: None),
            utils=SimpleNamespace(now_datetime=lambda: "now"), log_error=lambda **k: None)
        client = _Client(1200)
        with patch.object(pm, "frappe", fake), patch.object(pm, "_links", return_value={}), \
                patch.object(pm, "upsert", side_effect=lambda row, link, m: seen.append(row["product_id"]) or "added"):
            state = pm.sync_products(client)
        self.assertEqual(len(seen), 1200)
        self.assertEqual([offset for _, offset in client.calls], [0, 500, 1000])
        self.assertTrue(state["done"])
        self.assertEqual(state["counts"]["added"], 1200)


if __name__ == "__main__":
    unittest.main()
