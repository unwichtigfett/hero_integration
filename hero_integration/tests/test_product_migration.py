"""HERO article → ERPNext Item mapping and paging."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import product_migration as pm
from hero_integration.services import direct_runtime

BUILTIN = direct_runtime.builtin_read("HeroProducts")

ROW = {
    "product_id": "FSRmFpZSMAA", "nr": "PV-1001", "base_price": 145.55, "list_price": 199, "is_deleted": False,
    "base_data": {"name": "MODUL: AXIpremium", "description": "<p>Modul</p>", "ean": " 4260 4630 2005 8 ",
                  "manufacturer": "AXITEC", "unit_type": "Stk", "supply_catalog": {"name": "Eigene Artikel"}},
    "sales_prices": [{"net_price_per_unit": 94.25, "supply_sales_price": {"is_default": False}},
                     {"net_price_per_unit": 98, "supply_sales_price": {"is_default": True}}],
}


class TestMapping(unittest.TestCase):
    def test_article_fields(self):
        values = pm.item_values(ROW, BUILTIN)
        self.assertEqual((values["item_code"], values["item_name"], values["item_group"], values["stock_uom"]),
                         ("PV-1001", "MODUL: AXIpremium", "Eigene Artikel", "Stk"))
        self.assertEqual((values["brand"], values["barcode"], values["disabled"]), ("AXITEC", "4260463020058", 0))
        self.assertEqual((values["buying"], values["selling"]), (145.55, 98.0))

    def test_fallbacks(self):
        values = pm.item_values({"product_id": "X1", "nr": "", "is_deleted": True, "base_price": 0, "list_price": 12.5,
                                 "base_data": {"name": "", "unit_type": ""}}, BUILTIN)
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
        rows = pm.fetch_page(_Client(1000, reject_above=250), 0, read=BUILTIN)
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
        with patch.object(pm, "frappe", fake), patch.object(pm, "_links", return_value={}) as links, \
                patch.object(pm, "upsert", side_effect=lambda row, link, m, instance, **kw: seen.append(row["product_id"]) or "added"):
            state = pm.sync_products(client, source_instance="source-a", read=BUILTIN)
        links.assert_called_once_with("source-a")
        self.assertEqual(len(seen), 1200)
        self.assertEqual([offset for _, offset in client.calls], [0, 500, 1000])
        self.assertTrue(state["done"])
        self.assertEqual(state["counts"]["added"], 1200)


class _Row(dict):
    __getattr__ = dict.get


class _PriceDB:
    def __init__(self, prices):
        self.prices = {name: dict(row) for name, row in prices.items()}
        self.writes = []

    def exists(self, doctype, name):
        if doctype in ("Price List", "Item"):
            return True
        return name in self.prices

    def get_value(self, doctype, name, fields, as_dict=False):
        return _Row({field: self.prices[name].get(field) for field in fields})

    def set_value(self, doctype, name, values, *args):
        self.writes.append((doctype, name, values))
        if doctype == "Item Price":
            self.prices[name].update(values)


def _price_frappe(db):
    def get_all(doctype, filters, fields, limit_page_length=0):
        return [_Row({"name": name, **row}) for name, row in db.prices.items()
                if all(row.get(key) == value for key, value in filters.items())]

    class _Doc(dict):
        def insert(self, **kwargs):
            self.name = f"PRICE-NEW{len(db.prices) + 1}"
            db.prices[self.name] = {key: value for key, value in self.items() if key != "doctype"}
            return self

    return SimpleNamespace(db=db, get_all=get_all, get_doc=lambda values: _Doc(values),
                           utils=SimpleNamespace(now_datetime=lambda: "now"))


GENERAL = {"item_code": "PV-1001", "price_list": pm.SELLING, "uom": "Stk", "price_list_rate": 98}
CUSTOMER_BOX = {"item_code": "PV-1001", "price_list": pm.SELLING, "uom": "Box", "customer": "C-1", "price_list_rate": 1200}


class TestManagedPrices(unittest.TestCase):
    def plan_and_apply(self, prices, rate=100, managed=None):
        db = _PriceDB(prices)
        with patch.object(pm, "frappe", _price_frappe(db)):
            plan = pm.plan_price("PV-1001", pm.SELLING, rate, "Stk", managed)
            baseline = pm.apply_price("PV-1001", pm.SELLING, plan)
        return db, baseline

    def test_customer_specific_unit_price_is_never_overwritten(self):
        db, baseline = self.plan_and_apply({"BOX": CUSTOMER_BOX})
        self.assertEqual(db.prices["BOX"], CUSTOMER_BOX)
        self.assertEqual(db.prices[baseline["name"]]["price_list_rate"], 100)
        self.assertEqual(db.prices[baseline["name"]]["uom"], "Stk")

    def test_only_the_general_price_of_the_unit_is_adopted(self):
        db, baseline = self.plan_and_apply({"BOX": CUSTOMER_BOX, "GEN": GENERAL,
                                            "CUST": {**GENERAL, "customer": "C-1", "price_list_rate": 80}})
        self.assertEqual(baseline, {"name": "GEN", "rate": 100, "uom": "Stk"})
        self.assertEqual((db.prices["CUST"]["price_list_rate"], db.prices["BOX"]["price_list_rate"]), (80, 1200))

    def test_ambiguous_general_prices_block(self):
        db = _PriceDB({"A": GENERAL, "B": dict(GENERAL)})
        with patch.object(pm, "frappe", _price_frappe(db)), self.assertRaises(pm.PriceConflict):
            pm.plan_price("PV-1001", pm.SELLING, 100, "Stk", None)
        self.assertEqual(db.writes, [])

    def test_locally_changed_managed_price_is_a_conflict(self):
        db = _PriceDB({"GEN": {**GENERAL, "price_list_rate": 120}})
        with patch.object(pm, "frappe", _price_frappe(db)), self.assertRaises(pm.PriceConflict):
            pm.plan_price("PV-1001", pm.SELLING, 100, "Stk", {"name": "GEN", "rate": 98, "uom": "Stk"})
        _, baseline = self.plan_and_apply({"GEN": GENERAL}, managed={"name": "GEN", "rate": 98, "uom": "Stk"})
        self.assertEqual(baseline, {"name": "GEN", "rate": 100, "uom": "Stk"})

    def test_managed_price_later_bound_to_a_customer_is_a_conflict(self):
        db = _PriceDB({"P1": {**GENERAL, "customer": "CUSTOMER-1"}})  # only the customer changed locally
        with patch.object(pm, "frappe", _price_frappe(db)), self.assertRaises(pm.PriceConflict):
            pm.plan_price("PV-1001", pm.SELLING, 100, "Stk", {"name": "P1", "rate": 98, "uom": "Stk"})
        self.assertEqual((db.prices["P1"]["price_list_rate"], db.writes), (98, []))

    def test_managed_price_with_a_new_customer_binding_blocks_the_whole_article(self):
        db = _PriceDB({"P1": {**GENERAL, "customer": "CUSTOMER-1"}})
        fake = _price_frappe(db)
        item = SimpleNamespace(name="PV-1001", stock_uom="Stk", save=lambda **k: self.fail("Item must not be saved"))
        fake.get_doc = lambda doctype, name: item
        link = SimpleNamespace(name="L1", target_name="PV-1001", source_payload_hash="old", target_payload_hash="",
                               target_field_hashes=json.dumps({"prices": {pm.SELLING: {"name": "P1", "rate": 98, "uom": "Stk"}}}))
        with patch.object(pm, "frappe", fake), patch.object(pm, "owned_digest", return_value="x"):
            result = pm.upsert({**ROW, "base_price": None, "sales_prices": [], "list_price": 100}, link,
                               pm._Masters(), "source-a", read=BUILTIN)
        self.assertEqual(result, "conflict")
        self.assertEqual(db.prices["P1"]["price_list_rate"], 98)
        self.assertEqual([write[0] for write in db.writes], ["HERO Integration Link"])

    def test_price_conflict_marks_the_link_before_any_item_write(self):
        db = _PriceDB({"A": GENERAL, "B": dict(GENERAL)})
        fake = _price_frappe(db)
        item = SimpleNamespace(name="PV-1001", stock_uom="Stk", save=lambda **k: self.fail("Item must not be saved"))
        fake.get_doc = lambda doctype, name: item
        link = SimpleNamespace(name="L1", target_name="PV-1001", source_payload_hash="old", target_payload_hash="",
                               target_field_hashes="")
        with patch.object(pm, "frappe", fake), patch.object(pm, "owned_digest", return_value="x"):
            result = pm.upsert({**ROW, "base_price": None}, link, pm._Masters(), "source-a", read=BUILTIN)
        self.assertEqual(result, "conflict")
        self.assertEqual(db.writes[0][0], "HERO Integration Link")
        self.assertEqual(db.writes[0][2]["sync_status"], "Conflict")

    def test_links_and_offset_belong_to_one_source(self):
        calls = []
        fake = SimpleNamespace(get_all=lambda doctype, filters, fields, limit_page_length: calls.append(filters) or [])
        with patch.object(pm, "frappe", fake):
            pm._links("source-b")
        self.assertEqual(calls[0]["source_instance"], "source-b")
        saved = {pm.STATE_KEY: json.dumps({"source_instance": "source-b", "offset": 500})}
        db = SimpleNamespace(get_default=saved.get, set_default=lambda k, v: saved.__setitem__(k, v),
                             commit=lambda: None, rollback=lambda: None)
        client = _Client(10)
        with patch.object(pm, "frappe", SimpleNamespace(db=db, utils=SimpleNamespace(now_datetime=lambda: "now"))), \
                patch.object(pm, "_links", return_value={}), patch.object(pm, "upsert", return_value="added"):
            pm.sync_products(client, source_instance="source-a", read=BUILTIN)
        self.assertEqual(client.calls[0], (pm.PAGE_SIZE, 0))


if __name__ == "__main__":
    unittest.main()
