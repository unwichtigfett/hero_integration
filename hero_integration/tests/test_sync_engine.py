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
  intent=OutboundIntent(self.binding,1,FP,"local",True,"mutation X { x { id } }",{})
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
if __name__=="__main__": unittest.main()
