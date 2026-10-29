from __future__ import annotations

import threading
import unittest

from hero_integration.services.mapping_registry_storage import (
    InMemoryMappingRegistryStorage,
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
