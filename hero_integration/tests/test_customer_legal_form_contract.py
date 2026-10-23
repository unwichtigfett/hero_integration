from __future__ import annotations

import unittest
from types import SimpleNamespace

from hero_integration.services.direct_mapping import DirectMappingError, MappingSpec, validate_spec
from hero_integration.services.mapping_runtime import plan_from_definitions
from hero_integration.services.read_contract import ReadContractError, from_documents
from hero_integration.sources.hero import CUSTOMER_QUERY_V2, CUSTOMER_QUERY_V3
from hero_integration.tests.mapping_fixture import fixture_payload


LEGAL_FORM_ID = "customer-company-legal-form"
LINE2_ID = "customer-primary-address-line2"


class _Metadata:
    def ensure_write(self, doctype):
        self.doctype = doctype

    def field(self, doctype, fieldname):
        if doctype == "Customer" and fieldname == "custom_hero_company_legal_form":
            return SimpleNamespace(fieldtype="Data", read_only=1, is_custom_field=True)
        return None


def _spec() -> MappingSpec:
    return MappingSpec(
        "HeroCustomers", "company_legal_form", "Customer", "custom_hero_company_legal_form",
        "string|null", transformation="Trim", definition_id=LEGAL_FORM_ID,
    )


def _service(query):
    return SimpleNamespace(name="SERVICE", status="Validated", execution_status="Approved", approved_service_revision=1,
        approved_mapping_revision=1, service_revision=1, schema_snapshot="S", schema_fingerprint="f" * 64, query_document=query, read_mapping="MAP")


def _mapping(query, ids, version):
    import json
    return SimpleNamespace(name="MAP", status="Active", mapping_revision=1, schema_snapshot="S", schema_fingerprint="f" * 64,
        mapping_json=json.dumps({"target_operation":"HeroCustomers", "records_path":"contacts", "identity_path":"id", "page_size":100,
            "customer_contract_version":version, "mapping_definition_ids":ids,
            "variables":{"first":"$page_size","offset":"$offset","orderBy":"id","showDeleted":True}}))


class TestCustomerLegalFormContract(unittest.TestCase):
    def test_draft_mapping_is_a_read_only_custom_field_observation(self):
        validate_spec(_spec(), _Metadata())
        definitions = [MappingSpec(**{key: item[key] for key in MappingSpec.__dataclass_fields__}) for item in fixture_payload()["mappings"]]
        plan = plan_from_definitions("HeroCustomers", [*definitions, _spec()])
        record = {"id": 1, "company_name": "Legal GmbH", "company_legal_form":"  GmbH  "}
        self.assertEqual(plan.values(record)["custom_hero_company_legal_form"], "GmbH")
        self.assertNotIn("custom_hero_company_legal_form", plan.values({**record, "company_legal_form":"   "}))
        self.assertNotIn("custom_hero_company_legal_form", plan.values({**record, "company_legal_form":None}))

    def test_v3_requires_legal_form_and_v2_rejects_it(self):
        snapshot = SimpleNamespace(name="S", schema_fingerprint="f" * 64)
        v3 = _mapping(CUSTOMER_QUERY_V3, [LINE2_ID, LEGAL_FORM_ID], "v3")
        self.assertEqual(from_documents(_service(CUSTOMER_QUERY_V3), v3, snapshot).definition_ids[-1], LEGAL_FORM_ID)
        with self.assertRaises(ReadContractError):
            from_documents(_service(CUSTOMER_QUERY_V3), _mapping(CUSTOMER_QUERY_V3, [LINE2_ID], "v3"), snapshot)
        with self.assertRaises(ReadContractError):
            from_documents(_service(CUSTOMER_QUERY_V2), _mapping(CUSTOMER_QUERY_V2, [LINE2_ID, LEGAL_FORM_ID], "v2"), snapshot)

    def test_wrong_target_stays_closed(self):
        wrong = MappingSpec("HeroCustomers", "company_legal_form", "Customer", "customer_type", "string|null", transformation="Trim")
        with self.assertRaises(DirectMappingError):
            validate_spec(wrong, _Metadata())
