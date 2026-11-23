"""Isolated native document/transaction regression with synthetic source data."""
import json
from contextlib import contextmanager
from graphql import build_schema, introspection_from_schema
from unittest.mock import patch
from uuid import uuid4
from hashlib import sha256
from dataclasses import replace

import frappe
from frappe.model.document import Document
from frappe.tests import IntegrationTestCase

from hero_integration.services.binding_registry import (
    BindingRegistryService, FrappeBindingStorage, state_fingerprint,
)
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.sync_engine import (
    ChangePage, FrappeBoundIndex, FrappeBoundProcessor, FrappeSyncLedger,
    PollingConfiguration, SyncEngine, SyncRun, SyncStatus, SyncConflict, MaterializedReadPlan,
)
from hero_integration.tests.test_sync_engine import Provider
from hero_integration.services.mapping_contract_v1 import QueryAdapter, QueryDraft, SnapshotAdapter, StoredSnapshot
from hero_integration.services.migration_engine import SealedRead
from hero_integration.services.read_contract import ReadContract
from hero_integration.services.introspection import endpoint_fingerprint
from hero_integration.services.runtime_plan import RuntimePlanSelection
from hero_integration.services.b8_runtime import canonical_operation_contract


class TestNativeSyncEvents(IntegrationTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.site = frappe.local.site
        self.ledger = FrappeSyncLedger()
        if self.ledger.state(self.site) or self.ledger.active_run(self.site):
            self.skipTest("This test requires a disposable site with an unused synchronization slot.")
        self.identity = uuid4().hex
        self.identity = str(int(self.identity[:7],16)+1)
        schema=introspection_from_schema(build_schema("type Query { contacts(ids: [Int!]): [Contact] } type Contact { id: ID category: String is_contact_person: Boolean is_deleted: Boolean }"))
        self.snapshot=frappe.get_doc({"doctype":"HERO GraphQL Schema Snapshot","source_instance":"hero",
                                      "endpoint_fingerprint":endpoint_fingerprint(
                                          frappe.get_single("HERO Integration Settings").hero_api_url,"hero"),
                                      "snapshot_json":json.dumps(schema)}).insert()
        self.rule=frappe.get_doc({"doctype":"HERO Frappe Mapping","schema_snapshot":self.snapshot.name,
            "operation":"HeroCustomers","source_path":"category","source_type":"string|null",
            "target_doctype":"Customer","target_field":"customer_details","purpose":"Field","transformation":"Trim",
            "status":"Active"}).insert()
        request=QueryAdapter().materialize(SnapshotAdapter().adapt(StoredSnapshot(
            self.snapshot.name,self.snapshot.snapshot_json,self.snapshot.schema_fingerprint,"hero")),
            QueryDraft("contacts","contacts","contacts",("id","category","is_contact_person","is_deleted"),{}))
        self.request=request
        self.provisional=RuntimePlanSelection(self.site,"hero","service",1,1,"mapping",1,
            self.snapshot.name,self.snapshot.schema_fingerprint,"contacts","AUTO_SYNC")
        config={"target_operation":"HeroCustomers","mapping_definition_ids":[self.rule.definition_id],
            "identity_path":"id","records_path":"contacts","variables":{},"page_size":10,
            "b8_compilation":{"version":1,"operation_name":"contacts","alias":"contacts","field":"contacts",
                "selected_paths":["id","category","is_contact_person","is_deleted"],"variables":{}},
            "b8_canonical_operation":canonical_operation_contract(self.provisional,request)}
        self.mapping=frappe.get_doc({"doctype":"HERO GraphQL Mapping","schema_snapshot":self.snapshot.name,
            "operation_kind":"QUERY","operation_name":"contacts","direction":"QUERY_TO_FRAPPE","status":"Active",
            "mapping_json":json.dumps(config)}).insert()
        self.doc=frappe.get_doc({"doctype":"Customer","customer_name":self.identity,"customer_type":"Company",
                                 "customer_details":self.identity}).insert()
        self.registry = BindingRegistryService(FrappeBindingStorage())
        self.parent = self.registry.create_or_resolve_object_binding(site_scope=self.site, source_instance="hero",
            object_type="Contact",external_object_id=self.identity,target_app="erpnext",target_doctype="Customer",
            target_name=self.doc.name,mapping_definition_id=self.rule.definition_id,mapping_revision=1)
        self.binding = self.registry.create_or_resolve_field_binding(self.parent,source_field="category",target_field="customer_details",
            mapping_definition_id=self.rule.definition_id,mapping_revision=1,direction=MappingDirection.HERO_TO_FRAPPE)
        current = state_fingerprint(self.identity,present=True)
        self.registry.record_verified_acknowledgment(self.binding,expected_revision=1,mapping_definition_id=self.rule.definition_id,
            mapping_revision=1,source_fingerprint=current,target_fingerprint=current,evidence_id="fixture",version=1)
        self.addCleanup(self.clear_fixture)
        self.hooks = []
        self.post_commit = []
        self.payload = {"id":self.identity,"is_deleted":False,"is_contact_person":False,"category":"  revised  "}
        self.pages = {None:ChangePage((self.payload,),None,True,"w1")}
        self.processor = FrappeBoundProcessor(1,(self.rule.definition_id,),run_id=self.identity,object_type="Contact",mapping_name=self.mapping.name)
        contract=ReadContract("synthetic-service",1,self.mapping.name,1,"HeroCustomers","contacts","id",
            (self.rule.definition_id,),{},10)
        sealed=SealedRead(contract,request.document,self.snapshot.schema_fingerprint,
                          sha256(request.document.encode()).hexdigest())
        read=MaterializedReadPlan(sealed,request,"hero")
        self.engine = SyncEngine(self.ledger,read,Provider(self.pages),
            FrappeBoundIndex(object_type="Contact",definition_ids=(self.rule.definition_id,)),self.processor)
        self.engine.configure(PollingConfiguration(self.site,False,60,"hero",1,self.snapshot.schema_fingerprint))
        frappe.db.commit()

    def clear_fixture(self):
        frappe.db.rollback()
        frappe.db.delete("HERO Integration Record", {"correlation_id": self.parent.identity_key})
        frappe.db.delete("HERO Sync Change Event",{"site_scope":self.site,"external_id":self.identity})
        frappe.db.delete("HERO Sync Run Ledger",{"request_fingerprint":self.identity})
        frappe.db.delete("HERO Sync State Ledger",{"site_scope":self.site})
        frappe.db.delete("HERO Field Binding",self.binding.identity_key)
        frappe.db.delete("HERO Object Binding",self.parent.identity_key)
        frappe.db.delete("Customer",self.doc.name)
        frappe.db.delete("HERO GraphQL Mapping",self.mapping.name)
        frappe.db.delete("HERO Frappe Mapping",self.rule.name)
        frappe.db.delete("HERO GraphQL Schema Snapshot",self.snapshot.name)
        frappe.db.commit()

    def run_sync(self):
        run_id=uuid4().hex
        self.processor.run_id=run_id
        run = SyncRun(run_id,self.site,"Administrator",self.ledger.state(self.site).generation,False,self.identity)
        return self.engine.pending_sync(run)

    @contextmanager
    def observe_native_save(self):
        original = Document.run_method

        def observed(doc,method,*args,**kwargs):
            if (doc.doctype,doc.name,method) == ("Customer",self.doc.name,"on_update"):
                self.hooks.append(doc.customer_details)
                frappe.db.after_commit.add(lambda: self.post_commit.append(doc.customer_details))
            return original(doc,method,*args,**kwargs)

        with patch.object(Document,"run_method",observed):
            yield

    def test_bound_native_changes_capture_committed_transitions_once(self):
        from hero_integration.services.change_detection import observe_frappe_target_change

        initial_generation = self.ledger.state(self.site).generation
        filters = {"correlation_id": self.parent.identity_key, "origin": "frappe"}
        self.doc.customer_details = "local"
        self.doc.save()
        observe_frappe_target_change(self.doc)
        self.assertEqual(frappe.db.count("HERO Integration Record", filters), 1)
        frappe.db.rollback()
        self.assertEqual(frappe.db.count("HERO Integration Record", filters), 0)
        self.doc.reload()
        for value in ("local", "local", self.identity, "local"):
            self.doc.customer_details = value
            self.doc.save()
            frappe.db.commit()
        rows = frappe.get_all("HERO Integration Record", filters=filters,
                              fields=["source_id", "changed_fields", "target_name"], order_by="creation asc")
        self.assertEqual([(row.source_id, row.changed_fields, row.target_name) for row in rows],
                         [(self.identity, "customer_details", self.doc.name)] * 3)
        self.assertEqual(self.ledger.state(self.site).generation, initial_generation)

    def test_inbound_save_does_not_observe_its_own_binding(self):
        self.assertEqual(self.run_sync().status, SyncStatus.SUCCEEDED)
        self.assertFalse(frappe.db.exists("HERO Integration Record", {
            "correlation_id": self.parent.identity_key, "origin": "frappe"}))

    def test_native_event_ack_and_replay_after_cursor_failure(self):
        self.pages[None]=ChangePage((self.payload,),"page-two",False)
        with self.observe_native_save():
            first=self.run_sync()
            self.assertEqual((first.status,first.cursor),(SyncStatus.FAILED,None))
            self.pages["page-two"]=ChangePage((),None,True,"w1")
            retry=self.run_sync()
        self.assertEqual((retry.status,retry.cursor),(SyncStatus.SUCCEEDED,"w1"))
        self.assertEqual(self.hooks,["revised"])
        self.assertEqual(self.post_commit,["revised"])
        self.assertEqual(frappe.get_doc("Customer",self.doc.name).customer_details,"revised")
        self.assertEqual(frappe.db.count("HERO Sync Change Event",{"external_id":self.identity}),1)

    def test_changed_execution_contract_cannot_reuse_a_fast_receipt(self):
        fields = self.engine.index.fields(self.site, "hero")
        self.processor.contract_fingerprint = "a" * 64
        previous = self.processor._event_id(self.identity, "v1", fields)
        self.processor.contract_fingerprint = "b" * 64
        self.assertNotEqual(previous, self.processor._event_id(self.identity, "v1", fields))

    def test_local_conflict_preserves_value_and_advances_verified_watermark(self):
        self.doc.customer_details="local"
        self.doc.save()
        frappe.db.commit()
        with self.observe_native_save():
            result=self.run_sync()
        self.assertEqual((result.status,result.cursor),(SyncStatus.CONFLICT,"w1"))
        self.assertEqual(self.hooks,[])
        self.assertEqual(frappe.get_doc("Customer",self.doc.name).customer_details,"local")
        self.assertEqual(frappe.db.count("HERO Sync Change Event",{"external_id":self.identity,"outcome":"FIELD_CONFLICT"}),1)

    def test_watermark_failure_replays_open_field_review_without_a_second_native_write(self):
        self.doc.customer_details = "locally edited"
        self.doc.save()
        frappe.db.commit()
        save_state = self.ledger.save_state
        def reject_watermark(state, generation):
            if state.cursor == "w1":
                raise RuntimeError("watermark transaction interrupted")
            return save_state(state, generation)

        with patch.object(self.ledger, "save_state", side_effect=reject_watermark):
            failed = self.run_sync()
        self.assertEqual((failed.status, failed.cursor), (SyncStatus.FAILED, None))
        review = frappe.db.get_value("HERO Sync Change Event", {"open_field": self.binding.identity_key}, "name")
        self.assertTrue(review)
        with self.observe_native_save():
            replay = self.run_sync()
        self.assertEqual((replay.status, replay.cursor), (SyncStatus.CONFLICT, "w1"))
        self.assertEqual(frappe.db.get_value("HERO Sync Change Event", review, "outcome"), "FIELD_CONFLICT")
        self.assertEqual(self.doc.reload().customer_details, "locally edited")
        self.assertEqual(self.hooks, [])
        self.assertEqual(frappe.db.count("HERO Sync Change Event", {"external_id": self.identity}), 2)
        self.pages["w1"] = ChangePage((), None, True, "w2")
        idle_interval = self.run_sync()
        self.assertEqual((idle_interval.status, idle_interval.cursor), (SyncStatus.CONFLICT, "w2"))
        self.assertFalse(FrappeBoundIndex(object_type="ProjectMatch").has_open_review(self.site, "hero", 1))

    def test_replayed_review_converging_on_next_page_finishes_without_open_conflict(self):
        self.doc.customer_details = "locally edited"
        self.doc.save()
        frappe.db.commit()
        self.pages[None] = ChangePage((self.payload,), "next", False)
        interrupted = self.run_sync()
        self.assertEqual((interrupted.status, interrupted.cursor), (SyncStatus.FAILED, None))
        review = frappe.db.get_value("HERO Sync Change Event", {"open_field": self.binding.identity_key}, "name")
        self.assertTrue(review)
        self.pages["next"] = ChangePage((dict(self.payload, category="locally edited"),), None, True, "w1")
        with self.observe_native_save():
            resumed = self.run_sync()
        self.assertEqual(frappe.db.get_value("HERO Sync Change Event", review, "outcome"), "CONVERGED")
        self.assertEqual(self.hooks, [])
        self.assertEqual((resumed.status, resumed.cursor), (SyncStatus.SUCCEEDED, "w1"))

    def test_ack_exception_rolls_back_native_write_and_after_commit_work(self):
        with self.observe_native_save(), patch.object(BindingRegistryService,"record_verified_acknowledgment",
                                                     side_effect=RuntimeError("interrupted before ack")):
            result=self.run_sync()
        self.assertEqual((result.status,result.cursor),(SyncStatus.FAILED,None))
        self.assertEqual(self.hooks,["revised"])
        self.assertEqual(self.post_commit,[])
        self.assertEqual(frappe.get_doc("Customer",self.doc.name).customer_details,self.identity)
        self.assertFalse(frappe.db.exists("HERO Sync Change Event",{"external_id":self.identity}))


    def test_explicit_abort_requires_exclusive_session_lease_and_matching_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        run = SyncRun(uuid4().hex,self.site,"Administrator",self.ledger.state(self.site).generation,False,self.identity)
        self.ledger.claim(run)
        frappe.db.commit()
        sites_path = frappe.local.sites_path

        def other_session():
            frappe.init(site=self.site, sites_path=sites_path)
            try:
                frappe.connect()
                frappe.set_user("Administrator")
                with self.assertRaises(SyncConflict):
                    FrappeSyncLedger().abort_abandoned(run.run_id,self.identity)
            finally:
                frappe.destroy()

        with self.ledger.execution_lease(run.run_id), ThreadPoolExecutor(max_workers=1) as workers:
            workers.submit(other_session).result(timeout=5)
        with self.assertRaises(SyncConflict):
            self.ledger.abort_abandoned(run.run_id,"wrong-request")
        self.assertEqual(self.ledger.abort_abandoned(run.run_id,self.identity),"ABORTED")
        self.assertIsNone(self.ledger.active_run(self.site))

    def test_administrative_bound_oneshot_through_approved_materialized_service(self):
        from types import SimpleNamespace
        from hero_integration import api
        from hero_integration.client.graphql import HeroGraphQLClient
        from hero_integration.services.sync_runtime_adapter import _runtime_plan_service

        settings=frappe.get_single("HERO Integration Settings")
        old={key:settings.get(key) for key in ("hero_source_instance","polling_mode",
             "source_binding_revision","source_binding_confirmed_revision")}
        service=frappe.get_doc({"doctype":"HERO GraphQL Service","service_name":"Bound-"+self.identity,
            "operation_name":"contacts","read_mapping":self.mapping.name,"schema_snapshot":self.snapshot.name,
            "query_document":self.request.document,"status":"Validated"}).insert()
        req_id=uuid4().hex
        def cleanup():
            frappe.db.rollback()
            for key,value in old.items():
                frappe.db.set_single_value("HERO Integration Settings",key,value)
            frappe.db.delete("HERO Sync Run Ledger",req_id)
            frappe.db.delete("HERO Execution Budget",{"run_id":req_id})
            frappe.db.delete("HERO Runtime Plan Audit",{"runtime_plan":self.plan_key})
            frappe.db.delete("HERO Runtime Plan",{"plan_key":self.plan_key})
            frappe.db.delete("HERO GraphQL Service",service.name)
            frappe.db.commit()
        plans=_runtime_plan_service(frappe)
        settings.hero_source_instance="hero"
        settings.polling_mode="OFF"
        settings.save()
        revision=int(settings.source_binding_revision)
        frappe.db.set_single_value("HERO Integration Settings","source_binding_confirmed_revision",revision)
        api.approve_read_service(service.name,self.mapping.name,service.service_revision,self.mapping.mapping_revision)
        service.reload()
        selection=replace(self.provisional,graphql_service=service.name,graphql_mapping=self.mapping.name)
        draft=plans.create_draft(selection,actor="Administrator")
        self.plan_key=draft.plan_key
        self.addCleanup(cleanup)
        approved=plans.approve(draft.plan_key,expected_revision=draft.revision,actor="Administrator")
        frappe.db.commit()
        config=SimpleNamespace(source_binding_confirmed=True,source_binding_revision=revision,
            source_instance="hero",endpoint=settings.hero_api_url,api_token="synthetic",timeout_seconds=1)
        args=dict(request_id=req_id,plan_key=approved.plan_key,expected_plan_generation=approved.generation,
                  expected_source_revision=revision,confirmed=1,scope="BOUND_OBJECTS")
        with self.observe_native_save(), patch("hero_integration.client.settings.get_configuration",return_value=config),                 patch.object(HeroGraphQLClient,"query",return_value={"contacts":[self.payload]}) as transport:
            receipt=api.start_sync_once(**args)
            self.assertEqual((receipt["status"],receipt["reserved"],receipt["processed"]),
                             ("BOUNDED_COMPLETE",1,1))
            self.assertEqual(api.start_sync_once(**args),receipt)
            self.assertEqual(transport.call_count,1)
            self.assertIn("ids:",transport.call_args.args[0])
        self.assertEqual(self.post_commit,["revised"])
        self.assertEqual(frappe.get_doc("Customer",self.doc.name).customer_details,"revised")
        self.assertIsNone(self.ledger.state(self.site).cursor)
        self.assertEqual(frappe.get_single("HERO Integration Settings").polling_mode,"OFF")
        doc = self.doc.reload()
        doc.customer_details = "local decision"
        doc.save()
        frappe.db.commit()
        self.payload["category"] = "HERO decision"
        self.pages[None] = ChangePage((self.payload,), None, True, "w2")
        self.assertEqual(self.run_sync().status, SyncStatus.CONFLICT)
        event = frappe.get_doc("HERO Sync Change Event", {"open_field": self.binding.identity_key})
        with patch("hero_integration.client.settings.get_configuration", return_value=config), \
             patch.object(HeroGraphQLClient, "query", return_value={"contacts": [self.payload]}) as query:
            preview = api.preview_sync_field(event.name, str(event.modified), "HERO")
            result = api.resolve_sync_field(event.name, str(event.modified), "HERO",
                confirmation_fingerprint=preview["confirmation_fingerprint"], confirmed=True)
        self.assertEqual((result["field"], doc.reload().customer_details), ("customer_details", "HERO decision"))
        self.assertEqual(query.call_count, 2)
        self.assertIn("ids:", query.call_args.args[0])
        self.assertEqual(event.reload().outcome, "RESOLVED")


    def test_field_preview_is_read_only_and_confirmation_binds_actor_values_and_revision(self):
        from types import SimpleNamespace
        from hero_integration import api
        from hero_integration.services import conflict_report

        self.doc.customer_details = "local"
        self.doc.save()
        frappe.db.commit()
        self.assertEqual(self.run_sync().status, SyncStatus.CONFLICT)
        event = frappe.get_doc("HERO Sync Change Event", {"open_field": self.binding.identity_key})
        args = dict(event=event.name, expected_modified=str(event.modified), choice="CUSTOM",
                    mutation_mapping="approved", value_json=json.dumps("chosen"))
        context = dict(mapping={"target": {"operation": "updateContact", "field": "category", "argument": "data"}},
                       intent=SimpleNamespace(variables={"data": {"id": int(self.identity), "category": "chosen"}}),
                       fingerprint="approved-revision-1")
        read = lambda *_: (self.payload, self.processor._approved_plan(frappe, "hero"), 1, "id")
        with patch.object(conflict_report, "_read_reviewed_record", side_effect=read), \
             patch.object(api, "_sync_hero_context", return_value=context), \
             patch.object(api, "_reserve_mutation") as reserve:
            try:
                frappe.set_user("Guest")
                with self.assertRaises(frappe.PermissionError):
                    api.preview_sync_field(**args)
            finally:
                frappe.set_user("Administrator")
            preview = api.preview_sync_field(**args)
            self.assertEqual((preview["hero_value"], preview["erpnext_value"], preview["chosen_value"],
                              preview["operation"]), ("revised", "local", "chosen", "updateContact"))
            self.assertEqual(event.reload().outcome, "FIELD_CONFLICT")
            self.assertFalse(event.resolution_json)
            self.assertEqual(preview["remote_change"], {"object_type": "Contact", "id": int(self.identity),
                                                        "field": "category", "value": "chosen"})
            self.assertEqual(self.doc.reload().customer_details, "local")
            token = preview["confirmation_fingerprint"]
            for overrides in ({"confirmed": False}, {"confirmation_fingerprint": ""},
                              {"value_json": json.dumps("another choice")}, {"expected_modified": "stale"}):
                confirmation = dict(args, confirmed=True, confirmation_fingerprint=token)
                confirmation.update(overrides)
                with self.assertRaises(frappe.ValidationError):
                    api.resolve_sync_field(**confirmation)
            with patch.object(frappe.local, "lang", "de"):
                with self.assertRaisesRegex(frappe.ValidationError, "Öffnen Sie die Vorschau erneut"):
                    api.resolve_sync_field(**args, confirmation_fingerprint=token, confirmed=False)
            context["fingerprint"] = "approved-revision-2"
            with self.assertRaises(frappe.ValidationError):
                api.resolve_sync_field(**args, confirmation_fingerprint=token, confirmed=True)
            reserve.assert_not_called()

    def test_admin_custom_field_decision_commits_one_dispatch_and_verified_ack(self):
        self._resolve_reviewed_outbound("CUSTOM", timeout=False)

    def test_admin_local_field_after_timeout_requires_readback_without_resend(self):
        self._resolve_reviewed_outbound("ERPNEXT", timeout=True)

    def _resolve_reviewed_outbound(self, choice, *, timeout):
        from types import SimpleNamespace
        from hero_integration import api
        from hero_integration.services import conflict_report

        self.doc.customer_details = "locally selected"
        self.doc.save()
        frappe.db.commit()
        self.assertEqual(self.run_sync().status, SyncStatus.CONFLICT)
        event = frappe.get_doc("HERO Sync Change Event", {"open_field": self.binding.identity_key})
        version = str(event.modified)
        chosen = "reviewed correction" if choice == "CUSTOM" else self.doc.customer_details
        args = dict(value=chosen, mutation_mapping="approved-test-mapping")
        read = lambda *_: (self.payload, self.processor._approved_plan(frappe, "hero"), 1, "id")
        audit_scope = self.identity
        wire_context = {"mapping": {"target":{"operation":"review_fixture","argument":"data","field":"category"}}, "fingerprint": audit_scope,
            "executable": SimpleNamespace(selection=SimpleNamespace(schema_fingerprint=self.snapshot.schema_fingerprint)),
            "intent": SimpleNamespace(document="synthetic", variables={"data": {"id": int(self.identity), "category": chosen}})}
        def deliver(transport, _document, variables):
            transport._context["before_mutation"]()
            if timeout:
                raise TimeoutError("synthetic unknown outcome")
            self.payload["category"] = variables["data"]["category"]
        try:
            with patch.object(conflict_report, "_read_reviewed_record", side_effect=read), \
                 patch.object(api, "_assert_mutation_execution_released"), \
                 patch.object(api, "_sync_hero_context", return_value=wire_context), \
                 patch.object(api._SyncHeroTransport, "execute_once", autospec=True, side_effect=deliver) as dispatch:
                def resolve_current():
                    preview = conflict_report.preview_field(event.name, version, choice, **args)
                    return conflict_report.resolve_field(event.name, version, choice, **args, confirmed=True,
                        confirmation_fingerprint=preview["confirmation_fingerprint"])
                if timeout:
                    with self.assertRaises(TimeoutError):
                        resolve_current()
                    with self.assertRaises(frappe.ValidationError):
                        resolve_current()
                    self.assertEqual(self.registry.get_field_bindings(self.site,self.parent.identity_key)[0].acknowledged_version,1)
                    self.payload["category"] = chosen  # Late confirmed remote delivery.
                result = resolve_current()
                frappe.db.commit()
                self.assertEqual(result["status"], "RESOLVED")
                dispatch.assert_called_once()
            self.assertEqual(self.doc.reload().customer_details, chosen)
            current, = self.registry.get_field_bindings(self.site,self.parent.identity_key)
            self.assertEqual((current.acknowledged_version,json.loads(current.acknowledged_value_json)),(2,chosen))
            self.assertEqual(event.reload().outcome, "RESOLVED")
        finally:
            frappe.db.rollback()
            frappe.db.delete("HERO Mutation Audit", {"request_fingerprint":sha256((event.name + audit_scope).encode()).hexdigest()})
            frappe.db.commit()
