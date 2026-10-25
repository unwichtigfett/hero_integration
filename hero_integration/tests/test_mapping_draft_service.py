from __future__ import annotations

import unittest

from hero_integration.services.mapping_draft_service import (
    ActiveMapping,
    DraftConflict,
    InMemoryMappingDraftStorage,
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
