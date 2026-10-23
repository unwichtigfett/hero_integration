from __future__ import annotations

import unittest
from types import SimpleNamespace
from uuid import uuid4

from hero_integration.services.direct_mapping import (
    DirectMappingError,
    MappingSpec,
    RELATION_CUSTOMER_PRIMARY_ADDRESS,
    validate_spec,
)
from hero_integration.services.mapping_runtime import MappingPlanError, plan_from_definitions
from hero_integration.services.migration import (
    CustomerAddressError,
    _customer_primary_address,
    _synchronize_customer_address,
)
from hero_integration.services.read_contract import ReadContractError, from_documents
from hero_integration.sources.hero import CUSTOMER_QUERY_V1, CUSTOMER_QUERY_V2
from hero_integration.tests.mapping_fixture import fixture_plan, fixture_payload


LINE2_DEFINITION_ID = "customer-primary-address-line2"


def run_contract_verification() -> dict[str, int]:
    """Run the non-persistent M-099 contract tests from a Bench process."""

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestCustomerAddressLine2Contract)
    result = unittest.TestResult()
    suite.run(result)
    if not result.wasSuccessful():
        failures = result.failures + result.errors
        raise AssertionError("M-099 contract tests failed: " + repr(failures))
    return {"tests_run": result.testsRun}


def run_customer_regression_verification() -> dict[str, int]:
    """Run the existing pure Customer-contact and mapping-exchange regressions."""

    from hero_integration.tests.test_contact_persistence import TestContactPersistence
    from hero_integration.tests.test_mapping_exchange import TestMappingExchange

    suite = unittest.TestSuite()
    loader = unittest.defaultTestLoader
    suite.addTests(loader.loadTestsFromTestCase(TestContactPersistence))
    suite.addTests(loader.loadTestsFromTestCase(TestMappingExchange))
    result = unittest.TestResult()
    suite.run(result)
    if not result.wasSuccessful():
        failures = result.failures + result.errors
        raise AssertionError("Customer regression tests failed: " + repr(failures))
    return {"tests_run": result.testsRun}


def verify_saved_snapshot_binding() -> dict[str, object]:
    """Read-only check of the real selected snapshot and native Address field."""

    import json

    import frappe

    from hero_integration.services.mapping_suggestions import source_fields

    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", "2b55ter0bc")
    fields = source_fields("HeroCustomers", json.loads(snapshot.snapshot_json), "address.line_2")
    selected = [field for field in fields if field.path == "address.line_2" and field.selectable]
    if len(selected) != 1 or selected[0].fieldtype != "String":
        raise AssertionError("Saved snapshot does not expose one selectable Customer.address.line_2 String field.")
    target = frappe.get_meta("Address").get_field("address_line2")
    if not target or target.fieldtype != "Data" or target.reqd or target.read_only:
        raise AssertionError("Address.address_line2 is not an optional writable Data field.")
    return {
        "snapshot": snapshot.name,
        "fingerprint": snapshot.schema_fingerprint,
        "source_path": selected[0].graphql_path,
        "source_type": selected[0].fieldtype,
        "target": {"doctype": "Address", "field": target.fieldname, "type": target.fieldtype},
    }


def verify_persisted_drafts(
    mapping_name: str = "HERO-GQL-MAP-00088",
    service_name: str = "HERO-GQL-00089",
) -> dict[str, object]:
    """Confirm the new documents are draft-only and V1 remains executable."""

    import frappe

    from hero_integration.services.read_contract import ReadContractError, from_documents

    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", "2b55ter0bc")
    v1_service = frappe.get_doc("HERO GraphQL Service", "HERO-GQL-00080")
    v1_mapping = frappe.get_doc("HERO GraphQL Mapping", "HERO-GQL-MAP-00079")
    v1_contract = from_documents(v1_service, v1_mapping, snapshot)
    draft_service = frappe.get_doc("HERO GraphQL Service", service_name)
    draft_mapping = frappe.get_doc("HERO GraphQL Mapping", mapping_name)
    draft_definition = frappe.get_doc(
        "HERO Frappe Mapping", {"definition_id": LINE2_DEFINITION_ID}
    )
    try:
        from_documents(draft_service, draft_mapping, snapshot)
    except ReadContractError as error:
        rejected_reason = str(error)
    else:
        raise AssertionError("A Draft M-099 service must not become executable.")
    if draft_service.status != "Draft" or draft_mapping.status != "Draft" or draft_definition.status != "Draft":
        raise AssertionError("M-099 draft documents were activated unexpectedly.")
    active_count = frappe.db.count("HERO Frappe Mapping", {"status": "Active"})
    if active_count != 75:
        raise AssertionError(f"Expected 75 active mappings, found {active_count}.")
    return {
        "v1": {"service_revision": v1_contract.service_revision, "mapping_revision": v1_contract.mapping_revision},
        "draft": {
            "service_revision": draft_service.service_revision,
            "mapping_revision": draft_mapping.mapping_revision,
            "definition_revision": draft_definition.mapping_revision,
            "rejected_reason": rejected_reason,
        },
        "active_mappings": active_count,
    }


class TestCustomerAddressLine2Contract(unittest.TestCase):
    def test_native_binding_is_closed_to_customer_primary_address_line2(self):
        valid = _line2_spec()
        validate_spec(valid, _Metadata())
        for invalid in (
            MappingSpec("HeroCustomers", "address.line_2", "Address", "address_line1", "string|null", purpose="Native Binding", transformation="Trim", relation=RELATION_CUSTOMER_PRIMARY_ADDRESS),
            MappingSpec("HeroCustomers", "address.line_2", "Customer", "custom_hero_customer_type", "string|null", purpose="Native Binding", transformation="Trim", relation=RELATION_CUSTOMER_PRIMARY_ADDRESS),
            MappingSpec("HeroCustomers", "address.city", "Address", "address_line2", "string|null", purpose="Native Binding", transformation="Trim", relation=RELATION_CUSTOMER_PRIMARY_ADDRESS),
        ):
            with self.assertRaises(DirectMappingError):
                validate_spec(invalid, _Metadata())

    def test_line2_is_available_only_when_the_selected_plan_contains_the_binding(self):
        definitions = [
            MappingSpec(**{key: item[key] for key in MappingSpec.__dataclass_fields__})
            for item in fixture_payload()["mappings"]
        ]
        record = {"address": {"line_2": "  Suite 2  "}}
        self.assertIsNone(plan_from_definitions("HeroCustomers", definitions).customer_primary_address_line2(record))
        selected = plan_from_definitions("HeroCustomers", [*definitions, _line2_spec()])
        self.assertEqual(selected.customer_primary_address_line2(record), "Suite 2")
        with self.assertRaises(MappingPlanError):
            plan_from_definitions("HeroCustomers", [*definitions, _line2_spec(), _line2_spec("customer-primary-address-line2-duplicate")]).customer_primary_address_line2(record)

    def test_selected_customer_address_binding_is_handed_to_the_address_adapter_not_customer_values(self):
        definitions = [
            MappingSpec(**{key: item[key] for key in MappingSpec.__dataclass_fields__})
            for item in fixture_payload()["mappings"]
        ]
        record = {"address": {"line_2": "  Suite 2  "}}
        selected = plan_from_definitions("HeroCustomers", [*definitions, _line2_spec()])

        self.assertNotIn("address_line2", selected.values(record))
        self.assertEqual(selected.customer_primary_address_line2(record), "Suite 2")

        invalid = MappingSpec(
            "HeroCustomers", "address.line_2", "Customer", "address_line2", "string|null",
            purpose="Native Binding", transformation="Trim", relation=RELATION_CUSTOMER_PRIMARY_ADDRESS,
            definition_id="invalid-customer-address-native-binding",
        )
        invalid_selected = plan_from_definitions("HeroCustomers", [*definitions, invalid])
        with self.assertRaises(MappingPlanError):
            invalid_selected.customer_primary_address_line2(record)
        with self.assertRaises(MappingPlanError):
            invalid_selected.values(record)

    def test_optional_line2_never_enters_the_address_payload_without_a_selected_value(self):
        record = _address_record()
        for value in (None, "", "   "):
            self.assertNotIn("line_2", _customer_primary_address(record, value))
        self.assertEqual(_customer_primary_address(record, "  Suite 2 ")["line_2"], "Suite 2")

    def test_v2_read_contract_requires_the_selected_native_binding_while_v1_stays_unchanged(self):
        snapshot = SimpleNamespace(name="S", schema_fingerprint="f" * 64)
        v1_mapping = _mapping(CUSTOMER_QUERY_V1, [item["definition_id"] for item in fixture_payload()["mappings"]])
        v1_service = _service(CUSTOMER_QUERY_V1, "M")
        self.assertEqual(from_documents(v1_service, v1_mapping, snapshot).mapping_revision, 1)

        v2_ids = [*json_definition_ids(), LINE2_DEFINITION_ID]
        v2_mapping = _mapping(CUSTOMER_QUERY_V2, v2_ids, version="v2")
        v2_service = _service(CUSTOMER_QUERY_V2, "M")
        self.assertEqual(from_documents(v2_service, v2_mapping, snapshot).target_operation, "HeroCustomers")

        with self.assertRaises(ReadContractError):
            from_documents(v2_service, _mapping(CUSTOMER_QUERY_V2, json_definition_ids(), version="v2"), snapshot)
        with self.assertRaises(ReadContractError):
            from_documents(v1_service, _mapping(CUSTOMER_QUERY_V1, [*json_definition_ids(), LINE2_DEFINITION_ID]), snapshot)

    def test_stale_approval_is_rejected(self):
        snapshot = SimpleNamespace(name="S", schema_fingerprint="f" * 64)
        service = _service(CUSTOMER_QUERY_V1, "M")
        mapping = _mapping(CUSTOMER_QUERY_V1, json_definition_ids())
        service.approved_service_revision = 0
        with self.assertRaises(ReadContractError):
            from_documents(service, mapping, snapshot)
        service.approved_service_revision = 1
        mapping.mapping_revision = 2
        with self.assertRaises(ReadContractError):
            from_documents(service, mapping, snapshot)


def run_isolated_frappe_verification() -> dict[str, object]:
    """Exercise the real Address/Link relation inside a savepoint and roll it back."""

    import frappe

    marker = f"M099-{uuid4().hex[:12]}"
    before = {
        "customer": frappe.db.count("Customer"),
        "address": frappe.db.count("Address"),
        "link": frappe.db.count("HERO Integration Link"),
    }
    country = frappe.get_all("Country", filters={"code": ["!=", ""]}, fields=["name", "code"], limit_page_length=1)
    if not country:
        raise AssertionError("An installed Country with a code is required for the M-099 isolated test.")
    savepoint = "hero_m099_address_line2"
    frappe.db.savepoint(savepoint)
    try:
        customer = frappe.get_doc({"doctype": "Customer", "customer_name": marker, "customer_type": "Company"}).insert()
        address = _customer_primary_address(_address_record(source_id=983001, country_code=country[0]["code"]), "Suite 2")
        _synchronize_customer_address(customer, address, "m099-test", "hero-mapping-m099")
        link_name = frappe.db.get_value(
            "HERO Integration Link",
            {"source_type": "customer_address", "source_id": "983001", "source_instance": "m099-test"},
        )
        link = frappe.get_doc("HERO Integration Link", link_name)
        target = frappe.get_doc("Address", link.target_name)
        if target.address_line2 != "Suite 2":
            raise AssertionError("Selected line_2 was not persisted.")
        _synchronize_customer_address(customer, address, "m099-test", "hero-mapping-m099")
        if frappe.db.count("HERO Integration Link", {"source_type": "customer_address", "source_id": "983001", "source_instance": "m099-test"}) != 1:
            raise AssertionError("Repeated processing created a duplicate address identity.")

        # A V1-created link can accept only the newly selected binding.  Its
        # empty, untracked target address_line2 must not appear as a conflict.
        v1_customer = frappe.get_doc(
            {"doctype": "Customer", "customer_name": marker + "-v1", "customer_type": "Company"}
        ).insert()
        v1_source = _address_record(source_id=983002, country_code=country[0]["code"])
        _synchronize_customer_address(
            v1_customer,
            _customer_primary_address(v1_source),
            "m099-test",
            "hero-mapping-m099-v1",
        )
        _synchronize_customer_address(
            v1_customer,
            _customer_primary_address(v1_source, "V2 extension"),
            "m099-test",
            "hero-mapping-m099-v2",
        )
        v1_link = frappe.get_doc(
            "HERO Integration Link",
            frappe.db.get_value(
                "HERO Integration Link",
                {"source_type": "customer_address", "source_id": "983002", "source_instance": "m099-test"},
            ),
        )
        if frappe.get_doc("Address", v1_link.target_name).address_line2 != "V2 extension":
            raise AssertionError("The V1-to-V2 line_2 extension was not persisted.")
        for empty in (None, "", "   "):
            preserved = _customer_primary_address(_address_record(source_id=983001, country_code=country[0]["code"]), empty)
            _synchronize_customer_address(customer, preserved, "m099-test", "hero-mapping-m099")
            if frappe.get_doc("Address", target.name).address_line2 != "Suite 2":
                raise AssertionError("An empty optional line_2 removed an existing target value.")
        other = frappe.get_doc({"doctype": "Customer", "customer_name": marker + "-shared", "customer_type": "Company"}).insert()
        target.append("links", {"link_doctype": "Customer", "link_name": other.name})
        target.save()
        try:
            _synchronize_customer_address(customer, address, "m099-test", "hero-mapping-m099")
        except CustomerAddressError:
            shared_blocked = True
        else:
            shared_blocked = False
        if not shared_blocked:
            raise AssertionError("A shared Address was not blocked.")
        result = {
            "persisted": True,
            "idempotent": True,
            "v1_transition": True,
            "empty_preserved": True,
            "shared_blocked": True,
        }
    finally:
        frappe.db.rollback(save_point=savepoint)
    after = {
        "customer": frappe.db.count("Customer"),
        "address": frappe.db.count("Address"),
        "link": frappe.db.count("HERO Integration Link"),
    }
    if after != before:
        raise AssertionError("M-099 isolated verification did not roll back all fixture records.")
    return result


def _line2_spec(definition_id: str = LINE2_DEFINITION_ID) -> MappingSpec:
    return MappingSpec(
        "HeroCustomers", "address.line_2", "Address", "address_line2", "string|null",
        purpose="Native Binding", transformation="Trim", relation=RELATION_CUSTOMER_PRIMARY_ADDRESS,
        definition_id=definition_id,
    )


def _address_record(source_id: int = 8001, country_code: str = "de") -> dict[str, object]:
    return {
        "address": {
            "id": source_id, "street": "Synthetic Street 1", "zipcode": "77777", "city": "Synthetic City",
            "country": {"code": country_code},
        },
    }


def json_definition_ids() -> list[str]:
    return [item["definition_id"] for item in fixture_payload()["mappings"]]


def _mapping(query: str, definition_ids: list[str], version: str = "v1"):
    import json

    return SimpleNamespace(
        name="M", status="Active", mapping_revision=1, schema_snapshot="S", schema_fingerprint="f" * 64,
        mapping_json=json.dumps({
            "target_operation": "HeroCustomers", "records_path": "contacts", "identity_path": "id",
            "mapping_definition_ids": definition_ids,
            "variables": {"first": "$page_size", "offset": "$offset", "orderBy": "id", "showDeleted": True},
            "page_size": 100, **({"customer_contract_version": version} if version != "v1" else {}),
        }),
    )


def _service(query: str, mapping_name: str):
    return SimpleNamespace(
        name="SVC", status="Validated", execution_status="Approved", service_revision=1,
        approved_service_revision=1, approved_mapping_revision=1, schema_snapshot="S", schema_fingerprint="f" * 64,
        read_mapping=mapping_name, query_document=query,
    )


class _Metadata:
    @staticmethod
    def field(doctype, fieldname):
        del doctype, fieldname
        return None
