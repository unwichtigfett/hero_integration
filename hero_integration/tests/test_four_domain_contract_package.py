"""Offline source-contract tests for the reviewed four-domain package.

The JSON fixtures are intentionally test-only.  Installation continues to own
only the nine baseline fields, while schema adoption remains an explicit,
reviewed operation.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from collections import Counter
from unittest.mock import patch

from hero_integration.services import mapping_exchange, schema_provisioning
from hero_integration.tests.mapping_fixture import (
    customer_v3_legal_form_draft,
    four_domain_contract_payload,
)


FINGERPRINT = "fee37bfa55a02f7a4d09b6b902e6600a835c992c8fe6644c358f5e511e025201"


class TestFourDomainContractPackage(unittest.TestCase):
    def test_four_domain_package_has_the_reviewed_inventory_and_no_install_side_effect(self):
        package = four_domain_contract_payload()

        parsed = mapping_exchange._parse_payload(json.dumps(package), FINGERPRINT)
        self.assertEqual(parsed["format_version"], "hero-frappe-mapping-package-v2")
        self.assertEqual(parsed["schema_fingerprint"], FINGERPRINT)
        self.assertEqual(Counter(item["operation"] for item in parsed["mappings"]), {
            "HeroCustomers": 15,
            "HeroProjectMatches": 21,
            "HeroCalendarEvents": 25,
            "HeroTasks": 15,
        })
        mapping_contract = sorted(
            (
                item["definition_id"], item["operation"], item["source_path"], item["source_type"],
                item["target_doctype"], item["target_field"], item["purpose"], item["transformation"],
                item["relation"], item["priority"], item["mapping_revision"], tuple(item["schema_dependencies"]),
            )
            for item in parsed["mappings"]
        )
        self.assertEqual(
            hashlib.sha256(json.dumps(mapping_contract, separators=(",", ":")).encode()).hexdigest(),
            "88bd1dfe6c6f54d41d5564536f12548d7938ea801b5ed2ddf0e7431d7c859162",
        )
        self.assertEqual({key: len(value) for key, value in parsed["schema"].items()}, {
            "custom_doctypes": 3,
            "custom_fields": 35,
            "relations": 4,
        })

        # Project is an installed target, and the remaining links resolve to
        # package-owned custom DocTypes.  This validates the manifest without
        # creating a DocType, Custom Field, mapping, or business record.
        with patch.object(schema_provisioning, "_doctype_exists", return_value=True):
            definitions = schema_provisioning._definitions(parsed["schema"])
        self.assertEqual(len(definitions["relations"]), 4)
        self.assertEqual(
            next(item for item in parsed["mappings"] if item["definition_id"] == "customer-primary-address-line2"),
            {
                "definition_id": "customer-primary-address-line2",
                "operation": "HeroCustomers",
                "source_path": "address.line_2",
                "source_type": "string|null",
                "target_doctype": "Address",
                "target_field": "address_line2",
                "purpose": "Native Binding",
                "transformation": "Trim",
                "relation": "Customer Primary Address",
                "priority": 5,
                "schema_dependencies": [],
                "mapping_revision": 2,
            },
        )

    def test_project_field_types_follow_the_reviewed_source_contract(self):
        package = four_domain_contract_payload()
        fields = {
            item["fieldname"]: item["fieldtype"]
            for item in package["schema"]["custom_fields"]
            if item["doctype"] == "Project"
        }
        self.assertEqual(fields, {
            "custom_hero_observed_status_id": "Data",
            "custom_hero_observed_status_status_code": "Data",
            "custom_hero_observed_status_name": "Small Text",
            "custom_hero_observed_status_short_name": "Small Text",
            "custom_hero_observed_status_created": "Small Text",
            "custom_hero_observed_status_modified": "Small Text",
            "custom_hero_observed_status_step_id": "Data",
            "custom_hero_project_number": "Small Text",
            "custom_hero_project_type": "Small Text",
            "custom_hero_partner_notes": "Small Text",
            "custom_hero_customer_title": "Small Text",
            "custom_hero_customer_full_name": "Small Text",
            "custom_hero_address_full_address": "Small Text",
        })

    def test_m086_v3_is_a_separate_read_only_draft_not_an_active_package_change(self):
        package = customer_v3_legal_form_draft()

        parsed = mapping_exchange._parse_payload(json.dumps(package), FINGERPRINT)
        self.assertEqual(len(parsed["mappings"]), 1)
        field = parsed["schema"]["custom_fields"][0]
        self.assertEqual(field, {
            "id": "field:customer-company-legal-form",
            "doctype": "Customer",
            "fieldname": "custom_hero_company_legal_form",
            "label": "HERO Company Legal Form",
            "fieldtype": "Data",
            "options": "",
            "insert_after": "custom_hero_customer_type",
            "reqd": 0,
            "unique": 0,
            "read_only": 1,
        })
        mapping = parsed["mappings"][0]
        self.assertEqual(mapping["definition_id"], "customer-company-legal-form")
        self.assertEqual(mapping["schema_dependencies"], [field["id"]])


if __name__ == "__main__":
    unittest.main()
