"""B8 controlled synchronization orchestration.

This module has no scheduler registration, HERO client construction, or direct
Frappe mutation.  It consumes B7-sealed reads and B2 materialized requests via
injected adapters; production invocation remains separately authorized.
"""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
import json
from typing import Protocol

from hero_integration.graphql_protocol.operation_kind import GraphQLOperationKind
from hero_integration.services.binding_registry import BindingRegistryService, FieldBinding, BindingState
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.migration_engine import SealedRead


class SyncError(ValueError): pass
class SyncConflict(SyncError): pass
class SyncStatus(StrEnum): IDLE="IDLE"; RUNNING="RUNNING"; SUCCEEDED="SUCCEEDED"; PARTIAL="PARTIAL"; INCOMPLETE="INCOMPLETE"; RECONCILIATION_REQUIRED="RECONCILIATION_REQUIRED"; FAILED="FAILED"; DISABLED="DISABLED"
class Eligibility(StrEnum): NOT_ELIGIBLE="NOT_ELIGIBLE"; UNBOUND_ELIGIBLE="UNBOUND_ELIGIBLE"; LOCAL_CHANGED="LOCAL_CHANGED"; MAPPING_CHANGED="MAPPING_CHANGED"; RECONCILIATION_REQUIRED="RECONCILIATION_REQUIRED"


def _text(value, label):
    if not isinstance(value, str) or not value.strip(): raise SyncError(f"{label} is required.")
    return value
def _positive(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1: raise SyncError(f"{label} must be positive.")
    return value
def _fingerprint(value):
    value=_text(value,"schema fingerprint")
    if len(value)!=64 or any(c not in "0123456789abcdef" for c in value): raise SyncError("Schema fingerprint is invalid.")
    return value

@dataclass(frozen=True, slots=True)
class PollingConfiguration:
    site_scope:str; enabled:bool; interval_seconds:int; source_instance:str; mapping_revision:int; schema_fingerprint:str
    def __post_init__(self):
        _text(self.site_scope,"site scope"); _text(self.source_instance,"source instance"); _positive(self.mapping_revision,"mapping revision"); _fingerprint(self.schema_fingerprint)
        if not isinstance(self.enabled,bool) or not isinstance(self.interval_seconds,int) or isinstance(self.interval_seconds,bool) or not 60<=self.interval_seconds<=86400: raise SyncError("Polling interval must be between 60 and 86400 seconds.")

@dataclass(frozen=True, slots=True)
class MaterializedReadPlan:
    """The only B8 read entry: B7 seal plus canonical B2 materialized request."""
    sealed:SealedRead; request:object; source_instance:str
    def __post_init__(self):
        if not isinstance(self.sealed,SealedRead): raise SyncError("B7 sealed read is required.")
        _text(self.source_instance,"source instance")
        request=self.request
        if getattr(request,"operation_kind",None) is not GraphQLOperationKind.QUERY: raise SyncError("B8 accepts only a B2 query request.")
        # B7 seals the persisted introspection snapshot fingerprint, while B2
        # correctly carries the canonical graphql_protocol IR fingerprint.
        # They are validated at their own boundaries by the runtime
        # reconstructor and must not be conflated here.
        if not isinstance(getattr(request,"schema_fingerprint",None),str) or not getattr(request,"schema_fingerprint",None): raise SyncError("B2 request has no materializer fingerprint.")
        if getattr(request,"document",None)!=self.sealed.query_document: raise SyncError("B2 document must exactly preserve the approved saved query.")
        if not getattr(request,"result_bindings",None): raise SyncError("B2 request has no sealed result bindings.")

@dataclass(frozen=True, slots=True)
class BoundField:
    object_id:str; binding:FieldBinding; source_path:tuple[str,...]
    def __post_init__(self):
        _text(self.object_id,"bound object identity")
        if not isinstance(self.binding,FieldBinding) or not self.source_path or any(not isinstance(x,str) or not x for x in self.source_path): raise SyncError("Bound field is invalid.")

@dataclass(frozen=True, slots=True)
class ChangeCapabilities:
    cursor:bool; complete_change_discovery:bool; bounded_comparison:bool
@dataclass(frozen=True, slots=True)
class ChangePage:
    records:tuple[Mapping[str,object],...]; next_cursor:str|None; complete:bool; watermark:str|None=None
    def __post_init__(self):
        if not all(isinstance(x,Mapping) for x in self.records) or any(x is not None and not isinstance(x,str) for x in (self.next_cursor,self.watermark)): raise SyncError("Provider change page is invalid.")
@dataclass(frozen=True, slots=True)
class SyncState:
    configuration:PollingConfiguration; cursor:str|None=None; coverage:tuple[str,...]=(); status:SyncStatus=SyncStatus.IDLE; generation:int=0; diagnostic:str=""
    def __post_init__(self):
        if self.cursor is not None: _text(self.cursor,"cursor")
        if not isinstance(self.status,SyncStatus) or not isinstance(self.generation,int) or self.generation<0 or len(set(self.coverage))!=len(self.coverage): raise SyncError("Sync state is invalid.")
@dataclass(frozen=True, slots=True)
class SyncRun:
    run_id:str; site_scope:str; actor:str; expected_generation:int; automatic:bool
    def __post_init__(self): _text(self.run_id,"run id"); _text(self.site_scope,"site scope"); _text(self.actor,"actor");

class ChangeProvider(Protocol):
    capabilities:ChangeCapabilities
    def changes(self, cursor:str|None)->ChangePage: ...
class BoundIndex(Protocol):
    def fields(self, site_scope:str, source_instance:str)->Sequence[BoundField]: ...
class BoundProcessor(Protocol):
    def process(self, record:Mapping[str,object], fields:tuple[BoundField,...])->bool: ...
class SyncLedger(Protocol):
    def state(self, site_scope:str)->SyncState|None: ...
    def save_state(self, state:SyncState, expected:int|None)->SyncState: ...
    def active_run(self, site_scope:str)->str|None: ...
    def claim(self, run:SyncRun)->None: ...
    def release(self, run:SyncRun)->None: ...

class InMemorySyncLedger:
    def __init__(self): self.states={}; self.active={}
    def state(self, site): return self.states.get(site)
    def save_state(self,state,expected):
        old=self.state(state.configuration.site_scope)
        if (old is None and expected is not None) or (old is not None and old.generation!=expected): raise SyncConflict("Stale sync state.")
        self.states[state.configuration.site_scope]=state; return state
    def active_run(self,site): return self.active.get(site)
    def claim(self,run):
        if self.active_run(run.site_scope): raise SyncConflict("A synchronization run is already active.")
        self.active[run.site_scope]=run.run_id
    def release(self,run):
        if self.active.get(run.site_scope)==run.run_id: del self.active[run.site_scope]

class FrappeSyncLedger:
    """Persistent, site-scoped B8 state.  It never commits or creates schema."""
    STATE="HERO Sync State Ledger"; RUN="HERO Sync Run Ledger"
    def __init__(self,frappe_module=None): self._frappe_module=frappe_module
    def _frappe(self):
        if self._frappe_module is not None: return self._frappe_module
        import frappe
        return frappe
    def _scope(self,site):
        frappe=self._frappe()
        if getattr(getattr(frappe,"local",None),"site",None)!=site: raise SyncError("Sync ledger cannot cross site scope.")
        user=getattr(getattr(frappe,"session",None),"user",None)
        roles=getattr(frappe,"get_roles",lambda: ())()
        if user!="Administrator" and "System Manager" not in roles: raise PermissionError("System Manager or Administrator authority is required.")
        return frappe
    def state(self,site):
        frappe=self._frappe(); rows=frappe.get_all(self.STATE,filters={"site_scope":site},fields=["name"])
        if len(rows)>1: raise SyncConflict("Sync state scope is not unique.")
        return None if not rows else _state_from_doc(frappe.get_doc(self.STATE,rows[0].name))
    def save_state(self,state,expected):
        frappe=self._scope(state.configuration.site_scope); frappe.has_permission(self.STATE,"write",throw=True); old=self.state(state.configuration.site_scope)
        if old is None:
            if expected is not None: raise SyncConflict("Stale sync state.")
            frappe.has_permission(self.STATE,"create",throw=True); frappe.get_doc(_state_payload(state)).insert(); return state
        if old.generation!=expected: raise SyncConflict("Stale sync state.")
        frappe.db.sql("select name from `tabHERO Sync State Ledger` where name=%s for update",(state.configuration.site_scope,))
        doc=frappe.get_doc(self.STATE,state.configuration.site_scope)
        if _state_from_doc(doc).generation!=expected: raise SyncConflict("Stale sync state.")
        _assign(doc,_state_payload(state,doctype=False)); doc.save(); return state
    def active_run(self,site):
        rows=self._frappe().get_all(self.RUN,filters={"site_scope":site,"state":"RUNNING"},fields=["sync_run_id"])
        if len(rows)>1: raise SyncConflict("More than one sync run is active.")
        return None if not rows else rows[0].sync_run_id
    def claim(self,run):
        frappe=self._scope(run.site_scope)
        if self.active_run(run.site_scope): raise SyncConflict("A synchronization run is already active.")
        frappe.has_permission(self.RUN,"create",throw=True); frappe.get_doc({"doctype":self.RUN,"sync_run_id":run.run_id,"site_scope":run.site_scope,"actor":run.actor,"automatic":int(run.automatic),"state":"RUNNING"}).insert()
    def release(self,run):
        frappe=self._scope(run.site_scope); rows=frappe.get_all(self.RUN,filters={"sync_run_id":run.run_id,"state":"RUNNING"},fields=["name"])
        if rows:
            doc=frappe.get_doc(self.RUN,rows[0].name); doc.state="COMPLETED"; doc.save()

class FrappeBoundIndex:
    """Read-only B6 enumeration; it never creates bindings or target records."""
    def __init__(self, frappe_module=None): self._frappe_module=frappe_module
    def fields(self, site_scope, source_instance):
        from hero_integration.services.binding_registry import FrappeBindingStorage
        storage=FrappeBindingStorage(self._frappe_module)
        result=[]
        for binding in storage.fields_for_source(site_scope,source_instance):
            if binding.direction is not MappingDirection.HERO_TO_FRAPPE: continue
            parent=storage.object(site_scope,binding.object_binding_key)
            if parent is None or parent.source_instance!=source_instance: raise SyncConflict("Bound field has no current object binding.")
            result.append(BoundField(parent.external_object_id,binding,tuple(binding.source_field.split("."))))
        return tuple(sorted(result,key=lambda field: field.binding.identity_key))

class FrappeBoundProcessor:
    """B7 update-only writer for existing B6-bound target fields.

    Initial migration owns binding creation and inserts. This processor accepts
    neither an unbound object nor a new field and cannot provision schema.
    """
    def __init__(self, mapping_revision, definition_ids, frappe_module=None):
        self.mapping_revision,self.definition_ids,self._frappe_module=mapping_revision,frozenset(definition_ids),frappe_module
    def process(self, record, fields):
        try:
            identity=_identity(record); grouped={}
            for field in fields:
                binding=field.binding
                if (field.object_id!=identity or binding.direction is not MappingDirection.HERO_TO_FRAPPE or
                    binding.mapping_revision!=self.mapping_revision or binding.mapping_definition_id not in self.definition_ids or
                    binding.state is BindingState.RECONCILIATION_REQUIRED):
                    raise SyncConflict("Bound field is stale or ineligible.")
                value=_extract_bound(record,field.source_path)
                if value is _BOUND_MISSING: return False
                grouped.setdefault((binding.target_doctype,binding.target_name),{})[binding.target_field]=value
            frappe=self._frappe()
            for (doctype,name), values in grouped.items():
                if not frappe.db.exists(doctype,name): return False
                meta=frappe.get_meta(doctype)
                if any(field=="name" or not meta.get_field(field) for field in values): return False
                frappe.has_permission(doctype,"write",throw=True)
                document=frappe.get_doc(doctype,name)
                changed={key:value for key,value in values.items() if document.get(key)!=value}
                if changed:
                    _assign(document,changed); document.save()
            return True
        except Exception:
            return False
    def _frappe(self):
        if self._frappe_module is not None: return self._frappe_module
        import frappe
        return frappe

def scheduler_due(configuration:PollingConfiguration, elapsed_seconds:object)->bool:
    """Pure scheduler gate; scheduler registration and job enqueueing are external."""
    return configuration.enabled and isinstance(elapsed_seconds,(int,float)) and not isinstance(elapsed_seconds,bool) and elapsed_seconds>=configuration.interval_seconds

class SyncEngine:
    def __init__(self, ledger:SyncLedger, plan:MaterializedReadPlan, provider:ChangeProvider, index:BoundIndex, processor:BoundProcessor): self.ledger,self.plan,self.provider,self.index,self.processor=ledger,plan,provider,index,processor
    def configure(self, configuration:PollingConfiguration, *, expected:int|None=None):
        prior=self.ledger.state(configuration.site_scope)
        if prior and prior.configuration!=configuration: return self.ledger.save_state(SyncState(configuration,prior.cursor,(),SyncStatus.RECONCILIATION_REQUIRED,prior.generation+1,"configuration changed"),prior.generation if expected is None else expected)
        return self.ledger.save_state(SyncState(configuration),expected if expected is not None else (prior.generation if prior else None))
    def poll(self, run:SyncRun):
        state=self._state(run)
        if not run.automatic: raise SyncError("Polling requires automatic run identity.")
        if not state.configuration.enabled: return replace(state,status=SyncStatus.DISABLED,diagnostic="polling disabled")
        return self._execute(run,state)
    def pending_sync(self, run:SyncRun):
        state=self._state(run)
        if run.automatic: raise SyncError("Pending Sync requires explicit initiation.")
        return self._execute(run,state)
    def _state(self,run):
        state=self.ledger.state(run.site_scope)
        if state is None or state.generation!=run.expected_generation: raise SyncConflict("Sync state is missing or stale.")
        if state.configuration.site_scope!=run.site_scope or state.configuration.source_instance!=self.plan.source_instance or state.configuration.mapping_revision!=self.plan.sealed.contract.mapping_revision or state.configuration.schema_fingerprint!=self.plan.sealed.snapshot_fingerprint: raise SyncConflict("Sync state no longer matches B7/B2 evidence.")
        return state
    def _execute(self,run,state):
        self.ledger.claim(run)
        try:
            caps=self._provider_capabilities()
            if not caps.cursor or not caps.complete_change_discovery:
                return self._incomplete(state,SyncStatus.RECONCILIATION_REQUIRED if caps.bounded_comparison else SyncStatus.INCOMPLETE,"provider cannot prove complete cursor discovery")
            fields=tuple(self.index.fields(run.site_scope,state.configuration.source_instance))
            if any(f.binding.mapping_revision!=state.configuration.mapping_revision for f in fields): return self._incomplete(state,SyncStatus.RECONCILIATION_REQUIRED,"bound field mapping revision changed")
            page=self._changes(state.cursor); seen=set(); coverage=set(state.coverage)
            while True:
                marker=_page_marker(page)
                if marker in seen: return self._incomplete(state,SyncStatus.PARTIAL,"duplicate change page")
                seen.add(marker)
                by_object={}
                for field in fields: by_object.setdefault(field.object_id,[]).append(field)
                for record in page.records:
                    identity=_identity(record)
                    selected=tuple(by_object.get(identity,()))
                    if not selected: continue
                    if not self.processor.process(record,selected): return self._incomplete(state,SyncStatus.PARTIAL,"local processing was not durable")
                    coverage.update(field.binding.identity_key for field in selected)
                if page.next_cursor is None:
                    if not page.complete: return self._incomplete(state,SyncStatus.INCOMPLETE,"provider reported incomplete changes")
                    if not page.watermark: return self._incomplete(state,SyncStatus.INCOMPLETE,"provider supplied no committed watermark")
                    return self.ledger.save_state(replace(state,cursor=page.watermark,coverage=tuple(sorted(coverage)),status=SyncStatus.SUCCEEDED,generation=state.generation+1,diagnostic=""),state.generation)
                if page.next_cursor==state.cursor: return self._incomplete(state,SyncStatus.PARTIAL,"non-advancing cursor")
                state=replace(state,cursor=page.next_cursor)
                # Cursor is intentionally not saved between pages: no partial batch can advance it.
                page=self._changes(page.next_cursor)
        except SyncError: raise
        except Exception: return self._incomplete(state,SyncStatus.FAILED,"redacted provider or local failure")
        finally: self.ledger.release(run)
    def _provider_capabilities(self): return self.provider.capabilities
    def _changes(self,cursor): return self.provider.changes(cursor)
    def _incomplete(self,state,status,diagnostic): return self.ledger.save_state(replace(state,status=status,diagnostic=diagnostic,generation=state.generation+1),state.generation)

@dataclass(frozen=True, slots=True)
class OutboundIntent:
    binding:FieldBinding|None; mapping_revision:int; snapshot_fingerprint:str; local_fingerprint:str|None; value_present:bool; document:str; variables:Mapping[str,object]
@dataclass(frozen=True, slots=True)
class OutboundApproval:
    actor_authorized:bool; mutation_approved:bool; preview_confirmed:bool; audit_reserved:bool; no_retry:bool; acknowledgment_version:int
@dataclass(frozen=True, slots=True)
class OutboundAck:
    verified:bool; evidence_id:str; source_fingerprint:str; target_fingerprint:str
class OneShotTransport(Protocol):
    def execute_once(self, document:str, variables:Mapping[str,object])->OutboundAck: ...

def sync_hero_eligibility(intent:OutboundIntent)->Eligibility:
    if intent.binding is None: return Eligibility.UNBOUND_ELIGIBLE
    binding=intent.binding
    if binding.mapping_revision!=intent.mapping_revision: return Eligibility.MAPPING_CHANGED
    if binding.state is BindingState.RECONCILIATION_REQUIRED: return Eligibility.RECONCILIATION_REQUIRED
    if not intent.local_fingerprint or binding.acknowledged_target_fingerprint!=intent.local_fingerprint: return Eligibility.LOCAL_CHANGED
    return Eligibility.NOT_ELIGIBLE

def execute_sync_hero(intent:OutboundIntent, approval:OutboundApproval, transport:OneShotTransport, bindings:BindingRegistryService)->FieldBinding:
    eligible=sync_hero_eligibility(intent)
    if eligible is Eligibility.NOT_ELIGIBLE: raise SyncConflict("Synchronized binding is not eligible for outbound sync.")
    if intent.binding is None: raise SyncError("Unbound outbound eligibility requires an explicit binding workflow, not execution.")
    if not intent.value_present or not intent.document or not isinstance(intent.variables,Mapping): raise SyncError("Missing/null values cannot become an implicit HERO mutation.")
    if not all((approval.actor_authorized,approval.mutation_approved,approval.preview_confirmed,approval.audit_reserved,approval.no_retry)): raise PermissionError("System Manager approval, exact preview confirmation, audit reservation, and no-retry transport are required.")
    ack=transport.execute_once(intent.document,dict(intent.variables))
    if not isinstance(ack,OutboundAck) or not ack.verified: raise SyncConflict("Remote acknowledgment is absent or unverified; B6 state remains unchanged.")
    return bindings.record_verified_acknowledgment(intent.binding,expected_revision=intent.binding.revision,mapping_definition_id=intent.binding.mapping_definition_id,mapping_revision=intent.mapping_revision,source_fingerprint=ack.source_fingerprint,target_fingerprint=ack.target_fingerprint,evidence_id=ack.evidence_id,version=approval.acknowledgment_version)

def _identity(record):
    value=record.get("id")
    if not isinstance(value,(str,int)) or isinstance(value,bool) or not str(value): raise SyncError("Provider record has no stable identity.")
    return str(value)
_BOUND_MISSING=object()
def _extract_bound(value,path):
    for key in path:
        if not isinstance(value,Mapping) or key not in value: return _BOUND_MISSING
        value=value[key]
    return value
def _page_marker(page): return sha256(json.dumps([page.next_cursor,page.records],sort_keys=True,default=str,separators=(",",":")).encode()).hexdigest()

def _assign(doc,values):
    for key,value in values.items(): setattr(doc,key,value)
def _state_payload(state,*,doctype=True):
    config={key:getattr(state.configuration,key) for key in state.configuration.__dataclass_fields__}
    fields={"site_scope":state.configuration.site_scope,"configuration_json":json.dumps(config,sort_keys=True,separators=(",",":")),"cursor":state.cursor or "","coverage_json":json.dumps(state.coverage),"status":state.status.value,"generation":state.generation,"diagnostic":state.diagnostic}
    return ({"doctype":FrappeSyncLedger.STATE,"name":state.configuration.site_scope}|fields) if doctype else fields
def _state_from_doc(doc):
    try: return SyncState(PollingConfiguration(**json.loads(doc.configuration_json)),doc.cursor or None,tuple(json.loads(doc.coverage_json or "[]")),SyncStatus(doc.status),int(doc.generation or 0),doc.diagnostic or "")
    except (AttributeError,TypeError,ValueError,json.JSONDecodeError) as error: raise SyncError("Persistent sync state is malformed.") from error
