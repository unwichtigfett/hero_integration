"""Pure field-wise three-way reconciliation and MISSING/NULL/EMPTY semantics."""
from __future__ import annotations

import unittest
import sys
import types
from unittest.mock import Mock, patch

from hero_integration.services.change_detection import (
    EMPTY_HASH, encode, field_hashes, managed_target_update, managed_target_update_active, next_baseline,
    observe_frappe_target_change, protect_external_task_deadline, reconcile,
)
from hero_integration.services.direct_mapping import MISSING, MappingSpec, lookup, source_value, transformed_value
from hero_integration.tests.mapping_fixture import fixture_plan


def _h(**values):
    return field_hashes(values)


class TestReconcile(unittest.TestCase):
    def _classify(self, base, source, target, **kwargs):
        return reconcile(_h(f=base), _h(f=base), _h(f=source), _h(f=target), tracked={"f"}, **kwargs)

    def test_five_outcomes(self):
        self.assertEqual(self._classify("a", "a", "a").unchanged, {"f"})
        self.assertEqual(self._classify("a", "b", "a").apply, {"f"})
        self.assertEqual(self._classify("a", "a", "b").local, {"f"})
        self.assertEqual(self._classify("a", "b", "c").conflicts, {"f"})
        self.assertEqual(self._classify("a", "b", "b").converged, {"f"})

    def test_missing_fields_are_carried_forward(self):
        outcome = reconcile(_h(f="a"), _h(f="a"), {}, _h(f="z"), tracked={"f"}, missing={"f"})
        self.assertFalse(outcome.apply | outcome.local | outcome.conflicts)
        self.assertEqual(next_baseline(_h(f="a"), {}, outcome, tracked={"f"}), _h(f="a"))

    def test_local_only_change_never_advances_its_baseline(self):
        prior = _h(f="a", g="x")
        outcome = reconcile(prior, prior, _h(f="b", g="x"), _h(f="a", g="local"), tracked={"f", "g"})
        self.assertEqual((outcome.apply, outcome.local), ({"f"}, {"g"}))
        target = next_baseline(prior, _h(f="b", g="local"), outcome, tracked={"f", "g"})
        self.assertEqual(target["g"], prior["g"])
        self.assertEqual(target["f"], _h(f="b")["f"])

    def test_conflict_and_missing_keep_both_baselines_when_another_field_advances(self):
        prior = _h(apply="old", conflict="old", missing="old")
        source = _h(apply="new", conflict="remote")
        target = _h(apply="old", conflict="local", missing="local")
        tracked = set(prior)

        outcome = reconcile(prior, prior, source, target, tracked=tracked, missing={"missing"})

        self.assertEqual(outcome.apply, {"apply"})
        self.assertEqual(outcome.conflicts, {"conflict"})
        self.assertTrue(outcome.advances_baseline)
        for current in (source, target):
            baseline = next_baseline(prior, current, outcome, tracked=tracked)
            self.assertEqual(baseline["conflict"], prior["conflict"])
            self.assertEqual(baseline["missing"], prior["missing"])
        self.assertEqual(next_baseline(prior, source, outcome, tracked=tracked)["apply"], source["apply"])

    def test_unwritable_source_clear_is_acknowledged_only(self):
        outcome = reconcile(_h(f="a"), _h(f="a"), _h(f=None), _h(f="a"), tracked={"f"}, source_empty={"f"}, unwritable={"f"})
        self.assertEqual(outcome.acknowledged, {"f"})
        self.assertFalse(outcome.apply)

    def test_false_check_value_is_a_change_not_a_null_clear(self):
        prior = _h(f=True)
        source = _h(f=False)
        target = _h(f=1)
        outcome = reconcile(prior, prior, source, target, tracked={"f"})
        self.assertEqual(outcome.apply, {"f"})
        self.assertFalse(outcome.acknowledged | outcome.conflicts)
        self.assertEqual(next_baseline(prior, source, outcome, tracked={"f"}), source)

    def test_source_clear_conflicts_with_an_independent_local_change(self):
        prior = _h(f="original")
        outcome = reconcile(prior, prior, _h(f=None), _h(f="local"), tracked={"f"},
                            source_empty={"f"}, unwritable={"f"})
        self.assertEqual(outcome.conflicts, {"f"})
        self.assertFalse(outcome.apply | outcome.acknowledged)
        self.assertFalse(outcome.advances_baseline)
        self.assertEqual(next_baseline(prior, _h(f=None), outcome, tracked={"f"}), prior)
        self.assertEqual(next_baseline(prior, _h(f="local"), outcome, tracked={"f"}), prior)

    def test_newly_tracked_field_without_baseline(self):
        # Empty Frappe value: a new HERO value applies.
        outcome = reconcile({}, {}, _h(f="v"), _h(f=None), tracked={"f"}, target_empty={"f"})
        self.assertEqual(outcome.apply, {"f"})
        # Unknown non-empty local value that differs: review, not overwrite.
        outcome = reconcile({}, {}, _h(f="v"), _h(f="local"), tracked={"f"})
        self.assertEqual(outcome.conflicts, {"f"})
        # One-sided historical baseline (relation snapshot) stands in for both.
        outcome = reconcile({}, _h(f="p"), _h(f="p"), _h(f="p"), tracked={"f"})
        self.assertEqual(outcome.unchanged, {"f"})
        self.assertEqual(EMPTY_HASH, _h(f=None)["f"])


class TestMissingNullEmpty(unittest.TestCase):
    def test_json_value_transform_is_canonical_and_rejects_nonfinite_numbers(self):
        spec = MappingSpec("HeroTasks", "value", "Task", "custom_value", "string|null",
                           transformation="JSON Value")
        self.assertEqual(transformed_value(spec, {"z": [False, None], "a": "ä"}),
                         '{"a":"ä","z":[false,null]}')
        self.assertEqual(transformed_value(spec, {"a": "ä", "z": [False, None]}),
                         transformed_value(spec, {"z": [False, None], "a": "ä"}))
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                transformed_value(spec, {"nested": [value]})

    def test_lookup_distinguishes_missing_null_and_values(self):
        record = {"a": {"b": None, "zero": 0, "no": False, "empty": "", "items": []}, "parent": None}
        self.assertIs(lookup(record, "a.missing"), MISSING)
        self.assertIs(lookup(record, "x.y"), MISSING)
        self.assertIs(lookup({"a": "scalar"}, "a.b"), MISSING)
        self.assertIsNone(lookup(record, "a.b"))
        self.assertIsNone(lookup(record, "parent.child"))  # null object nulls its selections
        self.assertEqual(lookup(record, "a.zero"), 0)
        self.assertIs(lookup(record, "a.no"), False)
        self.assertEqual(lookup(record, "a.empty"), "")
        self.assertEqual(lookup(record, "a.items"), [])
        # The compatible public reader keeps its historical None result.
        self.assertIsNone(source_value(record, "a.missing"))

    def test_plan_reports_missing_and_empty_targets_separately(self):
        plan = fixture_plan("HeroCustomers")
        values = plan.values({"id": 1, "full_name": "Ada", "company_name": None, "email": "", "category": "Gold"})
        self.assertEqual(values["custom_hero_customer_category"], "Gold")
        self.assertEqual(values["customer_type"], "Individual")
        self.assertIn("email_id", values.empty)
        self.assertNotIn("email_id", values)
        self.assertIn("custom_hero_customer_number", values.missing)
        partial = plan.values({"id": 1, "full_name": "Ada"})
        self.assertNotIn("customer_type", partial)
        self.assertIn("customer_type", partial.missing)


class TestNewNumericFieldDefault(unittest.TestCase):
    """A new Currency/Float field starts at 0 in ERPNext; that is no local change (2026-11-02)."""

    def test_zero_default_of_a_new_field_takes_the_hero_value(self):
        from hero_integration.services.migration import _Comparison
        comparison = _Comparison({"custom_hero_volume": 12500.0}, None)
        outcome = comparison.reconcile({}, {}, {"custom_hero_volume": 0.0})
        self.assertEqual((set(outcome.apply), set(outcome.conflicts)), ({"custom_hero_volume"}, set()))

    def test_a_real_local_zero_with_a_baseline_is_still_a_local_change(self):
        from hero_integration.services.change_detection import field_hashes
        from hero_integration.services.migration import _Comparison
        base = field_hashes({"custom_hero_volume": 9000.0})
        comparison = _Comparison({"custom_hero_volume": 9000.0}, None)
        outcome = comparison.reconcile(base, base, {"custom_hero_volume": 0.0})
        self.assertEqual(set(outcome.local), {"custom_hero_volume"})


class TestEmptyFloatStoredAsZero(unittest.TestCase):
    """Frappe stores an empty Currency/Float as 0.0; that equals an empty HERO value (2026-11-02)."""

    def test_stored_zero_for_an_empty_hero_value_is_no_local_change(self):
        from types import SimpleNamespace
        from hero_integration.services.change_detection import field_hashes
        from hero_integration.services.migration import _Comparison
        base = field_hashes({"custom_hero_volume": None})
        comparison = _Comparison({"custom_hero_volume": None}, None)
        target = SimpleNamespace(get=lambda field: 0.0)
        current = comparison.target_values(target, lambda doc: {"custom_hero_volume": 0.0})
        self.assertIsNone(current["custom_hero_volume"])
        outcome = comparison.reconcile(base, base, current)
        self.assertEqual((set(outcome.local), set(outcome.conflicts)), (set(), set()))

    def test_integer_zero_and_false_are_kept(self):
        from types import SimpleNamespace
        from hero_integration.services.migration import _Comparison
        comparison = _Comparison({"all_day": 0, "flag": False}, None)
        current = comparison.target_values(SimpleNamespace(get=lambda f: None), lambda doc: {"all_day": 0, "flag": False})
        self.assertEqual(current, {"all_day": 0, "flag": False})


class TestFloatZeroBothSides(unittest.TestCase):
    """HERO volume 0 with an old integer baseline converged instead of conflicting (2026-11-02)."""

    def test_old_integer_zero_baseline_converges(self):
        from types import SimpleNamespace
        from hero_integration.services.change_detection import field_hashes
        from hero_integration.services.migration import _Comparison
        base = field_hashes({"custom_hero_volume": 0})  # recorded before float normalization
        comparison = _Comparison({"custom_hero_volume": 0.0}, None)
        current = comparison.target_values(SimpleNamespace(get=lambda f: 0.0), lambda doc: {"custom_hero_volume": 0.0})
        outcome = comparison.reconcile(base, base, current)
        self.assertEqual((set(outcome.converged), set(outcome.conflicts)), ({"custom_hero_volume"}, set()))

    def test_after_convergence_the_field_stays_quiet(self):
        from types import SimpleNamespace
        from hero_integration.services.change_detection import field_hashes
        from hero_integration.services.migration import _Comparison
        base = field_hashes({"custom_hero_volume": None})
        comparison = _Comparison({"custom_hero_volume": 0.0}, None)
        current = comparison.target_values(SimpleNamespace(get=lambda f: 0.0), lambda doc: {"custom_hero_volume": 0.0})
        outcome = comparison.reconcile(base, base, current)
        self.assertEqual(set(outcome.unchanged), {"custom_hero_volume"})

class TestManagedTargetOwnership(unittest.TestCase):
    def test_nested_ownership_is_restored_after_failure_and_does_not_leak(self):
        self.assertFalse(managed_target_update_active("Task", "outer"))
        with managed_target_update("Task", "outer", "link-outer"):
            self.assertTrue(managed_target_update_active("Task", "outer"))
            self.assertFalse(managed_target_update_active("Task", "other"))
            with self.assertRaisesRegex(ValueError, "interrupted"):
                with managed_target_update("Task", "inner", "link-inner"):
                    self.assertTrue(managed_target_update_active("Task", "inner"))
                    self.assertFalse(managed_target_update_active("Task", "outer"))
                    raise ValueError("interrupted")
            self.assertTrue(managed_target_update_active("Task", "outer"))
            self.assertFalse(managed_target_update_active("Task", "inner"))
        self.assertFalse(managed_target_update_active("Task", "outer"))

    def test_external_deadline_change_requires_managed_task_context(self):
        frappe = types.ModuleType("frappe")
        frappe.flags = types.SimpleNamespace(in_migrate=False)
        frappe.PermissionError = PermissionError
        def fail(message, error):
            raise error(message)
        frappe.throw = fail
        utils = types.ModuleType("frappe.utils")
        utils.get_datetime = lambda value: value
        doc = types.SimpleNamespace(name="TASK-1", get=lambda key: "new-deadline",
                                    get_doc_before_save=lambda: {"custom_hero_due_date": "old-deadline"})

        with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils}):
            with self.assertRaisesRegex(PermissionError, "managed by HERO Integration"):
                protect_external_task_deadline(doc)
            with managed_target_update("Task", "TASK-1", "link-1"):
                protect_external_task_deadline(doc)
            with self.assertRaises(PermissionError):
                protect_external_task_deadline(doc)


class TestGlobalObserverLifecycle(unittest.TestCase):
    def test_install_and_uninstall_guards_return_before_link_query(self):
        frappe = types.ModuleType("frappe")
        frappe.get_all = Mock(side_effect=AssertionError("link query during schema transition"))
        utils = types.ModuleType("frappe.utils")
        utils.now_datetime = Mock()
        with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils}):
            for migrating, uninstalling in ((True, False), (False, True)):
                frappe.flags = types.SimpleNamespace(in_migrate=migrating, in_uninstall=uninstalling)
                with self.subTest(migrating=migrating, uninstalling=uninstalling):
                    observe_frappe_target_change(types.SimpleNamespace(doctype="Customer", name="synthetic"))
        frappe.get_all.assert_not_called()
        utils.now_datetime.assert_not_called()

    def test_inbound_owner_is_skipped_but_other_link_observes_local_change(self):
        class Row(dict):
            __getattr__ = dict.__getitem__

        baseline = encode(_h(customer_name="old"))
        rows = [Row(name=name, source_type="customer", source_id=name,
                    target_field_hashes=baseline, observed_target_hash="")
                for name in ("owner", "other")]
        saved_link = types.SimpleNamespace(save=Mock())
        frappe = types.ModuleType("frappe")
        frappe.flags = types.SimpleNamespace(in_migrate=False, in_uninstall=False)
        frappe.get_all = Mock(return_value=rows)
        frappe.get_doc = Mock(return_value=saved_link)
        utils = types.ModuleType("frappe.utils")
        utils.now_datetime = Mock(return_value="synthetic time")
        drafts = types.ModuleType("hero_integration.services.frappe_mapping_draft_storage")
        drafts.personal_publication_active = Mock(return_value=False)
        events = types.ModuleType("hero_integration.services.integration_events")
        events.record_output = Mock()
        doc = types.SimpleNamespace(doctype="Customer", name="customer-1", get=lambda key: "new")
        modules = {"frappe": frappe, "frappe.utils": utils, drafts.__name__: drafts, events.__name__: events}

        with patch.dict(sys.modules, modules), managed_target_update("Customer", "customer-1", "owner"):
            observe_frappe_target_change(doc)

        frappe.get_doc.assert_called_once_with("HERO Integration Link", "other")
        saved_link.save.assert_called_once_with(ignore_permissions=True)
        self.assertEqual(saved_link.last_observed_on, "synthetic time")
        self.assertEqual(rows[0]["target_field_hashes"], baseline)
        self.assertEqual(rows[1]["target_field_hashes"], baseline)
        events.record_output.assert_called_once()
        self.assertEqual(events.record_output.call_args.kwargs["source_id"], "other")
        self.assertEqual(events.record_output.call_args.kwargs["changed_fields"], ["customer_name"])


if __name__ == "__main__":
    unittest.main()
