from __future__ import annotations
from hashlib import sha256
import unittest
from hero_integration.services.binding_registry import BindingRegistryService, InMemoryBindingStorage
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.migration_engine import FieldPlan, FrappeDocumentAdapter, FrappeLedger, InMemoryLedger, MigrationConflict, MigrationEngine, MigrationRequest, MigrationRun, Progress, ProgressState, RunState, SealedRead, SourcePage
from hero_integration.services.read_contract import ReadContract

QUERY = "query Contacts { contacts { id name } }"
FINGERPRINT = "a" * 64
class Reader:
 def __init__(self,pages=(),fail=False): self.pages=dict(pages); self.fail=fail; self.calls=[]
 def read_page(self,request,sealed,offset):
  self.calls.append(offset)
  if self.fail: raise RuntimeError("reader failure")
  return self.pages[offset]
class Writer:
 def __init__(self,fail=False): self.fail=fail; self.docs={}
 def resolve_or_write(self,site,app,doctype,identity,values):
  if self.fail: raise RuntimeError("writer failure")
  self.docs.setdefault((site,identity),dict(values)); return "DOC-"+identity
def sealed():
 contract=ReadContract("service",3,"mapping",1,"Projection","contacts","id",("contact-name",),{"first":"$page_size","offset":"$offset"},2)
 return SealedRead(contract,QUERY,FINGERPRINT,sha256(QUERY.encode()).hexdigest())
class TestEngine(unittest.TestCase):
 def setUp(self):
  self.ledger=InMemoryLedger(); self.reader=Reader(); self.writer=Writer(); self.engine=MigrationEngine(self.reader,self.writer,BindingRegistryService(InMemoryBindingStorage()),self.ledger,sealed())
  self.request=MigrationRequest("run-1","site.test","admin","hero-a","hero.query.Contacts",FINGERPRINT,1,"erpnext","Customer","Contact",True)
  self.plan=(FieldPlan("contact-name",1,("name",),"customer_name",MappingDirection.HERO_TO_FRAPPE),)
 def authorized(self): self.engine.prepare(self.request); return self.engine.authorize("run-1",0)
 def test_pagination_checkpoint_and_recovery(self):
  run=self.authorized(); self.reader.pages={0:SourcePage(({"id":"1","name":"Ada"},{"id":"2","name":"Bob"}),2),2:SourcePage(({"id":"3","name":"Cleo"},),None)}
  done=self.engine.process("run-1",self.plan,run.generation)
  self.assertEqual((done.state,done.checkpoint,done.completed),(RunState.COMPLETED,2,3)); self.assertEqual(self.reader.calls,[0,2]); self.assertEqual(len(self.writer.docs),3); self.assertEqual(len(self.ledger.progress),3)
 def test_new_run_resolves_acknowledged_field_binding_without_reset(self):
  from dataclasses import replace
  run=self.authorized(); self.reader.pages={0:SourcePage(({"id":"1","name":"Ada"},),None)}
  first=self.engine.process("run-1",self.plan,run.generation)
  self.assertEqual(first.state,RunState.COMPLETED)
  binding=self.engine.bindings.get_field_bindings_for_source("site.test","hero-a")[0]
  acknowledged=self.engine.bindings.record_verified_acknowledgment(binding,expected_revision=1,
    mapping_definition_id="contact-name",mapping_revision=1,source_fingerprint="a"*64,
    target_fingerprint="b"*64,evidence_id="verified",version=1)
  request=replace(self.request,run_id="run-2")
  self.engine.prepare(request); second=self.engine.authorize("run-2",0)
  done=self.engine.process("run-2",self.plan,second.generation)
  self.assertEqual(done.state,RunState.COMPLETED)
  self.assertEqual(self.engine.bindings.get_field_bindings_for_source("site.test","hero-a"),(acknowledged,))
 def test_target_failure_is_recoverable_without_checkpoint_advance(self):
  run=self.authorized(); self.reader.pages={0:SourcePage(({"id":"1","name":"Ada"},),None)}; self.writer.fail=True
  partial=self.engine.process("run-1",self.plan,run.generation)
  self.assertEqual((partial.state,partial.checkpoint,partial.record_index,partial.completed),(RunState.PARTIAL,0,0,0)); self.assertEqual(self.ledger.progress[("run-1","1")].state,ProgressState.FAILED)
  self.writer.fail=False; done=self.engine.process("run-1",self.plan,partial.generation); self.assertEqual((done.state,done.completed),(RunState.COMPLETED,1))
 def test_binding_failure_after_write_returns_partial_without_writer_rollback(self):
  class FailingBindings:
   def create_or_resolve_object_binding(self, **kwargs): raise RuntimeError("binding failure")
  self.engine.bindings=FailingBindings()
  run=self.authorized(); self.reader.pages={0:SourcePage(({"id":"1","name":"Ada"},),None)}
  partial=self.engine.process("run-1",self.plan,run.generation)
  self.assertEqual((partial.state,partial.record_index,partial.completed),(RunState.PARTIAL,0,0))
  self.assertEqual(self.ledger.progress[("run-1","1")].state,ProgressState.FAILED)
  self.assertEqual(self.writer.docs, {("site.test","1"): {"customer_name":"Ada"}})
 def test_partial_page_resume_requires_same_page_and_continues_after_completed_record(self):
  run=self.authorized(); self.reader.pages={0:SourcePage(({"id":"1","name":"Ada"},{"id":"2","name":"Bob"}),None)}
  original=self.writer.resolve_or_write
  def blocked(site,app,doctype,identity,values):
   if identity=="2": raise RuntimeError("failure")
   return original(site,app,doctype,identity,values)
  self.writer.resolve_or_write=blocked
  partial=self.engine.process("run-1",self.plan,run.generation)
  self.assertEqual((partial.state,partial.record_index,partial.completed),(RunState.PARTIAL,1,1))
  self.writer.resolve_or_write=original
  done=self.engine.process("run-1",self.plan,partial.generation)
  self.assertEqual((done.state,done.completed),(RunState.COMPLETED,2))
  self.assertEqual(self.reader.calls,[0,0]); self.assertEqual(len(self.writer.docs),2)
 def test_partial_page_resume_rejects_changed_order_before_further_writes(self):
  run=self.authorized(); original=self.writer.resolve_or_write
  self.reader.pages={0:SourcePage(({"id":"1","name":"Ada"},{"id":"2","name":"Bob"}),None)}
  def blocked(site,app,doctype,identity,values):
   if identity=="2": raise RuntimeError("failure")
   return original(site,app,doctype,identity,values)
  self.writer.resolve_or_write=blocked
  partial=self.engine.process("run-1",self.plan,run.generation)
  self.reader.pages[0]=SourcePage(({"id":"2","name":"Bob"},{"id":"1","name":"Ada"}),None)
  self.writer.resolve_or_write=original
  failed=self.engine.process("run-1",self.plan,partial.generation)
  self.assertEqual(failed.state,RunState.FAILED); self.assertEqual(set(self.writer.docs),{("site.test","1")})
 def test_repeated_page_is_rejected_at_next_offset(self):
  run=self.authorized(); records=({"id":"1","name":"Ada"},)
  self.reader.pages={0:SourcePage(records,1),1:SourcePage(records,None)}
  failed=self.engine.process("run-1",self.plan,run.generation)
  self.assertEqual((failed.state,failed.checkpoint,failed.completed),(RunState.FAILED,1,1))
  self.assertEqual(len(self.writer.docs),1)
 def test_partial_resume_requires_saved_page_fingerprint(self):
  from dataclasses import replace
  run=self.authorized(); self.reader.pages={0:SourcePage(({"id":"1","name":"Ada"},{"id":"2","name":"Bob"}),None)}
  partial=self.ledger.save_run(replace(run,state=RunState.PARTIAL,record_index=1,generation=run.generation+1),run.generation)
  failed=self.engine.process("run-1",self.plan,partial.generation)
  self.assertEqual(failed.state,RunState.FAILED); self.assertEqual(self.writer.docs,{})
 def test_stale_plan_and_invalid_pages_fail_closed(self):
  run=self.authorized()
  with self.assertRaises(MigrationConflict): self.engine.process("run-1",(FieldPlan("x",2,("name",),"x"),),run.generation)
  self.reader.pages={0:SourcePage(({"id":"1","name":"Ada"},),0)}; failed=self.engine.process("run-1",self.plan,run.generation); self.assertEqual(failed.state,RunState.FAILED)
 def test_missing_identity_and_snapshot_drift_fail_closed(self):
  run=self.authorized(); self.reader.pages={0:SourcePage(({"name":"Ada"},),None)}; failed=self.engine.process("run-1",self.plan,run.generation); self.assertEqual(failed.state,RunState.FAILED)
  bad=MigrationRequest("run-2","site.test","admin","hero-a","hero.query.Contacts","b"*64,1,"erpnext","Customer","Contact",True)
  with self.assertRaises(MigrationConflict): self.engine.prepare(bad)
class _Doc:
 def __init__(self,frappe,payload): self._frappe=frappe; self.__dict__.update(payload)
 def insert(self):
  if not getattr(self,"name",None): self.name=getattr(self,"migration_run_id",None) or getattr(self,"identity_key",None) or getattr(self,"custom_hero_key",None)
  self._frappe.docs.setdefault(self.doctype,{})[self.name]=self; return self
 def save(self): self._frappe.docs.setdefault(self.doctype,{})[self.name]=self; return self
class _FakeFrappe:
 def __init__(self):
  self.docs={}; self.local=type("Local",(),{"site":"site.test"})(); self.session=type("Session",(),{"user":"Administrator"})(); self.db=type("DB",(),{"sql":lambda *_: []})()
 def has_role(self,role): return role=="System Manager"
 def has_permission(self,*args,**kwargs): return True
 def get_all(self,doctype,filters=None,fields=None):
  rows=[]
  for doc in self.docs.get(doctype,{}).values():
   if all(getattr(doc,key,None)==value for key,value in (filters or {}).items()): rows.append(type("Row",(),{"name":doc.name})())
  return rows
 def get_doc(self,value,name=None):
  if isinstance(value,dict): return _Doc(self,value)
  return self.docs[value][name]
class TestFrappeAdapters(unittest.TestCase):
 def test_ledger_round_trip_recovery_and_document_upsert(self):
  frappe=_FakeFrappe(); ledger=FrappeLedger(frappe)
  request=MigrationRequest("run-ledger","site.test","admin","hero-a","hero.query.Contacts",FINGERPRINT,1,"erpnext","Customer","Contact",True)
  run=ledger.save_run(MigrationRun(request),None); self.assertEqual(ledger.run("run-ledger"),run)
  failed=ledger.save_progress(Progress("run-ledger","1",ProgressState.FAILED,detail="target-write-failed")); completed=ledger.save_progress(Progress("run-ledger","1",ProgressState.COMPLETED,("field",)))
  self.assertEqual((failed.state,completed.state),(ProgressState.FAILED,ProgressState.COMPLETED))
  writer=FrappeDocumentAdapter("custom_hero_key",frappe)
  self.assertEqual(writer.resolve_or_write("site.test","erpnext","Customer","1",{"customer_name":"Ada"}),"1")
  self.assertEqual(writer.resolve_or_write("site.test","erpnext","Customer","1",{"customer_name":"Ada Lovelace"}),"1")
  self.assertEqual(frappe.docs["Customer"]["1"].customer_name,"Ada Lovelace")

if __name__ == "__main__": unittest.main()
