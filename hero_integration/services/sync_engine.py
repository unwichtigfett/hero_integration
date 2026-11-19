"""Execute approved, bound-only synchronization against a materialized read.

Each native update, field acknowledgment and version receipt share one
transaction. Global cursor advancement requires complete discovery.
"""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
import json
from typing import Protocol

from hero_integration.graphql_protocol.operation_kind import GraphQLOperationKind
from hero_integration.services.binding_registry import BindingRegistryService, FieldBinding, BindingState, FrappeBindingStorage, state_fingerprint
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.migration_engine import SealedRead
from hero_integration.services.localized_error import LocalizedError


class SyncError(LocalizedError): pass
class SyncConflict(SyncError): pass
class SyncStatus(StrEnum): IDLE="IDLE"; RUNNING="RUNNING"; SUCCEEDED="SUCCEEDED"; PARTIAL="PARTIAL"; INCOMPLETE="INCOMPLETE"; RECONCILIATION_REQUIRED="RECONCILIATION_REQUIRED"; FAILED="FAILED"; CONFLICT="CONFLICT"; BUDGET_REACHED="BUDGET_REACHED"; BOUNDED_COMPLETE="BOUNDED_COMPLETE"; DISABLED="DISABLED"
class Eligibility(StrEnum): NOT_ELIGIBLE="NOT_ELIGIBLE"; UNBOUND_ELIGIBLE="UNBOUND_ELIGIBLE"; LOCAL_CHANGED="LOCAL_CHANGED"; MAPPING_CHANGED="MAPPING_CHANGED"; RECONCILIATION_REQUIRED="RECONCILIATION_REQUIRED"


def _text(value, label):
    if not isinstance(value, str) or not value.strip(): raise SyncError('{0} is required.', label)
    return value
def _positive(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1: raise SyncError('{0} must be positive.', label)
    return value
def _fingerprint(value):
    value=_text(value,"schema fingerprint")
    if len(value)!=64 or any(c not in "0123456789abcdef" for c in value): raise SyncError("Schema fingerprint is invalid.")
    return value

@dataclass(frozen=True, slots=True)
class PollingConfiguration:
    site_scope:str; enabled:bool; interval_seconds:int; source_instance:str; mapping_revision:int; schema_fingerprint:str; contract_fingerprint:str=""
    def __post_init__(self):
        _text(self.site_scope,"site scope"); _text(self.source_instance,"source instance"); _positive(self.mapping_revision,"mapping revision"); _fingerprint(self.schema_fingerprint)
        if self.contract_fingerprint: _fingerprint(self.contract_fingerprint)
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
        # B7 snapshot and B2 canonical IR fingerprints have separate validation
        # boundaries; the runtime reconstructor must not equate them.
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
    run_id:str; site_scope:str; actor:str; expected_generation:int; automatic:bool; request_fingerprint:str=""
    source_instance:str=""; plan_key:str=""; plan_generation:int=0; source_revision:int=0
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
    """Site-scoped intake, durable progress and transaction boundaries for synchronization."""
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
        doc=frappe.get_doc(self.STATE,state.configuration.site_scope,for_update=True)
        if _state_from_doc(doc).generation!=expected: raise SyncConflict("Stale sync state.")
        _assign(doc,_state_payload(state,doctype=False)); doc.save(); return state
    def active_run(self,site):
        rows=self._frappe().get_all(self.RUN,filters={"site_scope":site,"state":"RUNNING"},fields=["sync_run_id"])
        if len(rows)>1: raise SyncConflict("More than one sync run is active.")
        return None if not rows else rows[0].sync_run_id
    def claim(self,run):
        from hero_integration.services import run_reservation
        frappe=self._scope(run.site_scope)
        frappe.has_permission(self.RUN,"create",throw=True)
        if not run_reservation.acquire(frappe):
            raise SyncConflict("Could not acquire the HERO run reservation lock.")
        try:
            active = run_reservation.locked_legacy_slot(frappe)
            if run_reservation.legacy_slot_is_active(frappe, active) or run_reservation.locked_pending_migrations(frappe):
                raise SyncConflict("A HERO migration run is already active.")
            # Current locking reads are essential here: a transaction can
            # predate a competing reservation which committed while we waited.
            if run_reservation.locked_b8_runs(frappe, run.site_scope):
                raise SyncConflict("A synchronization run is already active.")
            state_doc = frappe.get_doc(self.STATE, run.site_scope, for_update=True)
            if int(state_doc.generation or 0) != run.expected_generation:
                raise SyncConflict("The approved sync generation changed before intake.")
            frappe.get_doc({
                "doctype": self.RUN, "sync_run_id": run.run_id, "site_scope": run.site_scope,
                "actor": run.actor, "automatic": int(run.automatic), "state": "RUNNING",
                "request_fingerprint": run.request_fingerprint, "source_instance": run.source_instance,
                "plan_key": run.plan_key, "plan_generation": run.plan_generation,
                "source_revision": run.source_revision,
            }).insert()
        except frappe.QueryDeadlockError as error:
            # The caller owns rollback; this attempt must neither retry nor
            # start provider work after a deadlock or stale snapshot conflict.
            raise SyncConflict("The HERO run reservation changed concurrently; no synchronization run was started.") from error
        finally:
            run_reservation.release(frappe)
    @contextmanager
    def execution_lease(self,run_id):
        db = self._frappe().db
        lock = sha256(f"HERO:sync:{self._frappe().local.site}:{run_id}".encode()).hexdigest()
        if db.sql("SELECT GET_LOCK(%s, 0)", (lock,))[0][0] != 1:
            raise SyncConflict("The sync worker still owns this run.")
        self._execution_lock = lock
        try:
            yield
        finally:
            self._execution_lock = None
            db.sql("SELECT RELEASE_LOCK(%s)", (lock,))

    def abort_abandoned(self,run_id,request_fingerprint):
        """Explicitly close an interrupted receipt after its DB session has gone."""
        frappe = self._scope(self._frappe().local.site)
        with self.execution_lease(run_id):
            doc = frappe.get_doc(self.RUN, run_id, for_update=True)
            if (doc.site_scope != frappe.local.site or doc.sync_run_id != run_id
                    or (doc.request_fingerprint or "") != request_fingerprint
                    or doc.state != "RUNNING"):
                raise SyncConflict("Recovery requires the exact abandoned RUNNING receipt.")
            doc.state = "ABORTED"
            doc.save()
            self.commit()
            return doc.state

    def commit(self):
        db = self._frappe().db
        lock = getattr(self,"_execution_lock",None)
        if lock and db.sql("SELECT IS_USED_LOCK(%s) = CONNECTION_ID()", (lock,))[0][0] != 1:
            db.rollback()
            raise SyncConflict("Sync worker lost its database session lease.")
        db.commit()

    def rollback(self):
        self._frappe().db.rollback()

    def progress(self,run,processed,skipped):
        self._frappe().db.sql("UPDATE `tabHERO Sync Run Ledger` SET processed_count=%s, skipped_count=%s "
            "WHERE name=%s AND site_scope=%s AND state='RUNNING'",
            (processed,skipped,run.run_id,run.site_scope))

    def release(self,run):
        self.finish(run, "COMPLETED", 0, 0)

    def finish(self,run,status,processed,skipped):
        frappe=self._scope(run.site_scope)
        rows=frappe.db.sql("SELECT name FROM `tabHERO Sync Run Ledger` WHERE site_scope=%s AND sync_run_id=%s AND state='RUNNING' FOR UPDATE", (run.site_scope,run.run_id), as_dict=True)
        if rows:
            doc=frappe.get_doc(self.RUN,rows[0].name,for_update=True)
            doc.state=str(status)
            doc.processed_count=processed
            doc.skipped_count=skipped
            doc.save()

class FrappeBoundIndex:
    """Read-only B6 enumeration; it never creates bindings or target records."""
    def __init__(self, frappe_module=None, *, object_type=None, definition_ids=None):
        self.definition_ids=frozenset(definition_ids) if definition_ids is not None else None
        self._frappe_module=frappe_module
        self.object_type=object_type
    def fields(self, site_scope, source_instance):
        storage = FrappeBindingStorage(self._frappe_module)
        frappe = storage._scope(site_scope)
        field = frappe.qb.DocType(storage.FIELD)
        parent = frappe.qb.DocType(storage.OBJECT)
        query = (frappe.qb.from_(field).left_join(parent).on(field.object_binding == parent.name)
            .select(field.star,parent.external_object_id,parent.object_type,
                    parent.site_scope.as_("parent_site"),parent.source_instance.as_("parent_source"))
            .where((field.site_scope == site_scope) & (field.source_instance == source_instance)
                   & (field.direction == MappingDirection.HERO_TO_FRAPPE.value)))
        if self.definition_ids is not None:
            if not self.definition_ids:
                return ()
            query = query.where(field.mapping_definition_id.isin(sorted(self.definition_ids)))
        result = []
        for row in query.orderby(field.identity_key).run(as_dict=True):
            if row.parent_site != site_scope or row.parent_source != source_instance or not row.external_object_id:
                raise SyncConflict("Bound field has no current object binding.")
            if self.object_type and row.object_type != self.object_type:
                continue
            binding = storage._field(row)
            result.append(BoundField(row.external_object_id,binding,tuple(binding.source_field.split("."))))
        return tuple(result)

class FrappeBoundProcessor:
    """Apply approved bound scalar changes under native Frappe row locks.

    The engine owns the transaction across native saves, acknowledgments and
    receipt insertion. Any exception rolls all three back together.
    """
    EVENT = "HERO Sync Change Event"

    def __init__(self, mapping_revision, definition_ids, frappe_module=None, document_adapter=None,
                 *, run_id=None, object_type=None, mapping_name=None, identity_path="id", contract_fingerprint=""):
        self.mapping_revision = mapping_revision
        self.definition_ids = tuple(definition_ids)
        self._frappe_module = frappe_module
        self.run_id = run_id
        self.object_type = object_type
        self.mapping_name = mapping_name
        self.contract_fingerprint = contract_fingerprint
        self.identity_path = identity_path.split(".") if isinstance(identity_path,str) else tuple(identity_path)

    def _frappe(self):
        if self._frappe_module is not None:
            return self._frappe_module
        import frappe
        return frappe

    def _event_id(self, identity, position, fields):
        first = fields[0].binding
        evidence = (first.site_scope, first.source_instance, self.object_type, identity,
                    self.mapping_name, self.mapping_revision, self.contract_fingerprint,
                    sorted(f.binding.identity_key for f in fields), position)
        return sha256(json.dumps(evidence, separators=(",",":"), ensure_ascii=False).encode()).hexdigest()

    def accepted(self, identity, event_key, fields):
        if not self.run_id or not self.mapping_name or not self.object_type:
            return False
        return self._frappe().db.get_value(self.EVENT, self._event_id(identity,event_key,fields), "outcome") == "APPLIED"

    def _approved_plan(self, frappe, source):
        from hero_integration.services.mapping_runtime import active_mapping_plan
        mapping = frappe.get_doc("HERO GraphQL Mapping", self.mapping_name, for_update=True)
        if (mapping.source_instance != source or int(mapping.mapping_revision) != self.mapping_revision
                or mapping.status != "Active" or mapping.direction != "QUERY_TO_FRAPPE"):
            raise SyncConflict("Bound write mapping changed.")
        operation = json.loads(mapping.mapping_json)["target_operation"]
        return active_mapping_plan(operation, definition_ids=self.definition_ids, mapping_name=self.mapping_name,
                                   bound_fields_only=True)

    def process(self, record, fields):
        # Compatibility with pre-release direct callers that have no run envelope.
        return False

    def process_event(self, record, fields, event_key, version):
        if not fields or not self.run_id or not self.mapping_name or not self.object_type:
            return False
        from frappe.modules.utils import get_module_app
        from hero_integration.services.direct_mapping import SYSTEM_FIELDS
        from hero_integration.services.change_detection import managed_target_update
        from hero_integration.services.query_mapping import mapped_values
        frappe = self._frappe()
        storage = FrappeBindingStorage(frappe)
        bindings = BindingRegistryService(storage)
        identity = _identity(record, self.identity_path)
        first = fields[0].binding
        if first.site_scope != frappe.local.site:
            raise SyncConflict("Bound write cannot cross site scope.")
        event_id = self._event_id(identity,event_key,fields)
        plan = self._approved_plan(frappe,first.source_instance)
        rules = {spec.definition_id:spec for spec in plan.definitions}
        targets = {}
        changes = []
        ack = []
        blocked = False
        destinations = set()
        for field in sorted(fields,key=lambda item:item.binding.identity_key):
            bound = storage._field(frappe.get_doc(storage.FIELD, field.binding.identity_key, for_update=True))
            parent = storage._object(frappe.get_doc(storage.OBJECT, bound.object_binding_key, for_update=True))
            spec = rules.get(bound.mapping_definition_id)
            target_key = (bound.target_doctype,bound.target_name)
            if (field.object_id != identity or bound.site_scope != first.site_scope or
                    bound.source_instance != first.source_instance or parent.site_scope != bound.site_scope or
                    parent.source_instance != bound.source_instance or parent.object_type != self.object_type or
                    parent.external_object_id != identity or parent.lifecycle != "VALID" or
                    (parent.target_doctype,parent.target_name,parent.target_app) != (*target_key,bound.target_app) or
                    bound.direction != MappingDirection.HERO_TO_FRAPPE or bound.mapping_revision != self.mapping_revision or
                    parent.mapping_revision != self.mapping_revision or not spec or
                    (spec.source_path,spec.target_doctype,spec.target_field) != (bound.source_field,bound.target_doctype,bound.target_field)):
                raise SyncConflict("Selected field lost its approved object, identity or mapping.")
            if (*target_key,bound.target_field) in destinations:
                raise SyncConflict("Multiple bindings address one destination field.")
            destinations.add((*target_key,bound.target_field))
            if target_key not in targets:
                target = frappe.get_doc(*target_key,for_update=True)
                target.check_permission("write")
                if get_module_app(target.meta.module) != bound.target_app:
                    raise SyncConflict("Target app no longer owns the bound document.")
                targets[target_key] = (target,{})
            target,updates = targets[target_key]
            df = target.meta.get_field(bound.target_field)
            # Native relations, child tables and derived/adapter transforms need
            # explicit write contracts of their own. They cannot fall back to identity.
            if (spec.purpose != "Field" or spec.relation or spec.transformation not in {
                    "Trim","Identity","Scalar Text","Positive Int To Text"}
                    or spec.target_field in SYSTEM_FIELDS or not df or df.is_virtual or df.fetch_from
                    or (df.read_only and not spec.target_field.startswith("custom_hero_"))
                    or df.fieldtype not in {"Data","Small Text","Text","Long Text","Select","Int","Float","Currency","Check"}
                    or int(df.permlevel or 0) not in target.get_permlevel_access("write")):
                return False
            single_field = replace(plan,definitions=(spec,))
            scoped_record = dict(record, _hero_source_instance=bound.source_instance)
            if plan.adapter_options:
                values = mapped_values(single_field,scoped_record,allow_partial=True,target_doctype=bound.target_doctype)
            else:
                values = single_field.values(scoped_record,allow_partial=True)
            projected = values.get(bound.target_field)
            if projected is None or projected == "" or isinstance(projected,(dict,list)):
                return False
            source_fp = state_fingerprint(projected,present=True)
            previous = state_fingerprint(target.get(bound.target_field),present=True)
            decision = bindings.inbound_reconciliation(bound,active_mapping_revision=self.mapping_revision,
                                                       source_fingerprint=source_fp,target_fingerprint=previous)
            if bound.state in {BindingState.MAPPING_CHANGED, BindingState.RECONCILIATION_REQUIRED}:
                decision = "CONFLICT"
            if decision == "KEEP_LOCAL":
                continue
            if decision == "CONFIRM_EQUAL" and source_fp == bound.acknowledged_source_fingerprint and previous == bound.acknowledged_target_fingerprint:
                continue
            if decision == "APPLY_SOURCE":
                updates[bound.target_field] = projected
            if decision in {"APPLY_SOURCE","CONFIRM_EQUAL"}:
                ack.append((bound,target_key,source_fp))
            else:
                blocked = True
            changes.append({"binding":bound.identity_key,"doctype":bound.target_doctype,"name":bound.target_name,
                            "field":bound.target_field,"previous_source":bound.acknowledged_source_fingerprint,
                            "previous_target":previous,"new_source":source_fp,"action":decision})
        for target_key in sorted(targets):
            target,updates = targets[target_key]
            if updates:
                target.update(updates)
                with managed_target_update(target.doctype,target.name,event_id):
                    target.save()
        for bound,target_key,source_fp in ack:
            saved = frappe.get_doc(*target_key,for_update=True)
            actual = state_fingerprint(saved.get(bound.target_field),present=True)
            if actual != source_fp:
                raise SyncConflict("Native lifecycle modified the proposed bound value.")
            bindings.record_verified_acknowledgment(bound,expected_revision=bound.revision,
                mapping_definition_id=bound.mapping_definition_id,mapping_revision=self.mapping_revision,
                source_fingerprint=source_fp,target_fingerprint=actual,evidence_id=event_id,
                version=bound.acknowledged_version+1)
        # Internal receipts have read-only user permissions. Only this checked,
        # nonwhitelisted writer creates them. Conflicts are specific to a run.
        if blocked:
            event_id = self._event_id(identity,("conflict",event_id,self.run_id),fields)
        if not frappe.db.exists(self.EVENT,event_id):
            frappe.get_doc({"doctype":self.EVENT,"event_id":event_id,"site_scope":first.site_scope,
                "source_instance":first.source_instance,"object_type":self.object_type,"external_id":identity,
                "source_version":str(version) if version!="" else state_fingerprint(record,present=True),
                "mapping_name":self.mapping_name,"mapping_revision":self.mapping_revision,"run_id":self.run_id,
                "outcome":"CONFLICT" if blocked else "APPLIED","field_changes":json.dumps(changes)
            }).insert(ignore_permissions=True)
        return SyncStatus.CONFLICT if blocked else True

def scheduler_due(configuration:PollingConfiguration, elapsed_seconds:object)->bool:
    """Pure scheduler gate; scheduler registration and job enqueueing are external."""
    return configuration.enabled and isinstance(elapsed_seconds,(int,float)) and not isinstance(elapsed_seconds,bool) and elapsed_seconds>=configuration.interval_seconds

class SyncEngine:
    def __init__(self, ledger:SyncLedger, plan:MaterializedReadPlan, provider:ChangeProvider,
                 index:BoundIndex, processor:BoundProcessor, *, reserve_identity=None,
                 revalidate=None, object_type=None):
        self.ledger,self.plan,self.provider,self.index,self.processor=ledger,plan,provider,index,processor
        self.reserve_identity=reserve_identity
        self.revalidate=revalidate
        self.object_type=object_type
    def configure(self, configuration:PollingConfiguration, *, expected:int|None=None):
        prior=self.ledger.state(configuration.site_scope)
        if prior:
            if expected is not None and prior.generation!=expected: raise SyncConflict("Stale sync state.")
            if prior.configuration==configuration: return prior
            # Polling switches and cadence are independent of the *read*
            # contract. A different source, mapping or selection invalidates its
            # cursor, even when the new selection happens to use the same schema.
            same_reader = replace(prior.configuration, enabled=configuration.enabled,
                                  interval_seconds=configuration.interval_seconds) == configuration
            return self.ledger.save_state(SyncState(configuration,prior.cursor if same_reader else None,
                prior.coverage if same_reader else (),SyncStatus.RECONCILIATION_REQUIRED,
                prior.generation+1,"configuration changed"),prior.generation)
        return self.ledger.save_state(SyncState(configuration),expected)
    def poll(self, run:SyncRun):
        state=self._state(run)
        if not run.automatic: raise SyncError("Polling requires automatic run identity.")
        if not state.configuration.enabled: return replace(state,status=SyncStatus.DISABLED,diagnostic="polling disabled")
        return self._execute(run,state)
    def pending_sync(self, run:SyncRun):
        state=self._state(run)
        if run.automatic: raise SyncError("Pending Sync requires explicit initiation.")
        return self._execute(run,state)
    def compare_bound(self,run):
        if run.automatic or self.reserve_identity is None:
            raise SyncError("Known-object comparison requires a budgeted manual run.")
        return self._execute(run,self._state(run),bounded=True)

    def _state(self,run):
        state=self.ledger.state(run.site_scope)
        if state is None or state.generation!=run.expected_generation: raise SyncConflict("Sync state is missing or stale.")
        if state.configuration.site_scope!=run.site_scope or state.configuration.source_instance!=self.plan.source_instance or state.configuration.mapping_revision!=self.plan.sealed.contract.mapping_revision or state.configuration.schema_fingerprint!=self.plan.sealed.snapshot_fingerprint: raise SyncConflict("Sync state no longer matches B7/B2 evidence.")
        return state
    def _execute(self,run,state,*,bounded=False):
        lease = getattr(self.ledger,"execution_lease",None)
        with lease(run.run_id) if lease else nullcontext():
            return self._run_under_lock(run,state,bounded=bounded)

    def _run_under_lock(self,run,state,*,bounded):
        self.ledger.claim(run)
        commit = getattr(self.ledger, "commit", lambda: None)
        rollback = getattr(self.ledger, "rollback", lambda: None)
        finish = getattr(self.ledger, "finish", None)
        commit()
        processed = skipped = 0
        outcome = SyncStatus.FAILED

        def checkpoint_record():
            progress = getattr(self.ledger, "progress", None)
            if progress:
                progress(run, processed, skipped)
            commit()

        def stop(status, diagnostic):
            nonlocal outcome
            outcome = status
            return self._incomplete(state, status, diagnostic)

        try:
            caps = self._provider_capabilities()
            if bounded and (not caps.bounded_comparison or not callable(getattr(self.provider, "read_bound", None))):
                return stop(SyncStatus.INCOMPLETE, "provider lacks an approved identity reader")
            if not bounded and (not caps.cursor or not caps.complete_change_discovery):
                return stop(SyncStatus.RECONCILIATION_REQUIRED if caps.bounded_comparison else SyncStatus.INCOMPLETE,
                            "provider cannot prove complete cursor discovery")
            fields = tuple(self.index.fields(run.site_scope, state.configuration.source_instance))
            by_object = {}
            for field in fields:
                if field.binding.mapping_revision != state.configuration.mapping_revision:
                    return stop(SyncStatus.RECONCILIATION_REQUIRED, "bound field mapping revision changed")
                by_object.setdefault(field.object_id, []).append(field)
            commit()  # No database snapshot or destination lock spans remote acquisition.
            if bounded and not by_object:
                return stop(SyncStatus.INCOMPLETE, "no eligible existing bound objects")
            identities = iter(sorted(by_object)) if bounded else None
            cursor = state.cursor
            seen_cursors = {cursor}
            coverage = set(state.coverage)
            identity_path = self.plan.sealed.contract.identity_path.split(".")
            while True:
                if bounded:
                    bound_id = next(identities, None)
                    if bound_id is None:
                        if self.revalidate:
                            self.revalidate()
                        return stop(SyncStatus.BOUNDED_COMPLETE, "reviewed existing identities compared; global cursor unchanged")
                    page = ChangePage(({"id":bound_id},),None,False)
                else:
                    if self.revalidate:
                        self.revalidate()
                        commit()  # Never span a remote read with Frappe evidence/target locks.
                    page = self._changes(cursor)
                if len(page.records) > self.plan.sealed.contract.page_size:
                    return stop(SyncStatus.INCOMPLETE, "provider exceeded approved page size")
                if page.next_cursor is not None and (not page.next_cursor or page.next_cursor in seen_cursors):
                    return stop(SyncStatus.PARTIAL, "repeated cursor")
                for position,record in enumerate(page.records):
                    fast = getattr(self.provider, "detail_for", None)
                    identity = _identity(record) if (bounded or fast) else _identity(record, identity_path)
                    if self.reserve_identity and not self.reserve_identity(self.object_type, identity):
                        return stop(SyncStatus.BUDGET_REACHED, "global test budget exhausted")
                    selected = tuple(by_object.get(identity, ()))
                    if not selected:
                        skipped += 1
                        checkpoint_record()
                        continue
                    # Proven, immutable versions can be recognized across cursor replays.
                    version = self.provider.version(record) if fast else ""
                    event_key = (("bound",run.run_id) if bounded else ("version", version) if fast else
                                 ("position", cursor, position, _page_marker(ChangePage((record,), None, True))))
                    receipt = getattr(self.processor, "accepted", None)
                    if receipt and receipt(identity, event_key, selected):
                        coverage.update(field.binding.identity_key for field in selected)
                        skipped += 1
                        checkpoint_record()
                        continue
                    if (bounded or fast) and self.revalidate:
                        self.revalidate()
                    commit()
                    if bounded:
                        record = self.provider.read_bound(identity)
                        if not isinstance(record, Mapping) or _identity(record, identity_path) != identity:
                            return stop(SyncStatus.INCOMPLETE, "identity reader returned unexpected identity")
                    elif fast:
                        record = fast(record)
                        if _identity(record, identity_path) != identity:
                            raise SyncConflict("Detail does not match the approved identity binding.")
                    if self.revalidate:
                        self.revalidate()
                    apply = getattr(self.processor, "process_event", None)
                    result = (apply(record, selected, event_key, version) if apply else
                              self.processor.process(record, selected))
                    if result == SyncStatus.CONFLICT:
                        return stop(SyncStatus.CONFLICT, "binding or field baseline conflict")
                    if not result:
                        return stop(SyncStatus.PARTIAL, "local processing was not durable")
                    processed += 1
                    coverage.update(field.binding.identity_key for field in selected)
                    checkpoint_record()  # Target save, acknowledgments and receipt commit together.
                if bounded:
                    continue
                if page.next_cursor is None:
                    if not page.complete or not page.watermark:
                        return stop(SyncStatus.INCOMPLETE, "provider supplied no complete watermark")
                    if self.revalidate:
                        self.revalidate()
                    done = self.ledger.save_state(replace(state,cursor=page.watermark,
                        coverage=tuple(sorted(coverage)),status=SyncStatus.SUCCEEDED,
                        generation=state.generation+1,diagnostic=""),state.generation)
                    outcome = SyncStatus.SUCCEEDED
                    return done
                cursor = page.next_cursor
                seen_cursors.add(cursor)
        except SyncConflict:
            rollback()
            outcome = SyncStatus.CONFLICT
            raise
        except SyncError:
            rollback()
            outcome = SyncStatus.INCOMPLETE
            raise
        except Exception:
            rollback()  # Discard target changes and queued Frappe after_commit callbacks.
            return stop(SyncStatus.FAILED, "redacted provider or local failure")
        finally:
            if finish:
                finish(run, outcome, processed, skipped)
                commit()
            else:
                self.ledger.release(run)
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
    if binding.state is BindingState.MAPPING_CHANGED: return Eligibility.MAPPING_CHANGED
    if binding.state in {BindingState.RECONCILIATION_REQUIRED, BindingState.REMOTE_CHANGED}: return Eligibility.RECONCILIATION_REQUIRED
    if not intent.local_fingerprint or binding.acknowledged_target_fingerprint!=intent.local_fingerprint: return Eligibility.LOCAL_CHANGED
    return Eligibility.NOT_ELIGIBLE

def execute_sync_hero(intent:OutboundIntent, approval:OutboundApproval, transport:OneShotTransport, bindings:BindingRegistryService)->FieldBinding:
    eligible=sync_hero_eligibility(intent)
    if eligible is not Eligibility.LOCAL_CHANGED: raise SyncConflict("Only a local field change is eligible for outbound sync; reconcile other states first.")
    if intent.binding is None: raise SyncError("Unbound outbound eligibility requires an explicit binding workflow, not execution.")
    if not intent.value_present or not intent.document or not isinstance(intent.variables,Mapping): raise SyncError("Missing/null values cannot become an implicit HERO mutation.")
    if not all((approval.actor_authorized,approval.mutation_approved,approval.preview_confirmed,approval.audit_reserved,approval.no_retry)): raise PermissionError("System Manager approval, exact preview confirmation, audit reservation, and no-retry transport are required.")
    ack=transport.execute_once(intent.document,dict(intent.variables))
    if not isinstance(ack,OutboundAck) or not ack.verified or ack.target_fingerprint != intent.local_fingerprint: raise SyncConflict("Remote acknowledgment is absent or unverified; B6 state remains unchanged.")
    return bindings.record_verified_acknowledgment(intent.binding,expected_revision=intent.binding.revision,mapping_definition_id=intent.binding.mapping_definition_id,mapping_revision=intent.mapping_revision,source_fingerprint=ack.source_fingerprint,target_fingerprint=ack.target_fingerprint,evidence_id=ack.evidence_id,version=approval.acknowledgment_version)

def _identity(record, path=("id",)):
    value=_extract_bound(record, path)
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
