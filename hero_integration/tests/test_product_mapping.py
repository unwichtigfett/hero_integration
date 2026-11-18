"""The product import follows its HeroProducts mapping (own approved or built-in)."""
from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import direct_runtime as runtime
from hero_integration.services import product_migration as pm
from hero_integration.services.mapping_runtime import plan_from_definitions
from hero_integration.tests.test_product_migration import ROW
from hero_integration.tests.saved_reader_fixture import isolate_direct_materialization

BUILTIN = runtime.builtin_read("HeroProducts")


def own_read(drop=(), add=()):
    """An own mapping: the built-in rules minus ``drop`` (target fields) plus ``add``."""
    rules = [spec for spec in BUILTIN.plan.definitions if spec.target_field not in drop and spec.relation not in drop]
    plan = plan_from_definitions("HeroProducts", [*rules, *add], target_operation="HeroProducts",
                                 adapter_options={"version": 2})
    return replace(BUILTIN, plan=plan, origin="SERVICE-1", mapping="MAP-1")


MANUFACTURER_NR = replace(BUILTIN.plan.definitions[1], definition_id="mnr", purpose="Field",
                          source_path="base_data.matchcode", target_field="custom_hero_matchcode")


class _Item(dict):
    __getattr__ = dict.get

    def update(self, values):  # noqa: D401 - dict API used by the importer
        super().update(values)

    def append(self, field, row):
        self.setdefault(field, []).append(row)

    def insert(self, **kwargs):
        self.name = self["item_code"]
        self.inserted = True
        return self

    def save(self, **kwargs):
        self.saved = True


class _Frappe:
    def __init__(self, items=None):
        self.items, self.links, self.masters = dict(items or {}), [], []
        self.db = SimpleNamespace(exists=self.exists, set_value=self.set_value, get_value=lambda *a, **k: None)
        self.utils = SimpleNamespace(now_datetime=lambda: "now")

    def exists(self, doctype, name=None):
        if doctype == "Item":
            return name if name in self.items else None
        if doctype == "Price List":
            return False  # prices are covered by the price tests
        return None

    def set_value(self, doctype, name, values, *args):
        self.links.append(("set", name, values))

    def get_all(self, *args, **kwargs):
        return []

    def has_permission(self, *args, **kwargs): return True

    def get_doc(self, values, name=None, **kwargs):
        if isinstance(values, str):
            return self.items[name]
        if values["doctype"] == "Item":
            item = _Item(values)
            self.items[values["item_code"]] = item
            return item
        if values["doctype"] == "HERO Integration Link":
            document = SimpleNamespace(name='LINK-1', **values)
            def insert(**kwargs):
                self.links.append(('insert', values))
                return document
            document.insert = insert
            return document
        self.masters.append(values)
        return SimpleNamespace(insert=lambda **k: None, name=values.get("supplier_name") or values.get("doctype"))


class TestOwnMapping(unittest.TestCase):
    def test_missing_price_identity_keeps_article_and_recovers_on_later_import(self):
        from hero_integration.services.mapped_collection_relations import MissingCollectionIdentity

        read = replace(BUILTIN, plan=replace(BUILTIN.plan, adapter_options={'version': 2, 'native_relations': [{'kind': 'sales_prices'}]}))
        fake = _Frappe()
        warnings = []
        with patch.object(pm, 'frappe', fake), \
             patch('hero_integration.services.mapped_relations.sync', side_effect=MissingCollectionIdentity('Native collection rows require a stable source identity.')):
            self.assertEqual(pm.upsert(ROW, None, pm._Masters(), 'default', read=read, relation_warnings=warnings), 'added')
        self.assertEqual(len(warnings), 1)
        created = fake.links[-1][1]
        self.assertEqual(created['sync_status'], 'Pending Review')
        self.assertIn('stable source identity', created['conflict_detail'])
        self.assertIn('PV-1001', fake.items)

        link = SimpleNamespace(name='LINK', **created)
        with patch.object(pm, 'frappe', fake), \
             patch('hero_integration.services.mapped_relations.sync', return_value={'conflicts': 0}):
            self.assertEqual(pm.upsert(ROW, link, pm._Masters(), 'default', read=read), 'updated')
        updated = fake.links[-1][2]
        self.assertEqual((updated['sync_status'], updated['conflict_detail']), ('Synchronized', ''))

    def test_article_code_follows_the_selected_source_and_is_not_a_rename(self):
        original = next(spec for spec in BUILTIN.plan.definitions if spec.target_field == "item_code")
        alternative = replace(original, definition_id="code-alternative", source_path="internal_identifier")
        row = {**ROW, "internal_identifier": " OTHER-42 "}
        for read, expected in ((BUILTIN, "PV-1001"),
                               (own_read(drop=("item_code",), add=(alternative,)), "OTHER-42")):
            fake = _Frappe()
            with patch.object(pm, "frappe", fake):
                self.assertEqual(pm.upsert(row, None, pm._Masters(), "default", read=read), "added")
            self.assertIn(expected, fake.items)
            self.assertNotIn("item_code", pm.item_values(row, read)["owned"])
        fake = _Frappe()
        with patch.object(pm, "frappe", fake):
            pm.upsert(row, None, pm._Masters(), "default", read=BUILTIN)
            item = fake.items["PV-1001"]
            link = SimpleNamespace(name="L1", target_name="PV-1001", source_payload_hash="old",
                                   target_payload_hash=pm.owned_digest(item), target_field_hashes="{}")
            pm.upsert(row, link, pm._Masters(), "default",
                      read=own_read(drop=("item_code",), add=(alternative,)))
        self.assertEqual(item["item_code"], "PV-1001")

    def test_unmapped_or_empty_article_code_uses_technical_identity(self):
        self.assertEqual(pm.item_values(ROW, own_read(drop=("item_code",)))["item_code"], ROW["product_id"])
        self.assertEqual(pm.item_values({**ROW, "nr": ""}, own_read())["item_code"], ROW["product_id"])

    def test_an_added_custom_field_is_imported(self):
        row = {**ROW, "base_data": {**ROW["base_data"], "matchcode": " AC-415 "}}
        values = pm.item_values(row, own_read(add=[MANUFACTURER_NR]))
        self.assertEqual(values["custom_hero_matchcode"], "AC-415")
        self.assertIn("custom_hero_matchcode", values["owned"])

    def test_an_unmapped_field_is_left_alone_and_required_fields_get_their_fallback(self):
        values = pm.item_values(ROW, own_read(drop=("brand", "item_group")))
        self.assertNotIn("brand", values)
        self.assertNotIn("item_group", values["owned"])
        fake = _Frappe()
        with patch.object(pm, "frappe", fake):
            self.assertEqual(pm.upsert(ROW, None, pm._Masters(), "default", read=own_read(drop=("brand", "item_group"))),
                             "added")
        item = fake.items["PV-1001"]
        self.assertEqual((item["item_group"], item.get("brand")), ("Ohne Katalog", None))
        link = next(entry[1] for entry in fake.links if entry[0] == "insert")
        self.assertEqual(set(json.loads(link["target_field_hashes"])["fields"]),
                         {"item_name", "description", "stock_uom", "disabled"})
        self.assertTrue(link["mapping_version"].startswith("product-migration-v3:"))

    def test_only_mapped_price_lists_are_filled(self):
        values = pm.item_values(ROW, own_read(drop=("Standard Buying",)))
        self.assertEqual((values["buying"], values["selling"]), (None, 98.0))

    def test_a_changed_mapping_updates_unchanged_articles_once(self):
        fake = _Frappe()
        with patch.object(pm, "frappe", fake):
            pm.upsert(ROW, None, pm._Masters(), "default", read=BUILTIN)
        source = next(entry[1] for entry in fake.links if entry[0] == "insert")["source_payload_hash"]
        item = fake.items["PV-1001"]
        link = SimpleNamespace(name="L1", target_name="PV-1001", source_payload_hash=source,
                               target_payload_hash=pm.owned_digest(item), target_field_hashes=json.dumps(
                                   {"prices": {}, "fields": pm.owned_field_digests(item)}))
        with patch.object(pm, "frappe", fake):
            self.assertEqual(pm.upsert(ROW, link, pm._Masters(), "default", read=BUILTIN), "unchanged")
            row = {**ROW, "base_data": {**ROW["base_data"], "matchcode": "AC-415"}}
            self.assertEqual(pm.upsert(row, link, pm._Masters(), "default", read=own_read(add=[MANUFACTURER_NR])),
                             "updated")
        self.assertEqual(item["custom_hero_matchcode"], "AC-415")

    def test_a_local_edit_of_a_previously_mapped_field_is_still_a_conflict(self):
        item = _Item({"item_code": "PV-1001", "item_name": "MODUL: AXIpremium", "description": "<p>Modul</p>",
                      "item_group": "Eigene Artikel", "stock_uom": "Stk", "brand": "AXITEC", "disabled": 0})
        item.name = "PV-1001"
        baseline = pm.owned_digest(item)  # a link written by the earlier fixed mapping
        item["brand"] = "Local brand"     # edited in ERPNext afterwards
        link = SimpleNamespace(name="L1", target_name="PV-1001", source_payload_hash="old", target_payload_hash=baseline,
                               target_field_hashes=json.dumps({"prices": {}, "fields": {f: "x" for f in pm.OWNED}}))
        fake = _Frappe({"PV-1001": item})
        with patch.object(pm, "frappe", fake):
            self.assertEqual(pm.upsert(ROW, link, pm._Masters(), "default", read=own_read(drop=("brand",))), "conflict")
        self.assertEqual(item["brand"], "Local brand")


class TestSyncFollowsTheMapping(unittest.TestCase):
    def setUp(self):
        isolate_direct_materialization(self)

    def test_a_mapping_change_restarts_the_pass_and_reads_with_its_query(self):
        saved = {pm.STATE_KEY: json.dumps({"source_instance": "default", "mapping": "old", "offset": 500})}
        db = SimpleNamespace(get_default=lambda key:None, get_value=lambda dt,filters,field:saved.get(filters['defkey']), set_default=lambda k, v: saved.__setitem__(k, v),
                             commit=lambda: None, rollback=lambda: None)
        documents = []

        class Client:
            def query(self, document, variables):
                documents.append((document, variables["offset"]))
                return {"supply_product_versions": []}

        read = own_read(add=[MANUFACTURER_NR])
        read = replace(read, document="query Own { supply_product_versions { product_id } }")
        with patch.object(pm, "frappe", SimpleNamespace(db=db, utils=SimpleNamespace(now_datetime=lambda: "now"))), \
                patch.object(pm, "_links", return_value={}):
            state = pm.sync_products(Client(), source_instance="default", read=read)
        self.assertEqual(documents, [("query Own { supply_product_versions { product_id } }", 0)])
        self.assertEqual((state["mapping"], state["mapping_origin"]), (read.mapping_version, "SERVICE-1"))


class TestProgress(unittest.TestCase):
    def setUp(self):
        isolate_direct_materialization(self)

    def test_progress_is_reported_after_every_page(self):
        saved, reports = {}, []
        db = SimpleNamespace(get_default=lambda key:None, get_value=lambda dt,filters,field:saved.get(filters['defkey']), set_default=lambda k, v: saved.__setitem__(k, v),
                             commit=lambda: None, rollback=lambda: None)

        class Client:
            def query(self, document, variables):
                start, size = variables["offset"], variables["first"]
                return {"supply_product_versions": [{"product_id": f"P{i}"} for i in range(start, min(start + size, 1200))]}

        with patch.object(pm, "frappe", SimpleNamespace(db=db, utils=SimpleNamespace(now_datetime=lambda: "now"),
                                                        log_error=lambda **k: None)), \
                patch.object(pm, "_links", return_value={}), patch.object(pm, "upsert", return_value="added"):
            pm.sync_products(Client(), source_instance="default", read=BUILTIN,
                             progress=lambda counts: reports.append(counts.get("added")))
        self.assertEqual(reports, [500, 1000, 1200])  # the next empty page proves the source is exhausted


SUPPLIER_ROW = {**ROW, "supply_operator": {"name": "Krannich Solar"},
                "base_data": {**ROW["base_data"], "category": "Batteriespeicher", "supplier_id": "0151231",
                              "description": "", "manufacturer_nr": "0151230"}}


class TestProductDecisions(unittest.TestCase):
    def test_deleted_bound_item_is_a_conflict_even_when_hero_payload_is_unchanged(self):
        for source_changed in (False, True):
            with self.subTest(source_changed=source_changed):
                fake = _Frappe()
                with patch.object(pm, "frappe", fake):
                    self.assertEqual(pm.upsert(ROW, None, pm._Masters(), "default", read=BUILTIN), "added")
                    link = SimpleNamespace(name="L1", **fake.links[-1][1])
                    del fake.items[link.target_name]
                    original_masters = list(fake.masters)
                    incoming = {**ROW, "base_price": 200} if source_changed else ROW
                    self.assertEqual(pm.upsert(incoming, link, pm._Masters(), "default", read=BUILTIN), "conflict")
                self.assertEqual(fake.items, {})
                self.assertEqual(fake.masters, original_masters)
                self.assertEqual(fake.links[-1][2]["sync_status"], "Conflict")
                self.assertIn("no longer exists", fake.links[-1][2]["conflict_detail"])

    def test_category_is_the_group_and_the_catalog_its_fallback(self):
        self.assertEqual(pm.item_values(SUPPLIER_ROW, own_read())["item_group"], "Batteriespeicher")
        self.assertEqual(pm.item_values(ROW, own_read())["item_group"], "Eigene Artikel")  # no category: catalog

    def test_description_stays_empty_and_the_manufacturer_part_number_is_kept(self):
        values = pm.item_values(SUPPLIER_ROW, own_read())
        self.assertIsNone(values["description"])
        self.assertEqual(values["custom_hero_manufacturer_nr"], "0151230")

    def test_supplier_is_created_once_and_its_row_carries_the_part_number(self):
        fake = _Frappe()
        with patch.object(pm, "frappe", fake):
            self.assertEqual(pm.upsert(SUPPLIER_ROW, None, pm._Masters(), "default", read=BUILTIN), "added")
        item = fake.items["PV-1001"]
        self.assertEqual(item["supplier_items"], [{"supplier": "Krannich Solar", "supplier_part_no": "0151231"}])
        self.assertEqual([m["supplier_name"] for m in fake.masters if m["doctype"] == "Supplier"], ["Krannich Solar"])
        # a second import keeps one row and updates its part number, other suppliers stay
        item["supplier_items"].append({"supplier": "Local", "supplier_part_no": "L-1"})
        changed = {**SUPPLIER_ROW, "base_data": {**SUPPLIER_ROW["base_data"], "supplier_id": "0151299"}}
        with patch.object(pm, "frappe", fake):
            pm._set_supplier(item, pm.item_values(changed, own_read())["supplier"], pm._Masters())
        self.assertEqual(item["supplier_items"], [{"supplier": "Krannich Solar", "supplier_part_no": "0151299"},
                                                  {"supplier": "Local", "supplier_part_no": "L-1"}])

    def test_unbound_item_is_linked_without_changing_local_values(self):
        foreign = _Item({"item_code": "PV-1001", "name": "PV-1001", "item_name": "Local article", 'stock_uom': 'Stk'})
        fake = _Frappe({"PV-1001": foreign})
        with patch.object(pm, "frappe", fake):
            self.assertEqual(pm.upsert(ROW, None, pm._Masters(), "default", read=BUILTIN), "unchanged")
        self.assertEqual(set(fake.items), {"PV-1001"})
        self.assertNotIn("saved", foreign)
        self.assertEqual(foreign['item_name'], 'Local article')
        link = next(entry[1] for entry in fake.links if entry[0] == "insert")
        self.assertEqual((link["sync_status"], link["target_name"]), ("Synchronized", "PV-1001"))
        self.assertTrue(json.loads(link['target_field_hashes'])['adopted_locally'])
        bound = SimpleNamespace(name='LINK-1', **link)
        with patch.object(pm, 'frappe', fake):
            self.assertEqual(pm.upsert(ROW, bound, pm._Masters(), 'default', read=BUILTIN), 'unchanged')
            changed = {**ROW, 'base_data': {**ROW['base_data'], 'name': 'A changed source name'}}
            self.assertEqual(pm.upsert(changed, bound, pm._Masters(), 'default', read=BUILTIN), 'conflict')
        self.assertEqual(foreign['item_name'], 'Local article')

    def test_service_code_collision_uses_stable_separate_article_code(self):
        service = _Item({'item_code': 'PV-1001', 'name': 'PV-1001', 'item_name': 'Local service'})
        fake = _Frappe({'PV-1001': service})
        fake.get_all = lambda *args, **kwargs: [SimpleNamespace(source_type='supply_service',
            source_instance='default', sync_status='Synchronized')]
        with patch.object(pm, 'frappe', fake):
            self.assertEqual(pm.upsert(ROW, None, pm._Masters(), 'default', read=BUILTIN), 'added')
        code = pm._collision_code('PV-1001', 'default', ROW['product_id'])
        self.assertEqual(fake.items[code]['custom_hero_original_article_number'], 'PV-1001')
        self.assertEqual(service['item_name'], 'Local service')

    def test_the_conflict_waits_approve_links_the_item_and_decline_skips(self):
        foreign = _Item({"item_code": "PV-1001", "item_name": "Local article", "stock_uom": "Stk"})
        foreign.name = "PV-1001"
        fake = _Frappe({"PV-1001": foreign})
        waiting = SimpleNamespace(name="L1", target_name="PV-1001", source_payload_hash="", target_payload_hash="",
                                  target_field_hashes="", sync_status="Conflict",
                                  conflict_detail=pm.CODE_TAKEN.format("PV-1001"))
        with patch.object(pm, "frappe", fake):
            self.assertEqual(pm.upsert(ROW, waiting, pm._Masters(), "default", read=BUILTIN), "conflict")
            self.assertNotIn("saved", foreign)
            approved = SimpleNamespace(**{**vars(waiting), "conflict_detail": "Recheck requested: " + waiting.conflict_detail})
            self.assertEqual(pm.upsert(ROW, approved, pm._Masters(), "default", read=BUILTIN), "updated")
            self.assertEqual(foreign["item_name"], "MODUL: AXIpremium")  # the existing Item now follows HERO
            declined = SimpleNamespace(**{**vars(waiting), "sync_status": "Rejected"})
            self.assertEqual(pm.upsert(ROW, declined, pm._Masters(), "default", read=BUILTIN), "skipped_rejected")


if __name__ == "__main__":
    unittest.main()
