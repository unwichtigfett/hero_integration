from __future__ import annotations
from hashlib import sha256
import unittest
from types import SimpleNamespace
from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationRequest, GraphQLResultBinding
from hero_integration.graphql_protocol.operation_kind import GraphQLOperationKind
from hero_integration.services.binding_registry import BindingRegistryService, InMemoryBindingStorage
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.migration_engine import SealedRead
from hero_integration.services.read_contract import ReadContract
from hero_integration.services.sync_engine import *

FP="a"*64; QUERY="query Contacts { contacts { id name } }"
def plan():
 contract=ReadContract("s",1,"m",1,"Projection","contacts","id",("d",),{},10)
 seal=SealedRead(contract,QUERY,FP,sha256(QUERY.encode()).hexdigest())
 request=GraphQLOperationRequest("b",FP,"node",GraphQLOperationKind.QUERY,"contacts","Contacts","contacts",QUERY,(),(GraphQLResultBinding(("id",),("n",),("data","contacts","id"),"ID!"),),"r",None,"{}")
 return MaterializedReadPlan(seal,request,"hero")
class Provider:
 def __init__(self,pages,caps=ChangeCapabilities(True,True,False)): self.pages=pages; self.capabilities=caps; self.calls=[]
 def changes(self,cursor): self.calls.append(cursor); return self.pages[cursor]
class Index:
 def __init__(self,fields): self.value=fields
 def fields(self,site,source): return self.value
class Processor:
 def __init__(self,ok=True): self.ok=ok; self.rows=[]
 def process(self,row,fields): self.rows.append((row,fields)); return self.ok
class Transport:
 def __init__(self,ack): self.ack=ack; self.calls=0
 def execute_once(self,document,variables): self.calls+=1; return self.ack
class TestSync(unittest.TestCase):
 def setUp(self):
  self.ledger=InMemorySyncLedger(); self.config=PollingConfiguration("site",False,60,"hero",1,FP); self.bindings=BindingRegistryService(InMemoryBindingStorage())
  parent=self.bindings.create_or_resolve_object_binding(site_scope="site",source_instance="hero",object_type="Contact",external_object_id="1",target_app="erpnext",target_doctype="Customer",target_name="C1",mapping_definition_id="d",mapping_revision=1)
  self.binding=self.bindings.create_or_resolve_field_binding(parent,source_field="name",target_field="customer_name",mapping_definition_id="d",mapping_revision=1,direction=MappingDirection.HERO_TO_FRAPPE)
  self.field=BoundField("1",self.binding,("name",))
 def engine(self,provider,processor=None,fields=None): return SyncEngine(self.ledger,plan(),provider,Index([self.field] if fields is None else fields),processor or Processor())
 def configured(self,engine): return engine.configure(self.config)
 def test_enabled_polling_obeys_interval_gate_and_uses_same_bound_path(self):
  enabled=PollingConfiguration("site",True,60,"hero",1,FP); provider=Provider({None:ChangePage(({"id":"1","name":"Ada"},),None,True,"w")}); engine=self.engine(provider); state=engine.configure(enabled)
  self.assertFalse(scheduler_due(enabled,59)); self.assertTrue(scheduler_due(enabled,60))
  done=engine.poll(SyncRun("auto","site","admin",state.generation,True)); self.assertEqual(done.status,SyncStatus.SUCCEEDED)

 def test_polling_disabled_but_pending_sync_is_explicit_and_bound_only(self):
  provider=Provider({None:ChangePage(({"id":"1","name":"Ada"},{"id":"2","name":"Unbound"}),None,True,"w1")}); processor=Processor(); engine=self.engine(provider,processor); state=self.configured(engine)
  self.assertEqual(engine.poll(SyncRun("auto","site","admin",state.generation,True)).status,SyncStatus.DISABLED)
  done=engine.pending_sync(SyncRun("manual","site","admin",state.generation,False))
  self.assertEqual((done.status,done.cursor,len(processor.rows)),(SyncStatus.SUCCEEDED,"w1",1)); self.assertEqual(processor.rows[0][0]["id"],"1")
 def test_cursor_is_preserved_after_partial_failure_or_missing_watermark(self):
  provider=Provider({None:ChangePage(({"id":"1","name":"Ada"},),None,True,"w1")}); engine=self.engine(provider,Processor(False)); state=self.configured(engine)
  partial=engine.pending_sync(SyncRun("m","site","admin",state.generation,False)); self.assertEqual((partial.status,partial.cursor),(SyncStatus.PARTIAL,None))
  self.ledger=InMemorySyncLedger(); provider=Provider({None:ChangePage(({"id":"1","name":"Ada"},),None,True,None)}); engine=self.engine(provider); state=self.configured(engine)
  incomplete=engine.pending_sync(SyncRun("m2","site","admin",state.generation,False)); self.assertEqual((incomplete.status,incomplete.cursor),(SyncStatus.INCOMPLETE,None))
 def test_second_page_failure_does_not_persist_intermediate_cursor(self):
  class FailsSecond(Processor):
   def process(self,row,fields):
    self.rows.append((row,fields)); return len(self.rows)==1
  provider=Provider({None:ChangePage(({"id":"1","name":"Ada"},),"c1",False),
                     "c1":ChangePage(({"id":"1","name":"changed"},),None,True,"w1")})
  processor=FailsSecond(); engine=self.engine(provider,processor); state=self.configured(engine)
  result=engine.pending_sync(SyncRun("partial-two","site","admin",state.generation,False))
  self.assertEqual((result.status,result.cursor,result.coverage),(SyncStatus.PARTIAL,None,()))
  self.assertEqual(provider.calls,[None,"c1"])
  self.assertEqual(self.ledger.state("site").cursor,None)
 def test_second_page_exception_does_not_persist_intermediate_cursor(self):
  provider=Provider({None:ChangePage(({"id":"1","name":"Ada"},),"c1",False)})
  engine=self.engine(provider); state=self.configured(engine)
  result=engine.pending_sync(SyncRun("failed-two","site","admin",state.generation,False))
  self.assertEqual((result.status,result.cursor),(SyncStatus.FAILED,None))
 def test_cursor_cycle_preserves_preexisting_committed_watermark(self):
  from dataclasses import replace
  provider=Provider({"w0":ChangePage((),"c1",False), "c1":ChangePage((),"c2",False),
                     "c2":ChangePage((),"c1",False)})
  engine=self.engine(provider); state=self.configured(engine)
  state=self.ledger.save_state(replace(state,cursor="w0",generation=state.generation+1),state.generation)
  result=engine.pending_sync(SyncRun("cycle","site","admin",state.generation,False))
  self.assertEqual((result.status,result.cursor),(SyncStatus.PARTIAL,"w0"))
  self.assertEqual(provider.calls,["w0","c1","c2"])
 def test_bound_writer_gate_keeps_cursor_when_local_baseline_contract_missing(self):
  provider=Provider({None:ChangePage(({"id":"1","name":"Remote"},),None,True,"w1")})
  class Writer:
   def __init__(self): self.calls=0
   def update_existing_bound(self,*args): self.calls+=1; return True
  writer=Writer(); processor=FrappeBoundProcessor(1,("d",),document_adapter=writer)
  engine=self.engine(provider,processor); state=self.configured(engine)
  result=engine.pending_sync(SyncRun("blocked-bound","site","admin",state.generation,False))
  self.assertEqual((result.status,result.cursor,writer.calls),(SyncStatus.PARTIAL,None,0))

 def test_capability_limits_and_revision_change_require_reconciliation(self):
  provider=Provider({None:ChangePage((),None,True,"w")},ChangeCapabilities(False,False,True)); engine=self.engine(provider); state=self.configured(engine)
  self.assertEqual(engine.pending_sync(SyncRun("m","site","admin",state.generation,False)).status,SyncStatus.RECONCILIATION_REQUIRED)
  self.ledger=InMemorySyncLedger(); engine=self.engine(provider); changed=PollingConfiguration("site",False,60,"hero",2,FP); state=engine.configure(self.config); self.assertEqual(engine.configure(changed).status,SyncStatus.RECONCILIATION_REQUIRED)
 def test_duplicate_pages_concurrency_and_stale_state_fail_closed(self):
  provider=Provider({None:ChangePage(({"id":"1","name":"Ada"},),"c",False),"c":ChangePage(({"id":"1","name":"Ada"},),"c",False)}); engine=self.engine(provider); state=self.configured(engine)
  self.assertEqual(engine.pending_sync(SyncRun("m","site","admin",state.generation,False)).status,SyncStatus.PARTIAL)
  self.ledger.active["site"]="other"
  with self.assertRaises(SyncConflict): engine.pending_sync(SyncRun("m2","site","admin",state.generation,False))
  self.ledger.active.clear()
  with self.assertRaises(SyncConflict): engine.pending_sync(SyncRun("m3","site","admin",999,False))
 def test_b7_b2_mismatch_is_rejected_without_second_compiler(self):
  with self.assertRaises(SyncError): MaterializedReadPlan(plan().sealed,object(),"hero")
 def test_sync_hero_eligibility_acknowledgment_and_reenable_after_change(self):
  intent=OutboundIntent(self.binding,1,FP,"b"*64,True,"mutation X { x { id } }",{})
  self.assertEqual(sync_hero_eligibility(intent),Eligibility.LOCAL_CHANGED)
  approval=OutboundApproval(True,True,True,True,True,1); ack=OutboundAck(True,"evidence","a"*64,"b"*64)
  updated=execute_sync_hero(intent,approval,Transport(ack),self.bindings); done=OutboundIntent(updated,1,FP,"b"*64,True,"m",{})
  self.assertEqual(sync_hero_eligibility(done),Eligibility.NOT_ELIGIBLE)
  changed=OutboundIntent(updated,1,FP,"new-local",True,"m",{}); self.assertEqual(sync_hero_eligibility(changed),Eligibility.LOCAL_CHANGED)
 def test_frappe_scope_rejects_unauthorized_actor_without_legacy_has_role_api(self):
  fake=SimpleNamespace(local=SimpleNamespace(site="site"),session=SimpleNamespace(user="Guest"),get_roles=lambda: ())
  with self.assertRaises(PermissionError): FrappeSyncLedger(fake)._scope("site")
  fake.session.user="manager"; fake.get_roles=lambda: ("System Manager",)
  self.assertIs(FrappeSyncLedger(fake)._scope("site"),fake)
 def test_outbound_rejects_unauthorized_unverified_or_implicit_write(self):
  intent=OutboundIntent(self.binding,1,FP,"local",True,"mutation X { x { id } }",{})
  with self.assertRaises(PermissionError): execute_sync_hero(intent,OutboundApproval(False,True,True,True,True,1),Transport(OutboundAck(True,"e","a"*64,"b"*64)),self.bindings)
  with self.assertRaises(SyncConflict): execute_sync_hero(intent,OutboundApproval(True,True,True,True,True,1),Transport(OutboundAck(False,"e","a"*64,"b"*64)),self.bindings)
  with self.assertRaises(SyncError): execute_sync_hero(OutboundIntent(self.binding,1,FP,"local",False,"m",{}),OutboundApproval(True,True,True,True,True,1),Transport(OutboundAck(True,"e","a"*64,"b"*64)),self.bindings)
 def test_each_missing_outbound_approval_blocks_transport(self):
  from dataclasses import replace
  intent=OutboundIntent(self.binding,1,FP,"local",True,"mutation X { x { id } }",{})
  approval=OutboundApproval(True,True,True,True,True,1)
  for field in ("actor_authorized","mutation_approved","preview_confirmed","audit_reserved","no_retry"):
   transport=Transport(OutboundAck(True,"e","a"*64,"b"*64))
   with self.subTest(missing=field), self.assertRaises(PermissionError):
    execute_sync_hero(intent,replace(approval,**{field:False}),transport,self.bindings)
   self.assertEqual(transport.calls,0)
 def test_unproven_change_provider_never_queries_or_reads_bound_index(self):
  provider=Provider({},ChangeCapabilities(False,False,False))
  engine=self.engine(provider); state=self.configured(engine)
  class RejectIndex:
   def fields(self,*args): raise AssertionError("unproven provider must not enumerate bindings")
  engine.index=RejectIndex()
  result=engine.pending_sync(SyncRun("unproven","site","admin",state.generation,False))
  self.assertEqual((result.status,result.cursor),(SyncStatus.INCOMPLETE,None))
  self.assertEqual(provider.calls,[])
  self.assertIsNone(self.ledger.active_run("site"))

 def test_failed_processor_releases_claim_for_next_run_without_advancing_cursor(self):
  provider=Provider({None:ChangePage(({"id":"1","name":"Ada"},),None,True,"w1")})
  engine=self.engine(provider,Processor(False)); state=self.configured(engine)
  result=engine.pending_sync(SyncRun("failed","site","admin",state.generation,False))
  self.assertEqual((result.status,result.cursor),(SyncStatus.PARTIAL,None))
  self.assertIsNone(self.ledger.active_run("site"))
  current=self.ledger.state("site")
  retry=engine.pending_sync(SyncRun("retry","site","admin",current.generation,False))
  self.assertEqual((retry.status,retry.cursor),(SyncStatus.PARTIAL,None))
  self.assertIsNone(self.ledger.active_run("site"))

 def test_provider_conflict_after_first_page_keeps_state_and_releases_claim(self):
  class ConflictingProvider(Provider):
   def changes(self,cursor):
    if cursor == "page-2":
     self.calls.append(cursor)
     raise SyncConflict("synthetic provider conflict")
    return super().changes(cursor)
  provider=ConflictingProvider({None:ChangePage(({"id":"1","name":"first"},),"page-2",False)})
  processor=Processor(); engine=self.engine(provider,processor); state=self.configured(engine)
  with self.assertRaisesRegex(SyncConflict,"synthetic provider conflict"):
   engine.pending_sync(SyncRun("conflict","site","admin",state.generation,False))
  self.assertEqual(provider.calls,[None,"page-2"])
  self.assertEqual(len(processor.rows),1)
  self.assertEqual(self.ledger.state("site"),state)
  self.assertIsNone(self.ledger.active_run("site"))

 def test_partial_batch_replays_from_committed_cursor_before_advancing(self):
  class FailsOnceOnSecond(Processor):
   def process(self,row,fields):
    self.rows.append((row,fields))
    return len(self.rows)!=2
  provider=Provider({None:ChangePage(({"id":"1","name":"first"},),"page-2",False),
                     "page-2":ChangePage(({"id":"1","name":"second"},),None,True,"w1")})
  processor=FailsOnceOnSecond(); engine=self.engine(provider,processor); state=self.configured(engine)
  partial=engine.pending_sync(SyncRun("first","site","admin",state.generation,False))
  self.assertEqual((partial.status,partial.cursor),(SyncStatus.PARTIAL,None))
  retry=engine.pending_sync(SyncRun("retry","site","admin",partial.generation,False))
  self.assertEqual((retry.status,retry.cursor),(SyncStatus.SUCCEEDED,"w1"))
  self.assertEqual(provider.calls,[None,"page-2",None,"page-2"])
  self.assertEqual([row[0]["name"] for row in processor.rows],["first","second","first","second"])
  self.assertIsNone(self.ledger.active_run("site"))

if __name__=="__main__": unittest.main()
