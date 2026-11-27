"""The approved collection is revisited within a persisted page budget."""
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services.b8_runtime import ApprovedBoundReader


class TestInventoryPages(TestCase):
    def reader(self, document=None):
        reader = ApprovedBoundReader.__new__(ApprovedBoundReader)
        request = NS(document=document or "query Rows($first:Int!,$offset:Int!){contacts(first:$first,offset:$offset,orderBy:\"id\"){id}}",
                     variables={"first": 100, "offset": 0})
        contract = NS(records_path="contacts", identity_path="id", page_size=100, target_operation="HeroCustomers",
                      variables={"first": "$page_size", "offset": "$offset"})
        reader.plan = NS(request=request, sealed=NS(contract=contract))
        self.rows = [{"id": i, "modified": "same"} for i in range(1, 214)]
        self.transport = Mock()
        self.transport.query.side_effect = lambda _, args: {"contacts": self.rows[args["offset"]:][:args["first"]]}
        reader._client = lambda: self.transport
        self.enterContext(patch("hero_integration.graphql_protocol.graphql_result_decoder.decode_graphql_result"))
        return reader

    def test_multiple_ticks_include_rows_after_100_and_changes_in_overlap(self):
        reader = self.reader()
        first = reader.inventory_page({}, 100, 5)
        self.rows[98]["child"] = "changed"
        second = reader.inventory_page(first.checkpoint, 100, 5)
        third = reader.inventory_page(second.checkpoint, 100, 5)
        end = reader.inventory_page(third.checkpoint, 100, 5)
        self.assertEqual([args.args[1]["offset"] for args in self.transport.query.call_args_list], [0, 95, 190, 213])
        self.assertEqual(set(row["id"] for page in (first, second, third) for row in page.rows), set(range(1, 214)))
        self.assertIn("99", second.changed)
        self.assertIsNone(end.next_offset)

    def test_allocation_resize_resumes_offset_without_skipping_changed_overlap(self):
        reader = self.reader()
        first = reader.inventory_page({}, 10, 5)
        self.rows[5]["modified"] = "changed"
        small = reader.inventory_page(first.checkpoint, 1, 0)
        self.assertEqual([row["id"] for row in small.rows], [6])
        self.assertEqual(small.next_offset, 6)
        larger = reader.inventory_page(small.checkpoint, 7, 2)
        self.assertEqual([row["id"] for row in larger.rows], list(range(7, 14)))

    def test_inventory_exhausts_project_logbook_with_the_approved_parent_request(self):
        from graphql import build_schema
        reader = self.reader('query P($first:Int!,$offset:Int!,$history_offset:Int!){project_matches(first:$first,offset:$offset,orderBy:"id"){id modified histories(first:100,offset:$history_offset){id}}}')
        reader.plan.request.variables = {"first":1,"offset":0,"history_offset":0}
        contract = NS(target_operation="HeroProjectMatches",records_path="project_matches",identity_path="id",
                      page_size=1,variables={"first":"$page_size","offset":"$offset","history_offset":0},
                      adapter_options={"logbook":{"path":"histories","identity_path":"id","offset_variable":"history_offset","max_pages":4}})
        reader.plan.sealed.contract = contract
        reader.schema = NS(core=build_schema('type Query { project_matches(first:Int!,offset:Int!,orderBy:String):[P] } type P { id:Int modified:String histories(first:Int,offset:Int):[H] } type H { id:Int }'),fingerprint="e"*64)
        reader.history_source = NS(contract=contract,revalidate=lambda:contract,client=self.transport)
        def response(doc, params):
            start = params["history_offset"]
            return {"project_matches":[{"id":19,"modified":"same","histories":[{"id":i} for i in range(start+1,min(5,start+3))]}]}
        self.transport.query.side_effect = response
        page = reader.inventory_page({},1,0)
        self.assertTrue(page.rows[0]["_hero_logbook_complete"])
        self.assertEqual([row["id"] for row in page.rows[0]["histories"]], [1,2,3,4])
        self.assertEqual([args.args[1]["history_offset"] for args in self.transport.query.call_args_list], [0,2,4])

    def test_employee_directory_uses_the_approved_nested_reader_and_reports_physical_volume(self):
        reader = self.reader("query E { company { partners { id modified } } }")
        reader.plan.sealed.contract = NS(records_path="company.partners", identity_path="id", page_size=100,
                                         target_operation="HeroEmployees", variables={})
        reader.plan.request.variables = {}
        reader.employee_directory = True
        self.transport.query.side_effect = lambda *_: {"company":{"partners":list(reversed(self.rows))}}
        checkpoint = {}
        result = []
        for _ in range(3):
            page = reader.inventory_page(checkpoint, 100, 1)
            self.assertEqual(page.checkpoint["source_rows_read"], 213)
            self.assertLessEqual(len(page.rows), 100)
            result.extend(row["id"] for row in page.rows)
            checkpoint = page.checkpoint
        self.assertEqual(result, list(range(1,214)))
        self.assertIsNone(page.next_offset)
        self.transport.query.side_effect = lambda *_: {"company":{"partners":[{"id":1},{"id":1}]}}
        with self.assertRaisesRegex(Exception, "duplicate"):
            reader.inventory_page({}, 100, 1)

    def test_shifted_overlap_requires_a_fresh_pass(self):
        from hero_integration.services.offset_pagination import InventoryWindowMoved
        reader = self.reader()
        first = reader.inventory_page({}, 10, 3)
        del self.rows[0]
        with self.assertRaises(InventoryWindowMoved):
            reader.inventory_page(first.checkpoint, 10, 3)
        first = reader.inventory_page({}, 10, 3)
        self.rows.insert(0, {"id": 0})
        with self.assertRaises(InventoryWindowMoved):
            reader.inventory_page(first.checkpoint, 10, 3)

    def test_whole_repeated_window_after_insertions_recovers_and_reaches_tail(self):
        from hero_integration.services.offset_pagination import InventoryWindowMoved

        for overlap in (0, 3):
            with self.subTest(overlap=overlap):
                reader = self.reader()
                # Leave space for stable source IDs to be inserted before this page.
                self.rows = [{"id": key} for key in range(100, 125)]
                first = reader.inventory_page({}, 10, overlap)
                self.rows[:0] = [{"id": key} for key in range(90 + overlap, 100)]
                with self.assertRaises(InventoryWindowMoved):
                    reader.inventory_page(first.checkpoint, 10, overlap)
                checkpoint, seen = {}, []
                for _ in range(10):
                    page = reader.inventory_page(checkpoint, 10, overlap)
                    seen.extend(row["id"] for row in page.rows)
                    if page.next_offset is None:
                        break
                    checkpoint = page.checkpoint
                self.assertIsNone(page.next_offset)
                self.assertEqual(seen, [row["id"] for row in self.rows])

    def test_ignored_offset_and_unordered_or_unbounded_reader_are_rejected(self):
        reader = self.reader()
        page = reader.inventory_page({}, 10, 0)
        self.transport.query.side_effect = lambda *_: {"contacts": self.rows[:10]}
        with self.assertRaises(ValueError):
            reader.inventory_page(page.checkpoint, 10, 0)
        self.rows.reverse()
        with self.assertRaises(ValueError):
            reader.inventory_page({}, 10, 0)
        reader = self.reader("query R($first:Int!,$offset:Int!){contacts(first:$first,offset:$offset){id}}")
        self.assertFalse(reader.inventory_arguments())
        self.transport.query.assert_not_called()


class TestWritebackCoverage(TestCase):
    def test_held_mapping_does_not_block_an_independent_mapping(self):
        from hero_integration import api
        from hero_integration.services import automatic_writeback as worker

        event = NS(name="observation", technical_action="frappe_change_observed", changed_fields="notes, title")
        site = Mock()
        site.db.get_value.return_value = None
        profiles = [("notes-mapping", {"notes"}, False), ("title-mapping", {"title"}, False)]
        with patch.object(api, "_event_action_key"), patch.object(api, "sync_hero_event_preview", side_effect=[
                {"conflicts": [{"field": "notes"}], "can_send": False},
                {"can_send": True, "fingerprint": "title-proof"},
        ]), patch.object(api, "sync_hero_event_execute", return_value={"status": "Confirmed"}) as execute:
            self.assertEqual(worker._deliver(site, NS(generation=1), event, profiles)[0], "Deferred")
            execute.assert_called_once_with(event.name, "title-mapping", "title-proof", confirmed=True,
                                            expected_plan_generation=1)

    def test_uncertain_receipt_is_reconciled_before_any_other_mapping(self):
        from hero_integration import api
        from hero_integration.services import automatic_writeback as worker

        event = NS(name="observation", technical_action="frappe_change_observed", changed_fields="notes, title")
        site = Mock()
        site.db.get_value.return_value = NS(delivery_stage="Uncertain", correlation_id="first-receipt")
        profiles = [("notes-mapping", {"notes"}, False), ("title-mapping", {"title"}, False)]
        with patch.object(api, "_event_action_key"), patch.object(api, "reconcile_bound_writeback",
                return_value={"status": "Uncertain"}) as readback, patch.object(api, "sync_hero_event_preview") as preview:
            self.assertEqual(worker._deliver(site, NS(generation=1), event, profiles)[0], "Deferred")
            readback.assert_called_once_with("first-receipt")
            preview.assert_not_called()

    def test_later_uncertain_profile_is_read_back_before_new_work(self):
        from hero_integration import api
        from hero_integration.services import automatic_writeback as worker

        event = NS(name="observation", technical_action="frappe_change_observed", changed_fields="notes, title")
        site = Mock()
        later = NS(delivery_stage="Uncertain", correlation_id="later-receipt", intent_json="{}")
        site.db.get_value.side_effect = lambda _table, key, *args, **kwargs: (
            later if key["event_action_key"] == "title-mapping" else None
        )
        profiles = [("notes-mapping", {"notes"}, False), ("title-mapping", {"title"}, False)]
        with patch.object(api, "_event_action_key", side_effect=lambda _event, mapping: mapping), \
                patch.object(api, "reconcile_bound_writeback", return_value={"status": "Uncertain"}) as readback, \
                patch.object(api, "sync_hero_event_preview", return_value={"can_send": True, "fingerprint": "notes-proof"}) as preview, \
                patch.object(api, "sync_hero_event_execute", side_effect=RuntimeError("Object already has a pending receipt")) as send:
            self.assertEqual(worker._deliver(site, NS(generation=1), event, profiles)[0], "Deferred")
            readback.assert_called_once_with("later-receipt")
            preview.assert_not_called()
            send.assert_not_called()

    def test_due_events_are_rechecked_and_attempts_read_under_the_lease(self):
        from contextlib import nullcontext
        from datetime import datetime, timedelta
        from sys import modules
        from hero_integration.services import automatic_writeback as worker

        site = Mock()
        now = datetime(2026, 11, 27, 2, 0)
        site.utils.now_datetime.return_value = now
        site.get_single.return_value = NS(testing_enabled=False)
        rows = {
            "delayed": NS(outbound_status="Deferred", outbound_attempts=3, outbound_retry_on=now + timedelta(minutes=1)),
            "due": NS(outbound_status="Deferred", outbound_attempts=4, outbound_retry_on=now),
        }
        def read_state(doctype, name, fields, **kwargs):
            row = rows.get(name)
            return getattr(row, fields, None) if isinstance(fields, str) else row
        site.db.get_value.side_effect = read_state
        events = [NS(name=name, target_doctype="Task", outbound_attempts=1) for name in ("delayed", "removed", "due")]
        profiles = {"Task": [("mapping", {"title"}, False)]}
        plan = NS()
        ledger = Mock()
        ledger.execution_lease.side_effect = lambda _: nullcontext()
        with patch.dict(modules, {"frappe": site}), patch.object(worker, "current_plan", return_value=plan), \
                patch.object(worker, "_profiles", return_value=profiles), patch.object(worker, "_due_events", return_value=events), \
                patch.object(worker, "_deliver", return_value=("Deferred", "Waiting")) as deliver, \
                patch("hero_integration.services.polling.runtime_plan", return_value={"page_size": 10, "retry_backoff_seconds": 2}), \
                patch("hero_integration.services.sync_engine.FrappeSyncLedger", return_value=ledger):
            worker.process_pending("hero", 1)
        deliver.assert_called_once_with(site, plan, events[-1], profiles["Task"])
        site.db.set_value.assert_called_once_with(worker.RECORD, "due", {
            "outbound_status": "Deferred", "outbound_detail": "Waiting", "outbound_attempts": 5,
            "outbound_retry_on": now + timedelta(seconds=32),
        }, update_modified=False)

    def test_independent_direct_area_does_not_inherit_an_unrelated_core_switch(self):
        from hero_integration.services.sync_runtime_adapter import RuntimeBridgeError, _require_area_enabled
        from hero_integration.services.file_migration import DIRECT_OPERATIONS
        selection = NS(graphql_mapping="approved-map")
        settings = NS(sync_area_tasks=0)
        site = Mock()
        with patch("hero_integration.services.file_migration.pictures_enabled", return_value=False):
            for operation in DIRECT_OPERATIONS.values():
                site.db.get_value.return_value = json.dumps({"target_operation":operation})
                if operation == "HeroPictures":
                    with self.assertRaises(RuntimeBridgeError):
                        _require_area_enabled(site, settings, selection)
                else:
                    _require_area_enabled(site, settings, selection)
            for operation in ("HeroTasks", "Unknown"):
                site.db.get_value.return_value = json.dumps({"target_operation":operation})
                with self.assertRaises(RuntimeBridgeError):
                    _require_area_enabled(site, settings, selection)

    def test_product_field_evidence_distinguishes_falsy_native_values(self):
        from hero_integration.services.product_migration import owned_field_digests
        seen = [owned_field_digests({"value": value}, ("value",))["value"] for value in (None, "", False, 0)]
        self.assertEqual(len(set(seen)), 4)

    def test_only_an_explicit_present_empty_can_establish_readback(self):
        from hero_integration.services.binding_registry import BindingConflict, native_fingerprint
        from hero_integration.services.execution_runtime import acknowledge_outbound_field
        doc = NS(meta=NS(get_field=lambda field: NS(fieldtype="Data")))
        binding = NS(target_field="details", revision=2, mapping_revision=3, mapping_definition_id="details", acknowledged_version=4)
        registry = Mock()
        rule = NS(source_path="child.details", transformation="Trim")
        with patch("hero_integration.services.sync_engine.FrappeBoundProcessor.project_scalar", return_value=None):
            for null_policy, value in (("EXPLICIT_NULL", None), ("EMPTY_STRING", "")):
                acknowledge_outbound_field(registry, None, rule, doc, {"child": {"details": value}},
                    "test", binding, value, "receipt", null_policy=null_policy)
                self.assertEqual(registry.record_verified_acknowledgment.call_args.kwargs["source_fingerprint"],
                                 native_fingerprint(value, "Data"))
                for missing in ({}, {"child": {}}, {"child": None}):
                    with self.assertRaises(BindingConflict):
                        acknowledge_outbound_field(registry, None, rule, doc, missing, "test", binding,
                                                   value, "receipt", null_policy=null_policy)
            with self.assertRaises(BindingConflict):
                acknowledge_outbound_field(registry, None, rule, doc, {"child": {"details": None}}, "test", binding, None, "receipt")
        self.assertEqual(registry.record_verified_acknowledgment.call_count, 2)

    def test_partial_receipt_keeps_only_unsent_changes_pending_under_one_child_key(self):
        from hero_integration.services import automatic_writeback as worker
        from unittest.mock import patch
        event = NS(name="observation", technical_action="frappe_change_observed", changed_fields="notes, title")
        receipt = NS(delivery_stage="Confirmed", intent_json='{"deferred_native_fields":["notes"]}', correlation_id="receipt")
        parent = NS(source_type="Task", source_id="5", source_instance="synthetic", target_doctype="Task",
                    target_name="synthetic-task", correlation_id="binding", native_state_json='{"notes":"digest","title":"other"}')
        site = Mock()
        site.get_doc.return_value = parent
        states = {}
        site.db.get_value.side_effect = lambda table, key, *a, **kw: receipt if table=="HERO Mutation Audit" else states.get(key)
        with patch("hero_integration.api._event_action_key", return_value="observation-mapping"), patch("hero_integration.services.integration_events.record_output") as record, \
                patch("hero_integration.api.sync_hero_event_execute") as execute:
            for _ in range(2):
                self.assertEqual(worker._deliver(site, NS(generation=1), event, [("mapping", {"notes","title"}, False)])[0], "Deferred")
            calls = [c.kwargs for c in record.call_args_list]
            self.assertEqual(calls[0], calls[1])
            self.assertEqual(calls[0]["native_state"], {"notes":"digest"})
            states[calls[0]["event_id"]] = "Delivered"
            self.assertEqual(worker._deliver(site, NS(generation=1), event, [("mapping", {"notes","title"}, False)])[0], "Delivered")
            execute.assert_not_called()

    def test_related_preview_requires_the_current_selected_native_incarnation_and_field(self):
        from hero_integration.services import mapped_update_v2 as update
        from hero_integration.services.binding_registry import native_fingerprint
        from hero_integration.services.change_detection import related_document_key
        doc = Mock(doctype="Address", creation="2026-01-01")
        doc.name="synthetic-address"
        doc.get.return_value="city"
        doc.meta.get_field.return_value=NS(fieldtype="Data")
        item = {"id":"item", "source":{"doctype":"Address", "field":"city"}}
        ctx=NS(proof=None, content={"area":"HeroCustomers","assignments":[item]}, source_documents={"item":doc},hero_id="5")
        prepared=NS(changes=[{"id":"item"}],conflicts=[],held=[],fingerprint="mock",mutation_id=5)
        observed={"Address.city":native_fingerprint("city","Data"),"_related_document":related_document_key(doc)}
        with patch.object(update, "_load_context", return_value=ctx), patch.object(update, "_prepare", return_value=prepared), \
                patch("hero_integration.api._mutation_execution_state",return_value={"mutation_execution_enabled":True}), \
                patch("hero_integration.services.writeback_source.validate_source"):
            def preview():
                return update.preview("mapping","Customer","owner",client=Mock(),related_observation=observed)
            self.assertTrue(preview()["can_send"])
            doc.creation="2026-01-02"
            self.assertTrue(preview()["superseded"])
            doc.creation="2026-01-01"
            doc.get.return_value="newer change"
            self.assertTrue(preview()["superseded"])

    def test_file_name_uses_the_file_baseline_and_exact_document_for_event_delivery(self):
        from hero_integration.services import mapped_update_v2 as update
        from hero_integration.services.change_detection import mapped_observation_hash, related_document_key
        doc = Mock(doctype="File", creation="2026-01-01")
        doc.name = "synthetic-file"
        doc.get.return_value = "renamed.txt"
        doc.meta.get_field.return_value = NS(fieldtype="Data")
        item = {"id":"filename","source":{"doctype":"File","field":"file_name"}}
        ctx = NS(proof=None, parent=NS(), fields={}, content={"area":"HeroDocuments", "assignments":[item]},
                 source_documents={"filename":doc}, hero_id="5")
        prepared = NS(changes=[{"id":"filename"}], conflicts=[], held=[], fingerprint="proof", mutation_id=5)
        obs = {"File.file_name":mapped_observation_hash(doc,"file_name"),"_related_document":related_document_key(doc)}
        with patch.object(update,"_load_context",return_value=ctx), patch.object(update,"_prepare",return_value=prepared), \
                patch("hero_integration.api._mutation_execution_state",return_value={"mutation_execution_enabled":True}), \
                patch("hero_integration.services.writeback_source.validate_source"):
            def preview():
                return update.preview("mapping","File",doc.name,client=Mock(),related_observation=obs)
            self.assertTrue(preview()["can_send"])
            prepared.changes = []
            prepared.conflicts = [{"id":"filename"}]
            self.assertFalse(preview()["can_send"])
            ctx.fields = {"file_name":NS()}
            self.assertTrue(preview()["superseded"])


class TestInventoryTaskPermission(TestCase):
    def page(self, *, reject_review=False, unexpected=False):
        from contextlib import nullcontext
        from hero_integration.services.sync_engine import (
            FrappeBoundProcessor, NativeTaskWriteDenied, SyncEngine, SyncState,
        )
        import frappe

        ledger, provider, index, processor, intake = (Mock() for _ in range(5))
        order = []
        ledger.inventory_record.side_effect = lambda predicate: nullcontext()
        ledger.save_state.side_effect = lambda state, generation: order.append("checkpoint") or state
        intake.file_configuration = None
        intake.origin = None
        intake.reconcile.return_value = True
        def save_review(row, identity, error):
            self.assertEqual((identity, error.target_name, error.fields), ("1", "TASK-A", ("modified_source",)))
            order.append("review")
            if reject_review:
                raise RuntimeError("review persistence failed")
        intake.defer_task_write.side_effect = save_review
        processor.is_inventory_record_error.side_effect = FrappeBoundProcessor(1, (), frappe).is_inventory_record_error
        processor.process_event.side_effect = [
            frappe.PermissionError("read authority lost") if unexpected else NativeTaskWriteDenied("TASK-A", ["modified_source"]),
            True,
        ]
        provider.inventory_page.return_value = NS(rows=[{"id": 1}, {"id": 2}], next_offset=2,
                                                 checkpoint={"next_offset": 2, "count": 2})
        index.fields_for_ids.return_value = [NS(object_id=str(i), binding=NS(mapping_revision=1)) for i in (1, 2)]
        plan = NS(sealed=NS(contract=NS(identity_path="id", adapter_options={})))
        engine = SyncEngine(ledger, plan, provider, index, processor, revalidate=lambda: None, inventory=(2, 0, intake))
        state = SyncState(NS(source_instance="default", mapping_revision=1))
        run = NS(site_scope="test", run_id="run")
        return lambda: engine._inventory_slice(state, run), ledger, processor, order

    def test_rejected_save_retains_work_before_checkpoint_and_processes_next_row(self):
        apply, ledger, processor, order = self.page()
        state, processed, skipped, _ = apply()
        self.assertEqual((state.inventory_checkpoint["next_offset"], processed, skipped), (2, 1, 1))
        self.assertEqual(order, ["review", "checkpoint"])
        self.assertEqual(processor.process_event.call_count, 2)
        processor.record_inventory_gap.assert_called_once()

    def test_review_storage_failure_keeps_previous_checkpoint(self):
        apply, ledger, processor, order = self.page(reject_review=True)
        with self.assertRaisesRegex(RuntimeError, "persistence failed"):
            apply()
        ledger.save_state.assert_not_called()
        self.assertEqual(order, ["review"])

    def test_failed_inventory_page_does_not_count_rolled_back_records_as_processed(self):
        from dataclasses import replace
        from hero_integration.services.sync_engine import SyncEngine, SyncState, SyncStatus

        ledger = Mock()
        ledger.save_state.side_effect = lambda updated, generation: updated
        ledger.progress.side_effect = [None, RuntimeError("Could not persist progress")]
        state = SyncState(NS())
        first = replace(state, generation=1, inventory_checkpoint={"next_offset": 1}, status=SyncStatus.PARTIAL)
        second = replace(first, generation=2, inventory_checkpoint={"next_offset": 3})
        engine = SyncEngine(ledger, NS(), NS(), NS(), NS(), inventory=(2, 0, None), inventory_pages=3)
        run = NS(automatic=True, execution_scope="INVENTORY")
        with patch.object(engine, "_inventory_slice", side_effect=[(first, 1, 0, 1), (second, 2, 0, 2)]):
            result = engine._run_under_lock(run, state, bounded=True)
        self.assertEqual(result.inventory_checkpoint, first.inventory_checkpoint)
        ledger.rollback.assert_called_once()
        ledger.finish.assert_called_once_with(run, SyncStatus.FAILED, 1, 0, 3)

    def test_permission_failure_outside_native_save_fails_page(self):
        import frappe
        apply, ledger, processor, order = self.page(unexpected=True)
        with self.assertRaises(frappe.PermissionError):
            apply()
        ledger.save_state.assert_not_called()
        self.assertEqual(order, [])

    def test_denial_preserves_existing_relationship_review(self):
        from hero_integration.services import deferred_references as refs
        db = Mock()
        db.exists.return_value = False
        previous = [NS(name="assignment", detail="Assignment remains pending."),
                    NS(name="earlier-write", detail=refs.NATIVE_TASK_DENIAL)]
        with patch.object(refs, "frappe") as site, patch.object(refs, "_observations", return_value=previous), \
                patch.object(refs, "dependencies_ready", return_value=True):
            site.db = db
            refs.save("run", "default", "HeroTasks", "1", "revision", {"id": 1}, [],
                      result_code="BLOCKED_RECORD", defer_empty=True, native_task_denial=True)
            self.assertEqual([call.args[1] for call in db.set_value.call_args_list], ["earlier-write"])
            values = site.get_doc.return_value.update.call_args.args[0]
            self.assertEqual((values["detail"], values["status"]), (refs.NATIVE_TASK_DENIAL, "Conflict"))
            self.assertEqual(json.loads(values["record_json"]), {"id": 1})


class TestNullableInventoryFields(TestCase):
    def test_native_project_identity_can_be_explicitly_absent(self):
        from hero_integration.services.sync_engine import FrappeBoundProcessor
        opts = dict(source_type="int|null", purpose="Field")
        for source_path, record in (("project_id", {"project_id": None}), ("project.id", {"project": None})):
            field = NS(source_path=source_path, **opts)
            for local, expected in [(None, "UNCHANGED_EMPTY"), ("", "UNCHANGED_EMPTY"),
                                    ("103", "SOURCE_EMPTY_RETAINED"), (0, "SOURCE_EMPTY_RETAINED"),
                                    (False, "SOURCE_EMPTY_RETAINED")]:
                with self.subTest(path=source_path, local=local):
                    self.assertEqual(FrappeBoundProcessor._nullable_source_observation(field, record, local), expected)

    def test_missing_nested_key_and_nonnullable_value_still_require_review(self):
        from hero_integration.services.sync_engine import FrappeBoundProcessor
        field = NS(source_type="int|null", purpose="Field", source_path="project.id")
        for record in ({}, {"project": {}}, {"project": []}, {"project": {"id": ""}}):
            self.assertIsNone(FrappeBoundProcessor._nullable_source_observation(field, record, None))
        field.source_type = "int"
        self.assertIsNone(FrappeBoundProcessor._nullable_source_observation(field, {"project": None}, None))
        field.source_type, field.purpose = "int|null", "Record Name"
        self.assertIsNone(FrappeBoundProcessor._nullable_source_observation(field, {"project": None}, None))
