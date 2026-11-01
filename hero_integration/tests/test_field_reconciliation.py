"""Pure field-wise three-way reconciliation and MISSING/NULL/EMPTY semantics."""
from __future__ import annotations

import unittest

from hero_integration.services.change_detection import EMPTY_HASH, field_hashes, next_baseline, reconcile
from hero_integration.services.direct_mapping import MISSING, lookup, source_value
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

    def test_unwritable_source_clear_is_acknowledged_only(self):
        outcome = reconcile(_h(f="a"), _h(f="a"), _h(f=None), _h(f="a"), tracked={"f"}, source_empty={"f"}, unwritable={"f"})
        self.assertEqual(outcome.acknowledged, {"f"})
        self.assertFalse(outcome.apply)

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


if __name__ == "__main__":
    unittest.main()
