"""Synthetic discovery and point-reader coverage through the common sync loop."""
import unittest
from dataclasses import replace
from types import SimpleNamespace

from hero_integration.services.sync_engine import (
    BoundField, ChangeCapabilities, ChangePage, InMemorySyncLedger, PollingConfiguration,
    SyncEngine, SyncRun, SyncStatus,
)
from hero_integration.services.sync_provider_capabilities import (
    FastChangeProvider, ProviderCapability, ProviderCapabilityError, ProviderContract,
)
from hero_integration.tests.test_sync_engine import FP, Index, plan
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.binding_registry import FieldBinding, ObjectBinding


class Processor:
    def __init__(self):
        self.events = set()
        self.writes = []

    def accepted(self, identity, key, fields):
        return (identity,key) in self.events

    def process_event(self, record, fields, key, version):
        self.writes.append(dict(record))
        self.events.add((str(record["id"]),key))
        return True


def existing(identity):
    parent = ObjectBinding.create(site_scope="site",source_instance="hero",object_type="Contact",
        external_object_id=identity,target_app="erpnext",target_doctype="Customer",target_name=identity,
        mapping_definition_id="d",mapping_revision=1)
    binding = FieldBinding.create(parent,source_field="name",target_field="customer_name",
        mapping_definition_id="d",mapping_revision=1,direction=MappingDirection.HERO_TO_FRAPPE)
    return BoundField(identity,binding,("name",))


class TestFastSyncRuntime(unittest.TestCase):
    def setUp(self):
        self.ledger = InMemorySyncLedger()
        self.processor = Processor()
        self.reads = []
        self.detail = {"id":"1","version":"v2","name":"Neo"}

    def fast(self):
        return FastChangeProvider(
            ProviderContract(ProviderCapability.COMPLETE_DELTA_CURSOR),
            lambda _cursor: ChangePage(({"id":"1","version":"v2"},{"id":"2","version":"v1"}),None,True,"w1"),
            lambda identity: self.reads.append(identity) or self.detail,
            lambda row: set(row)=={"id","version","name"},
            identity_of=lambda row:row["id"], version_of=lambda row:row["version"])

    def engine(self,provider,admit=lambda _kind,_identity:True):
        engine=SyncEngine(self.ledger,plan(),provider,Index([existing("1"),existing("2")]),
                          self.processor,reserve_identity=admit,object_type="Contact")
        engine.configure(PollingConfiguration("site",False,60,"hero",1,FP))
        return engine

    def run_request(self,name):
        return SyncRun(name,"site","Administrator",self.ledger.state("site").generation,False)

    def test_skip_accepted_versions_target_only_changed_then_replay(self):
        self.processor.events.add(("2",("version","v1")))
        engine=self.engine(self.fast())
        done=engine.pending_sync(self.run_request("first"))
        self.assertEqual((done.status,done.cursor),(SyncStatus.SUCCEEDED,"w1"))
        engine.pending_sync(self.run_request("second"))
        self.assertEqual(self.reads,["1"])
        self.assertEqual(self.processor.writes,[self.detail])

    def test_detail_must_match_schema_id_and_announced_revision(self):
        engine=self.engine(self.fast())
        for key,value in (("id","other"),("version","newer"),("unknown","shape")):
            before=self.detail.copy()
            self.detail[key]=value
            with self.subTest(key=key),self.assertRaises(ProviderCapabilityError):
                engine.pending_sync(self.run_request(key))
            self.detail=before
        self.assertEqual(self.processor.writes,[])
        self.assertIsNone(self.ledger.state("site").cursor)

    def test_no_budget_no_detail_and_no_watermark(self):
        engine=self.engine(self.fast(),admit=lambda *_:False)
        result=engine.pending_sync(self.run_request("cap"))
        self.assertEqual((result.status,result.cursor),(SyncStatus.BUDGET_REACHED,None))
        self.assertEqual(self.reads,[])

    def test_point_comparison_only_existing_ids_and_without_global_cursor_claim(self):
        def lookup(identity):
            self.reads.append(identity)
            return {"id":identity,"name":"test"}
        provider=SimpleNamespace(capabilities=ChangeCapabilities(False,False,True),read_bound=lookup)
        engine=self.engine(provider)
        state=self.ledger.state("site")
        self.ledger.save_state(replace(state,cursor="previous"),state.generation)
        result=engine.compare_bound(self.run_request("manual"))
        self.assertEqual((result.status,result.cursor),(SyncStatus.BOUNDED_COMPLETE,"previous"))
        self.assertEqual(self.reads,["1","2"])

    def test_revoked_authority_prevents_point_read(self):
        from hero_integration.services.sync_engine import SyncConflict
        engine = self.engine(self.fast())
        calls = []
        def check():
            calls.append(1)
            if len(calls) == 2:
                raise SyncConflict("source approval changed")
        engine.revalidate = check
        with self.assertRaises(SyncConflict):
            engine.pending_sync(self.run_request("revoked"))
        self.assertEqual(len(calls),2)
        self.assertEqual(self.reads,[])
        self.assertEqual(self.processor.writes,[])

    def test_schedule_change_keeps_watermark_but_different_read_contract_invalidates_it(self):
        engine=self.engine(self.fast())
        current=self.ledger.state("site")
        current=self.ledger.save_state(replace(current,cursor="watermark"),current.generation)
        scheduled=engine.configure(replace(current.configuration,enabled=True))
        self.assertEqual(scheduled.cursor,"watermark")
        changed=engine.configure(replace(scheduled.configuration,contract_fingerprint="a"*64))
        self.assertIsNone(changed.cursor)

    def test_duplicate_or_incomplete_indicators_cannot_promote_offset(self):
        with self.assertRaises(ProviderCapabilityError):
            FastChangeProvider(ProviderContract(ProviderCapability.UNSUPPORTED),lambda _:None,
                               lambda _:None,lambda _:True,identity_of=lambda _:1,version_of=lambda _:"v")
        for indicator in ({"id":"1"},{"id":"1","version":None},{"id":"1","name":"unexpected","version":1}):
            with self.subTest(indicator=indicator),self.assertRaises(ProviderCapabilityError):
                self.fast().version(indicator)
