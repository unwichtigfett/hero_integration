"""No activation: lock, authorization, and revision regressions for shared drafts."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration import api
from hero_integration.tests.test_frontend_api_boundary import _Frappe, _Mapping
from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping as controller


class TestSharedWritebackCAS(unittest.TestCase):
    def setUp(self):
        mocked = patch.object(api, '_lock_mapping_save_context', return_value={})
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_existing_noop_is_locked_and_own_draft_preserved(self):
        from hero_integration.services import frappe_writeback
        mapping = _Mapping()
        mapping.direction, mapping.operation_kind, mapping.operation_name = "FRAPPE_TO_MUTATION", "MUTATION", "update"
        value = {"source": {"doctype": "Customer", "field": "customer_name"}, "target": {"operation": "update"}}
        mapping.mapping_json = json.dumps(value)
        fake = _Frappe(mapping=mapping)
        with patch.object(fake, "get_doc", wraps=fake.get_doc) as get_doc, patch.object(api, "frappe", fake), \
             patch.object(api, "_validated_snapshot", return_value=(None, {})), patch.object(frappe_writeback, "normalise_mapping", return_value=value):
            result = api.graphql_mapping_save("SNAPSHOT", "MUTATION", "update", "FRAPPE_TO_MUTATION",
                                              mapping.mapping_json, mapping_name=mapping.name, expected_revision=3)
        get_doc.assert_called_once_with("HERO GraphQL Mapping", mapping.name, for_update=True)
        self.assertTrue(result["unchanged"])
        self.assertEqual(mapping.save_calls, 0)

    def test_foreign_draft_noop_is_rejected_without_save(self):
        from hero_integration.services import frappe_writeback
        mapping = _Mapping(modified_by="other")
        mapping.direction, mapping.operation_kind, mapping.operation_name = "FRAPPE_TO_MUTATION", "MUTATION", "update"
        value = {"source": {"doctype": "Customer", "field": "customer_name"}, "target": {"operation": "update"}}
        mapping.mapping_json = json.dumps(value)
        fake = _Frappe(user="admin", mapping=mapping)
        with patch.object(api, "frappe", fake), patch.object(api, "_validated_snapshot", return_value=(None, {})), \
             patch.object(frappe_writeback, "normalise_mapping", return_value=value):
            with self.assertRaisesRegex(PermissionError, "Foreign legacy mapping drafts"):
                api.graphql_mapping_save("SNAPSHOT", "MUTATION", "update", "FRAPPE_TO_MUTATION",
                                         mapping.mapping_json, mapping_name=mapping.name, expected_revision=3)
        self.assertEqual(mapping.save_calls, 0)

    def test_existing_controller_advances_revision_for_status_and_draft_editor_only(self):
        previous = SimpleNamespace(status="Draft", modified_by="alice", mapping_revision=4, definition_id="id", direction="FRAPPE_TO_MUTATION", get=lambda key: None)
        snapshot = SimpleNamespace(analysis_status="Validated", snapshot_json="{}", schema_fingerprint="fp", source_instance="src")
        fake = SimpleNamespace(only_for=lambda role: None, get_doc=lambda *_: snapshot, session=SimpleNamespace(user="alice"))
        for current_status, editor in (("Disabled", "alice"), ("Draft", "bob")):
            class Document:
                schema_snapshot = "S"
                mapping_json = "{}"
                direction = "QUERY_TO_FRAPPE"
                operation_kind = "QUERY"
                operation_name = "readThings"
                definition_id = "id"
                mapping_revision = 4
                status = current_status
                modified_by = editor
                def is_new(self): return False
                def get(self, key): return getattr(self, key, None)
                def get_doc_before_save(self): return previous
                def has_value_changed(self, field): return False
            doc = Document()
            with self.subTest(status=current_status, editor=editor), patch.object(controller, "frappe", fake), \
                 patch.object(controller, "catalog", return_value=[{"operation_name": "readThings"}]), \
                 patch("hero_integration.services.mapping_draft_access.protect_managed_mapping"):
                controller.HEROGraphQLMapping.validate(doc)
                self.assertEqual(doc.mapping_revision, 5)
                self.assertEqual(doc.status, current_status)

    def test_review_only_edit_is_saved_and_omitted_confidence_preserved(self):
        from hero_integration.services import frappe_writeback
        mapping = _Mapping()
        mapping.direction, mapping.operation_kind, mapping.operation_name = "FRAPPE_TO_MUTATION", "MUTATION", "update"
        value = {"source": {"doctype": "Customer", "field": "customer_name"}, "target": {"operation": "update"}}
        mapping.mapping_json = json.dumps(value)
        mapping.suggestion_reason = "original"
        mapping.confidence = 0.7
        fake = _Frappe(mapping=mapping)
        with patch.object(api, "frappe", fake), patch.object(api, "_validated_snapshot", return_value=(None, {})), \
             patch.object(frappe_writeback, "normalise_mapping", return_value=value):
            result = api.graphql_mapping_save("SNAPSHOT", "MUTATION", "update", "FRAPPE_TO_MUTATION",
                                              mapping.mapping_json, mapping_name=mapping.name, expected_revision=3,
                                              suggestion_reason="reviewed")
        self.assertFalse(result["unchanged"])
        self.assertEqual((mapping.suggestion_reason, mapping.confidence, mapping.status), ("reviewed", 0.7, "Draft"))
        self.assertEqual(mapping.save_calls, 1)

    def test_review_only_edit_on_existing_active_is_rejected_without_status_change(self):
        from hero_integration.services import frappe_writeback
        mapping = _Mapping(status="Active")
        mapping.direction, mapping.operation_kind, mapping.operation_name = "FRAPPE_TO_MUTATION", "MUTATION", "update"
        value = {"source": {"doctype": "Customer", "field": "customer_name"}, "target": {"operation": "update"}}
        mapping.mapping_json = json.dumps(value)
        fake = _Frappe(mapping=mapping)
        with patch.object(api, "frappe", fake), patch.object(api, "_validated_snapshot", return_value=(None, {})), \
             patch.object(frappe_writeback, "normalise_mapping", return_value=value):
            with self.assertRaisesRegex(ValueError, "Review metadata can only be edited on a Draft"):
                api.graphql_mapping_save("SNAPSHOT", "MUTATION", "update", "FRAPPE_TO_MUTATION",
                                         mapping.mapping_json, mapping_name=mapping.name, expected_revision=3,
                                         suggestion_reason="new")
        self.assertEqual((mapping.status, mapping.save_calls), ("Active", 0))

    def test_api_rejects_direction_operation_mismatch_before_snapshot_or_write(self):
        mapping = _Mapping()
        fake = _Frappe(mapping=mapping)
        with patch.object(api, "frappe", fake), patch.object(api, "_lock_mapping_save_context") as lock:
            for direction, kind in (("QUERY_TO_FRAPPE", "MUTATION"), ("FRAPPE_TO_MUTATION", "QUERY")):
                with self.subTest(direction=direction), self.assertRaisesRegex(ValueError, "direction and operation kind must agree"):
                    api.graphql_mapping_save("SNAPSHOT", kind, "operation", direction, "{}")
            lock.assert_not_called()
        self.assertEqual(mapping.save_calls, 0)

    def test_existing_query_cannot_be_repurposed_as_mutation_operation(self):
        mapping = _Mapping()
        before = (mapping.operation_kind, mapping.operation_name, mapping.mapping_json, mapping.mapping_revision)
        with patch.object(api, "frappe", _Frappe(mapping=mapping)):
            with self.assertRaisesRegex(ValueError, "direction and operation kind must agree"):
                api.graphql_mapping_save("SNAPSHOT", "MUTATION", "update", "QUERY_TO_FRAPPE", "{}",
                                         mapping_name=mapping.name, expected_revision=3)
        self.assertEqual((mapping.operation_kind, mapping.operation_name, mapping.mapping_json, mapping.mapping_revision), before)
        self.assertEqual(mapping.save_calls, 0)

    def test_controller_rejects_direction_operation_mismatch(self):
        fake = SimpleNamespace(only_for=lambda role: None, throw=lambda message: (_ for _ in ()).throw(ValueError(message)))
        with patch.object(controller, "frappe", fake):
            for direction, kind in (("QUERY_TO_FRAPPE", "MUTATION"), ("FRAPPE_TO_MUTATION", "QUERY")):
                doc = SimpleNamespace(direction=direction, operation_kind=kind, is_new=lambda: True)
                with self.subTest(direction=direction), self.assertRaisesRegex(ValueError, "direction and operation kind must agree"):
                    controller.HEROGraphQLMapping.validate(doc)

    def test_unknown_submitted_writeback_parameter_is_rejected(self):
        from hero_integration.services import frappe_writeback
        mapping = _Mapping()
        fake = _Frappe(mapping=mapping)
        clean = {"source": {"doctype": "Customer", "field": "customer_name"}, "target": {"operation": "update"}}
        submitted = {**clean, "unknown_option": "must-preserve"}
        with patch.object(api, "frappe", fake), patch.object(api, "_validated_snapshot", return_value=(None, {})), \
             patch.object(frappe_writeback, "normalise_mapping", return_value=clean):
            with self.assertRaisesRegex(ValueError, "parameters that would not be preserved"):
                api.graphql_mapping_save("SNAPSHOT", "MUTATION", "update", "FRAPPE_TO_MUTATION", json.dumps(submitted))
        self.assertEqual(mapping.save_calls, 0)

    def test_unknown_existing_writeback_parameter_is_not_overwritten(self):
        from hero_integration.services import frappe_writeback
        mapping = _Mapping()
        mapping.direction = "FRAPPE_TO_MUTATION"
        mapping.operation_kind = "MUTATION"
        mapping.operation_name = "update"
        clean = {"source": {"doctype": "Customer", "field": "customer_name"}, "target": {"operation": "update"}}
        mapping.mapping_json = json.dumps({**clean, "old_option": "must-preserve"})
        before = mapping.mapping_json
        fake = _Frappe(mapping=mapping)
        with patch.object(api, "frappe", fake), patch.object(api, "_validated_snapshot", return_value=(None, {})), \
             patch.object(frappe_writeback, "normalise_mapping", return_value=clean):
            with self.assertRaisesRegex(ValueError, "Existing writeback mapping contains parameters"):
                api.graphql_mapping_save("SNAPSHOT", "MUTATION", "update", "FRAPPE_TO_MUTATION", json.dumps(clean),
                                         mapping_name=mapping.name, expected_revision=3)
        self.assertEqual(mapping.mapping_json, before)
        self.assertEqual(mapping.save_calls, 0)

    def test_review_metadata_changes_increment_revision_without_status_change(self):
        previous = SimpleNamespace(status="Draft", modified_by="alice", mapping_revision=4, definition_id="id", direction="FRAPPE_TO_MUTATION", get=lambda key: None)
        snapshot = SimpleNamespace(analysis_status="Validated", snapshot_json="{}", schema_fingerprint="fp", source_instance="src")
        fake = SimpleNamespace(only_for=lambda role: None, get_doc=lambda *_: snapshot, session=SimpleNamespace(user="alice"))
        class Document:
            schema_snapshot, mapping_json, direction = "S", "{}", "QUERY_TO_FRAPPE"
            operation_kind, operation_name, definition_id = "QUERY", "readThings", "id"
            mapping_revision, status, modified_by = 4, "Draft", "alice"
            def is_new(self): return False
            def get(self, key): return getattr(self, key, None)
            def get_doc_before_save(self): return previous
            def has_value_changed(self, field): return field == "suggestion_reason"
        doc = Document()
        with patch.object(controller, "frappe", fake), patch.object(controller, "catalog", return_value=[{"operation_name": "readThings"}]), \
             patch("hero_integration.services.mapping_draft_access.protect_managed_mapping"):
            controller.HEROGraphQLMapping.validate(doc)
        self.assertEqual((doc.mapping_revision, doc.status), (5, "Draft"))

    def test_duplicate_input_and_unknown_json_scalars_are_rejected(self):
        from hero_integration.services import frappe_writeback
        mapping = _Mapping()
        fake = _Frappe(mapping=mapping)
        clean = {"source": {"doctype": "Customer", "field": "customer_name"}, "target": {"operation": "update"}}
        duplicate = json.dumps(clean).replace("Customer", "Customer\", \"doctype\": \"Other", 1)
        with patch.object(api, "frappe", fake), patch.object(api, "_validated_snapshot", return_value=(None, {})), \
             patch.object(frappe_writeback, "normalise_mapping", return_value=clean):
            with self.assertRaisesRegex(ValueError, "duplicate JSON object keys"):
                api.graphql_mapping_save("SNAPSHOT", "MUTATION", "update", "FRAPPE_TO_MUTATION", duplicate)
            for value in (None, False, 0, 10 ** 80):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "parameters that would not be preserved"):
                    api.graphql_mapping_save("SNAPSHOT", "MUTATION", "update", "FRAPPE_TO_MUTATION", json.dumps({**clean, "extra": value}))
        self.assertEqual(mapping.save_calls, 0)

    def test_duplicate_existing_writeback_json_is_not_overwritten(self):
        from hero_integration.services import frappe_writeback
        mapping = _Mapping()
        mapping.direction, mapping.operation_kind, mapping.operation_name = "FRAPPE_TO_MUTATION", "MUTATION", "update"
        clean = {"source": {"doctype": "Customer", "field": "customer_name"}, "target": {"operation": "update"}}
        mapping.mapping_json = json.dumps(clean).replace("Customer", "Customer\", \"doctype\": \"Other", 1)
        before = mapping.mapping_json
        fake = _Frappe(mapping=mapping)
        with patch.object(api, "frappe", fake), patch.object(api, "_validated_snapshot", return_value=(None, {})), \
             patch.object(frappe_writeback, "normalise_mapping", return_value=clean):
            with self.assertRaisesRegex(ValueError, "Existing writeback mapping cannot be safely replaced"):
                api.graphql_mapping_save("SNAPSHOT", "MUTATION", "update", "FRAPPE_TO_MUTATION", json.dumps(clean), mapping_name=mapping.name, expected_revision=3)
        self.assertEqual(mapping.mapping_json, before)
        self.assertEqual(mapping.save_calls, 0)

    def test_changed_query_mapping_keeps_legacy_default_review_values(self):
        mapping = _Mapping()
        mapping.suggestion_reason, mapping.confidence = "old", 0.7
        fake = _Frappe(mapping=mapping)
        with patch.object(api, "frappe", fake):
            result = api.graphql_mapping_save("SNAPSHOT", "QUERY", "readThings", "QUERY_TO_FRAPPE", json.dumps({"record_path": "other"}), mapping_name=mapping.name, expected_revision=3)
        self.assertFalse(result["unchanged"])
        self.assertEqual((mapping.suggestion_reason, mapping.confidence), ("", 0))
        self.assertEqual(mapping.save_calls, 1)

    def test_named_direction_changes_are_rejected_both_ways_without_mutation(self):
        from hero_integration.services import frappe_writeback
        writeback = {"source": {"doctype": "Customer", "field": "customer_name"}, "target": {"operation": "update"}}
        for stored_direction, requested_direction in (("FRAPPE_TO_MUTATION", "QUERY_TO_FRAPPE"),
                                                       ("QUERY_TO_FRAPPE", "FRAPPE_TO_MUTATION")):
            mapping = _Mapping()
            mapping.direction = stored_direction
            mapping.mapping_json = json.dumps({"legacy": {"keep": None, "zero": 0}})
            before = (mapping.direction, mapping.mapping_json, mapping.status, mapping.mapping_revision)
            fake = _Frappe(mapping=mapping)
            requested = writeback if requested_direction == "FRAPPE_TO_MUTATION" else {"record_path": "things"}
            with self.subTest(stored=stored_direction), patch.object(api, "frappe", fake), \
                 patch.object(api, "_validated_snapshot", return_value=(None, {})), \
                 patch.object(frappe_writeback, "normalise_mapping", return_value=writeback):
                with self.assertRaisesRegex(ValueError, "cannot change direction"):
                    api.graphql_mapping_save("SNAPSHOT", "MUTATION", "update", requested_direction,
                                             json.dumps(requested), mapping_name=mapping.name, expected_revision=3)
            self.assertEqual((mapping.direction, mapping.mapping_json, mapping.status, mapping.mapping_revision), before)
            self.assertEqual(mapping.save_calls, 0)


class TestMappingSaveContext(unittest.TestCase):
    def test_query_api_reads_mapping_after_context_lock_for_cas(self):
        events = []
        mapping = _Mapping(revision=3)
        fake = _Frappe(mapping=mapping)
        original = fake.get_doc
        def get_doc(doctype, name, **kwargs):
            events.append(('mapping', kwargs))
            return original(doctype, name, **kwargs)
        snapshot = SimpleNamespace(source_instance='source', schema_fingerprint='fp')
        with patch.object(fake, 'get_doc', side_effect=get_doc), patch.object(api, 'frappe', fake), \
             patch.object(api, '_validated_snapshot', return_value=(snapshot, {})), \
             patch.object(controller, 'lock_exchange_context', side_effect=lambda *args: events.append(('context', args))):
            api.graphql_mapping_save('SNAPSHOT', 'QUERY', 'readThings', 'QUERY_TO_FRAPPE',
                                     mapping.mapping_json, mapping_name=mapping.name, expected_revision=3)
        self.assertEqual([event[0] for event in events], ['context', 'mapping'])
        self.assertEqual(events[1][1], {'for_update': True})

    def test_mutation_api_acquires_context_before_field_and_row_locks(self):
        from hero_integration.services import frappe_writeback
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as field_controller
        events = []
        mapping = _Mapping(revision=3)
        mapping.direction, mapping.operation_kind, mapping.operation_name = 'FRAPPE_TO_MUTATION', 'MUTATION', 'update'
        value = {'source': {'doctype': 'Customer', 'field': 'customer_name'}, 'target': {'operation': 'update'}}
        mapping.mapping_json = json.dumps(value)
        fake = _Frappe(mapping=mapping)
        fake.db.sql = lambda *args, **kwargs: events.append(('scan',)) or [SimpleNamespace(name=mapping.name, mapping_json=mapping.mapping_json)]
        original = fake.get_doc
        def get_doc(doctype, name, **kwargs):
            events.append(('mapping', kwargs))
            return original(doctype, name, **kwargs)
        snapshot = SimpleNamespace(source_instance='source', schema_fingerprint='fp')
        with patch.object(fake, 'get_doc', side_effect=get_doc), patch.object(api, 'frappe', fake), \
             patch.object(api, '_validated_snapshot', return_value=(snapshot, {})), \
             patch.object(frappe_writeback, 'normalise_mapping', return_value=value), \
             patch.object(controller, 'lock_exchange_context', side_effect=lambda *args: events.append(('context',))), \
             patch.object(field_controller, '_lock_active_target', side_effect=lambda *args: events.append(('field',))):
            api.graphql_mapping_save('SNAPSHOT', 'MUTATION', 'update', 'FRAPPE_TO_MUTATION',
                                     mapping.mapping_json, expected_revision=3)
        self.assertEqual([event[0] for event in events], ['context', 'field', 'scan', 'mapping'])
        self.assertEqual(events[-1][1], {'for_update': True})

    def test_context_is_locked_before_fresh_snapshot_read(self):
        events = []
        snapshot = SimpleNamespace(source_instance='source', schema_fingerprint='fingerprint')
        def read(name, *, for_update=False):
            events.append(('snapshot', for_update))
            return snapshot, {'validated': True}
        def lock(source, name, direction):
            events.append(('context', source, name, direction))
        with patch.object(api, '_validated_snapshot', side_effect=read), patch.object(controller, 'lock_exchange_context', side_effect=lock):
            result = api._lock_mapping_save_context('SNAPSHOT', 'QUERY_TO_FRAPPE')
        self.assertEqual(result, {'validated': True})
        self.assertEqual(events, [('snapshot', False), ('context', 'source', 'SNAPSHOT', 'QUERY_TO_FRAPPE'), ('snapshot', True)])

    def test_context_change_is_rejected_after_lock(self):
        first = SimpleNamespace(source_instance='source', schema_fingerprint='fingerprint')
        changed = SimpleNamespace(source_instance='other', schema_fingerprint='fingerprint')
        fake = SimpleNamespace(throw=lambda message: (_ for _ in ()).throw(ValueError(message)))
        with patch.object(api, 'frappe', fake), patch.object(api, '_validated_snapshot', side_effect=[(first, {}), (changed, {})]), \
             patch.object(controller, 'lock_exchange_context'):
            with self.assertRaisesRegex(ValueError, 'source context changed'):
                api._lock_mapping_save_context('SNAPSHOT', 'QUERY_TO_FRAPPE')
