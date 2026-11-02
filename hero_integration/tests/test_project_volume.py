"""HERO volume is the project volume in euros."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from hero_integration.services.direct_mapping import DirectMappingError, MappingSpec, validate_spec
from hero_integration.services.mapping_runtime import MappingPlanError, plan_from_definitions
from hero_integration.sources import hero

RULE = MappingSpec(operation="HeroProjectMatches", source_path="volume", target_doctype="Project",
                   target_field="custom_hero_volume", source_type="float|null", transformation="Identity")


class _Metadata:
    def __init__(self, fieldtype="Currency", read_only=1):
        self.field_ = SimpleNamespace(fieldtype=fieldtype, read_only=read_only, is_custom_field=True, options="")

    def field(self, doctype, fieldname):
        return self.field_


class TestProjectVolume(unittest.TestCase):
    def test_contract_v3_reads_volume_and_older_versions_are_unchanged(self):
        self.assertIn("    volume\n", hero.project_query("v3"))
        self.assertNotIn("volume", hero.project_query("v2"))
        self.assertNotIn("volume", hero.project_query("v1"))
        self.assertEqual(hero.project_query("v3").replace("    volume\n", ""), hero.project_query("v2"))

    def test_volume_maps_only_to_a_read_only_currency_field(self):
        validate_spec(RULE, _Metadata())
        with self.assertRaises(DirectMappingError):
            validate_spec(RULE, _Metadata(fieldtype="Data"))
        with self.assertRaises(DirectMappingError):
            validate_spec(MappingSpec(**{**RULE.__dict__, "source_type": "string|null"}), _Metadata())

    def test_numbers_are_accepted_and_text_or_booleans_are_refused(self):
        from hero_integration.tests.mapping_fixture import fixture_payload
        from hero_integration.tests.test_project_company_migration import _project_title_inputs

        rules = [MappingSpec(**{key: item[key] for key in MappingSpec.__dataclass_fields__})
                 for item in fixture_payload()["mappings"] if item["operation"] == "HeroProjectMatches"]
        plan = plan_from_definitions("HeroProjectMatches", [*rules, RULE])
        values = lambda volume: plan.values(
            _project_title_inputs(id=1, customer_id=101, volume=volume),
            lambda customer: "CUST", lambda: "Company", lambda record: "Title")
        for volume in (12500.5, 9000, None):
            self.assertEqual(values(volume).get("custom_hero_volume"), volume)
        # Whole numbers arrive as int but are stored and read back as float.
        self.assertIs(type(values(35713).get("custom_hero_volume")), float)
        for volume in ("12500", True):
            with self.assertRaises(MappingPlanError):
                values(volume)


if __name__ == "__main__":
    unittest.main()
