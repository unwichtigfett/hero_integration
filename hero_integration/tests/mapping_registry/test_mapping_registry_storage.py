from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hero_integration.services.mapping_registry_storage import (
    InMemoryMappingRegistryStorage,
    FrappeMappingRegistryStorage,
    MappingRegistryInjectedFailure,
    MappingRegistryRevisionConflict,
)


class TestInMemoryMappingRegistryStorage(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = InMemoryMappingRegistryStorage()

    def test_fresh_registry_is_empty_and_site_scoped(self) -> None:
        self.assertIsNone(self.storage.read_active("one.test"))
        self.assertEqual(self.storage.history("one.test"), ())
        one = self.storage.replace("one.test", None, {"definitions": ["a"]})
        two = self.storage.replace("two.test", None, {"definitions": ["b"]})
        self.assertEqual(one.revision, 1)
        self.assertEqual(two.revision, 1)
        self.assertEqual(self.storage.read_active("one.test").payload["definitions"], ("a",))

    def test_complete_replace_preserves_immutable_history(self) -> None:
        source = {"definitions": ["first"]}
        first = self.storage.replace("site.test", None, source)
        source["definitions"].append("mutated")
        second = self.storage.replace("site.test", first.revision, {"definitions": ["second"]})
        self.assertEqual(second.revision, 2)
        self.assertEqual(
            [item.payload["definitions"] for item in self.storage.history("site.test")],
            [("first",), ("second",)],
        )
        self.assertEqual(self.storage.read_active("site.test").revision, 2)

    def test_revision_conflict_does_not_change_active_version(self) -> None:
        first = self.storage.replace("site.test", None, {"definitions": ["first"]})
        with self.assertRaises(MappingRegistryRevisionConflict):
            self.storage.replace("site.test", None, {"definitions": ["bad"]})
        self.assertEqual(self.storage.read_active("site.test"), first)
        self.assertEqual(len(self.storage.history("site.test")), 1)

    def test_failure_before_or_during_activation_retains_prior_version(self) -> None:
        first = self.storage.replace("site.test", None, {"definitions": ["first"]})
        for phase in ("persist", "activate"):
            self.storage.fail_next(phase)
            with self.assertRaises(MappingRegistryInjectedFailure):
                self.storage.replace("site.test", first.revision, {"definitions": [phase]})
            self.assertEqual(self.storage.read_active("site.test"), first)
            self.assertEqual(self.storage.history("site.test"), (first,))

    def test_concurrent_readers_only_observe_complete_versions(self) -> None:
        first = self.storage.replace("site.test", None, {"definitions": ["one"]})
        seen: list[tuple[int, tuple[str, ...]]] = []
        ready = threading.Event()
        release = threading.Event()

        def reader() -> None:
            ready.set()
            release.wait(timeout=2)
            active = self.storage.read_active("site.test")
            seen.append((active.revision, active.payload["definitions"]))

        worker = threading.Thread(target=reader)
        worker.start()
        self.assertTrue(ready.wait(timeout=2))
        self.storage.replace("site.test", first.revision, {"definitions": ["two"]})
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertIn(seen[0], {(1, ("one",)), (2, ("two",))})

    def test_competing_writers_cannot_both_replace_the_same_revision(self) -> None:
        first = self.storage.replace("site.test", None, {"definitions": ["original"]})
        start = threading.Barrier(2)
        outcomes = []

        def writer(value):
            start.wait(timeout=2)
            try:
                result = self.storage.replace("site.test", first.revision, {"definitions": [value]})
                outcomes.append(("accepted", result.payload["definitions"]))
            except MappingRegistryRevisionConflict:
                outcomes.append(("stale", (value,)))

        workers = [threading.Thread(target=writer, args=(name,)) for name in ("first", "second")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sorted(status for status, _ in outcomes), ["accepted", "stale"])
        self.assertEqual(len(self.storage.history("site.test")), 2)
        self.assertEqual(self.storage.read_active("site.test").payload["definitions"],
                         next(value for status, value in outcomes if status == "accepted"))


class TestFrappeRegistryFailureBoundary(unittest.TestCase):
    def test_failed_version_insert_rolls_back_before_publishing_pointer(self):
        registry = SimpleNamespace(name="registry", active_revision=1, active_version="version-1", save=Mock())
        version = SimpleNamespace(insert=Mock(side_effect=RuntimeError("synthetic insert failure")))
        db = SimpleNamespace(savepoint=Mock(), rollback=Mock(), commit=Mock())
        frappe = SimpleNamespace(local=SimpleNamespace(site="site.test"), session=SimpleNamespace(user="Administrator"),
                                 has_permission=Mock(), db=db, get_doc=Mock(return_value=version))
        storage = FrappeMappingRegistryStorage(frappe)

        with patch.object(storage, "_locked_registry", return_value=registry):
            with self.assertRaisesRegex(RuntimeError, "synthetic insert failure"):
                storage.replace("site.test", 1, {"definitions": []})

        db.savepoint.assert_called_once_with("hero_mapping_registry_v3_replace")
        db.rollback.assert_called_once_with(save_point="hero_mapping_registry_v3_replace")
        db.commit.assert_not_called()
        registry.save.assert_not_called()
        self.assertEqual((registry.active_revision, registry.active_version), (1, "version-1"))
