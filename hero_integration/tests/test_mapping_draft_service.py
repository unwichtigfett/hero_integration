from __future__ import annotations

import unittest

from hero_integration.services.mapping_draft_service import (
    ActiveMapping,
    DraftConflict,
    InMemoryMappingDraftStorage,
    MappingDraftError,
    MappingDraftService,
)


class TestMappingDraftService(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = InMemoryMappingDraftStorage()
        self.active = ActiveMapping(
            site_scope="b9-acceptance-41i.localhost",
            definition_id="mapping.customer.read",
            name="HERO-GQL-MAP-00001",
            revision=3,
            schema_fingerprint="a" * 64,
            content={"record_path": "customers", "selected_paths": ["id"]},
        )
        self.storage.put_active(self.active)
        self.service = MappingDraftService(self.storage)

    def test_new_definition_is_owned_parentless_and_revision_checked(self):
        definition = {'site_scope': 'b9-acceptance-41i.localhost',
            'definition_id': 'a16b15fa-7ca8-4722-b63c-ac60cc46629a',
            'source_instance': 'source-one', 'snapshot_name': 'SNAPSHOT', 'schema_fingerprint': 'a' * 64,
            'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers'}
        owner = 'admin.one@example.invalid'
        content = {'records_path': 'customers', 'identity_path': 'id'}
        draft = self.service.open_new_draft(definition, owner, content)
        self.assertEqual((draft.definition_id, draft.owner, draft.base_active_name, draft.base_active_revision),
                         (definition['definition_id'], owner, '', 0))
        self.assertEqual(draft.content_revision, 1)
        saved = self.service.save(draft.definition_id, owner, 1, 0, {**content, 'page_size': 10}, expected_generation=1)
        reopened = self.service.open_draft(draft.definition_id, owner)
        self.assertEqual(reopened.content, saved.draft.content)
        self.assertEqual(reopened.content_revision, 2)
        with self.assertRaises(DraftConflict):
            self.service.save(draft.definition_id, owner, 1, 0, content, expected_generation=1)
        with self.assertRaises(DraftConflict):
            self.service.open_draft(draft.definition_id, 'foreign@example.invalid')
        self.assertEqual(self.storage.revisions()[0].kind, 'DRAFT_SAVE')

    def test_new_definition_reimport_and_foreign_collision_fail_closed(self):
        definition = {'site_scope': 'b9-acceptance-41i.localhost',
            'definition_id': 'a16b15fa-7ca8-4722-b63c-ac60cc46629a',
            'source_instance': 'source-one', 'snapshot_name': 'SNAPSHOT', 'schema_fingerprint': 'a' * 64,
            'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers'}
        content = {'records_path': 'customers', 'identity_path': 'id'}
        first = self.service.open_new_draft(definition, 'alice@example.invalid', content)
        repeat = self.service.open_new_draft(definition, 'alice@example.invalid', content)
        self.assertEqual(first, repeat)
        self.assertEqual(len(self.storage.revisions()), 1)
        with self.assertRaises(DraftConflict):
            self.service.open_new_draft(definition, 'bob@example.invalid', content)
        with self.assertRaises(DraftConflict):
            self.service.open_new_draft(definition, 'alice@example.invalid', {**content, 'identity_path': 'other'})
        self.assertEqual(len(self.storage.revisions()), 1)
        self.service.discard(first.definition_id, first.owner, 1, 1, confirmed=True)
        reopened = self.service.open_new_draft(definition, first.owner, content)
        self.assertEqual((reopened.generation, reopened.content_revision), (2, 1))
        self.assertEqual(len(self.storage.revisions()), 2)

    def test_new_definition_rejects_invalid_identity_and_direction(self):
        definition = {'site_scope': 'site', 'definition_id': 'HERO-GQL-MAP-00001', 'source_instance': 'source',
            'snapshot_name': 'snapshot', 'schema_fingerprint': 'a' * 64, 'direction': 'QUERY_TO_FRAPPE',
            'operation_kind': 'QUERY', 'operation_name': 'customers'}
        with self.assertRaises(MappingDraftError):
            self.service.open_new_draft(definition, 'owner@example.invalid', {})
        definition['definition_id'] = 'a16b15fa-7ca8-4722-b63c-ac60cc46629a'
        definition['operation_kind'] = 'MUTATION'
        with self.assertRaises(MappingDraftError):
            self.service.open_new_draft(definition, 'owner@example.invalid', {})

    def test_reconciliation_reports_competing_additional_mappings_as_conflict(self):
        draft = self.service.open_draft(self.active.definition_id, 'owner@example.invalid')
        candidate = dict(draft.content, additional_mappings=[{'id': 'mine'}])
        changed = ActiveMapping(self.active.site_scope, self.active.definition_id, self.active.name, 4,
                                self.active.schema_fingerprint,
                                dict(self.active.content, additional_mappings=[{'id': 'theirs'}]))
        self.storage.put_active(changed)
        preview = self.service.reconcile_preview(self.active.definition_id, draft.owner, 0, 4, candidate,
                                                  expected_generation=1, expected_reconciliation_revision=0)
        self.assertTrue(any(item.identifier == 'root:additional_mappings' and item.state == 'CONFLICT'
                            for item in preview.items))

    def test_administrators_receive_separate_open_drafts(self) -> None:
        first = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")
        second = self.service.open_draft(self.active.definition_id, "admin.two@example.invalid")

        self.assertNotEqual(first.key, second.key)
        self.assertEqual(first.owner, "admin.one@example.invalid")
        self.assertEqual(second.owner, "admin.two@example.invalid")
        self.assertEqual(first.base_active_revision, 3)

    def test_noop_does_not_create_revision_or_change_author(self) -> None:
        draft = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")
        saved = self.service.save(
            self.active.definition_id,
            "admin.one@example.invalid",
            expected_draft_revision=draft.content_revision,
            expected_active_revision=3,
            content=self.active.content,
        )

        self.assertTrue(saved.unchanged)
        self.assertEqual(saved.draft.content_revision, 0)
        self.assertEqual(self.storage.revisions(), ())
        self.assertEqual(saved.draft.owner, "admin.one@example.invalid")

    def test_incomplete_structural_content_is_saved_as_a_draft_revision(self) -> None:
        draft = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")
        saved = self.service.save(
            self.active.definition_id,
            "admin.one@example.invalid",
            expected_draft_revision=draft.content_revision,
            expected_active_revision=3,
            content={"source": {"doctype": "Customer"}},
        )

        self.assertFalse(saved.unchanged)
        self.assertEqual(saved.draft.content_revision, 1)
        self.assertEqual(len(self.storage.revisions()), 1)
        self.assertEqual(self.storage.revisions()[0].kind, "DRAFT_SAVE")

    def test_stale_compare_and_swap_preserves_draft_and_history(self) -> None:
        draft = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")
        saved = self.service.save(
            self.active.definition_id, "admin.one@example.invalid", 0, 3, {"record_path": "changed"}
        )
        with self.assertRaises(DraftConflict):
            self.service.save(
                self.active.definition_id, "admin.one@example.invalid", 0, 3, {"record_path": "other"}
            )

        current = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")
        self.assertEqual(current.content_revision, saved.draft.content_revision)
        self.assertEqual(len(self.storage.revisions()), 1)

    def test_discard_preserves_history_and_allows_a_new_generation(self) -> None:
        draft = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")
        saved = self.service.save(
            self.active.definition_id, "admin.one@example.invalid", 0, 3, {"record_path": "changed"}
        )
        discarded = self.service.discard(
            self.active.definition_id, "admin.one@example.invalid", saved.draft.generation, saved.draft.content_revision, confirmed=True
        )
        reopened = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")

        self.assertEqual(discarded.state, "DISCARDED")
        self.assertEqual(reopened.generation, 2)
        self.assertEqual(reopened.content_revision, 0)
        self.assertEqual(len(self.storage.revisions()), 1)
        self.assertEqual(self.storage.active(self.active.definition_id), self.active)

    def test_actor_cannot_discard_another_administrators_draft(self) -> None:
        first = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")
        with self.assertRaises(DraftConflict):
            self.service.discard(self.active.definition_id, "admin.two@example.invalid", first.generation, 0, confirmed=True)

    def test_reconciliation_requires_the_immutable_base_and_explicit_conflict_choice(self) -> None:
        draft = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")
        self.storage.put_active(ActiveMapping(
            self.active.site_scope, self.active.definition_id, self.active.name, 4,
            self.active.schema_fingerprint, {"record_path": "active-customers", "selected_paths": ["id"]},
        ))
        candidate = {"record_path": "draft-customers", "selected_paths": ["id"]}
        preview = self.service.reconcile_preview(
            self.active.definition_id, "admin.one@example.invalid", draft.content_revision, 4,
            candidate, expected_generation=draft.generation, expected_reconciliation_revision=0,
        )
        self.assertEqual(["root:record_path"], [item.identifier for item in preview.conflicts])
        with self.assertRaises(MappingDraftError):
            self.service.reconcile_apply(preview, accepted_suggestions=(), conflict_choices={})
        applied = self.service.reconcile_apply(
            preview, accepted_suggestions=(), conflict_choices={"root:record_path": "DRAFT"},
        )
        self.assertEqual("draft-customers", applied.content["record_path"])
        saved = self.service.save(
            self.active.definition_id, "admin.one@example.invalid", draft.content_revision, 4,
            applied.content, expected_generation=draft.generation,
            expected_reconciliation_revision=0, reconciliation=applied.proof,
        )
        self.assertFalse(saved.unchanged)
        self.assertTrue(saved.reconciliation_updated)
        self.assertEqual(1, saved.draft.content_revision)
        self.assertEqual(1, saved.draft.reconciliation_revision)
        self.assertEqual(1, len(self.storage.revisions()))

    def test_reconciliation_of_a_revision_only_active_change_has_no_content_revision(self) -> None:
        draft = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")
        self.storage.put_active(ActiveMapping(
            self.active.site_scope, self.active.definition_id, self.active.name, 4,
            self.active.schema_fingerprint, self.active.content,
        ))
        preview = self.service.reconcile_preview(
            self.active.definition_id, "admin.one@example.invalid", 0, 4, draft.content,
            expected_generation=1, expected_reconciliation_revision=0,
        )
        self.assertFalse(preview.suggestions)
        self.assertFalse(preview.conflicts)
        applied = self.service.reconcile_apply(preview, accepted_suggestions=(), conflict_choices={})
        saved = self.service.save(
            self.active.definition_id, "admin.one@example.invalid", 0, 4, applied.content,
            expected_generation=1, expected_reconciliation_revision=0, reconciliation=applied.proof,
        )
        self.assertTrue(saved.unchanged)
        self.assertTrue(saved.reconciliation_updated)
        self.assertEqual(0, saved.draft.content_revision)
        self.assertEqual(1, saved.draft.reconciliation_revision)

    def test_save_after_reconciliation_uses_verified_active_baseline_without_replacing_b(self) -> None:
        draft = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")
        changed_active = ActiveMapping(
            self.active.site_scope, self.active.definition_id, self.active.name, 4,
            self.active.schema_fingerprint, {"record_path": "active-customers", "selected_paths": ["id"]},
        )
        self.storage.put_active(changed_active)
        preview = self.service.reconcile_preview(
            self.active.definition_id, "admin.one@example.invalid", 0, 4,
            {"record_path": "draft-customers", "selected_paths": ["id"]},
            expected_generation=1, expected_reconciliation_revision=0,
        )
        applied = self.service.reconcile_apply(
            preview, accepted_suggestions=(), conflict_choices={"root:record_path": "DRAFT"},
        )
        reconciled = self.service.save(
            self.active.definition_id, "admin.one@example.invalid", 0, 4, applied.content,
            expected_generation=1, expected_reconciliation_revision=0, reconciliation=applied.proof,
        )
        later = self.service.save(
            self.active.definition_id, "admin.one@example.invalid", 1, 4,
            {"record_path": "later-draft-customers", "selected_paths": ["id"]},
            expected_generation=1, expected_reconciliation_revision=1,
        )
        self.assertEqual(3, reconciled.draft.base_active_revision)
        self.assertEqual(2, later.draft.content_revision)
        self.assertEqual(1, later.draft.reconciliation_revision)

    def test_active_change_after_preview_rejects_the_reconciliation_proof(self) -> None:
        draft = self.service.open_draft(self.active.definition_id, "admin.one@example.invalid")
        self.storage.put_active(ActiveMapping(
            self.active.site_scope, self.active.definition_id, self.active.name, 4,
            self.active.schema_fingerprint, self.active.content,
        ))
        preview = self.service.reconcile_preview(
            self.active.definition_id, "admin.one@example.invalid", 0, 4, draft.content,
            expected_generation=1, expected_reconciliation_revision=0,
        )
        applied = self.service.reconcile_apply(preview, accepted_suggestions=(), conflict_choices={})
        self.storage.put_active(ActiveMapping(
            self.active.site_scope, self.active.definition_id, self.active.name, 5,
            self.active.schema_fingerprint, self.active.content,
        ))
        with self.assertRaises(DraftConflict):
            self.service.save(
                self.active.definition_id, "admin.one@example.invalid", 0, 5, applied.content,
                expected_generation=1, expected_reconciliation_revision=0, reconciliation=applied.proof,
            )
