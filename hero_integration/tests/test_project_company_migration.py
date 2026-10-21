from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace

from hero_integration.tests.mock_hero_v1 import AMBIGUOUS_PROJECT, CUSTOMERS, PROJECTS, UPDATED_CUSTOMER
from hero_integration.tests.mapping_fixture import fixture_plan
from hero_integration.sources.hero import SourcePage


def _project_title_inputs(**overrides):
    values = {
        "project_nr": "1111",
        "project_type": {"code": "PV"},
        "customer": {"title": "Herr", "full_name": "Max Mustermann"},
        "address": {
            "formatted_address": "", "street": "Musterstraße", "house_number": "10",
            "postal_code": "77777", "city": "Musterstadt",
        },
    }
    values.update(overrides)
    return values

class TestProjectCompanyMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frappe = _FakeFrappe()
        cls._install_fake_frappe()
        cls.migration = importlib.import_module("hero_integration.services.migration")
        cls._confirmed_project_company = cls.migration.confirmed_project_company
        cls._synchronize_home_phone = cls.migration.synchronize_home_phone
        cls._home_phone_for_customer = cls.migration.home_phone_for_customer
        cls._active_mapping_plan = cls.migration.active_mapping_plan
        cls._synchronize_customer_relations = cls.migration._synchronize_customer_relations

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("hero_integration.services.migration", None)
        sys.modules.pop("frappe.utils", None)
        sys.modules.pop("frappe", None)

    @classmethod
    def _install_fake_frappe(cls):
        frappe_module = types.ModuleType("frappe")
        frappe_module.db = cls.frappe.db
        frappe_module.get_doc = cls.frappe.get_doc
        frappe_module.get_meta = cls.frappe.get_meta
        frappe_module.get_single = cls.frappe.get_single
        frappe_module.has_permission = cls.frappe.has_permission
        frappe_module.log_error = lambda **kwargs: None
        utils_module = types.ModuleType("frappe.utils")
        utils_module.now_datetime = lambda: "2026-10-16 00:00:00"
        sys.modules["frappe"] = frappe_module
        sys.modules["frappe.utils"] = utils_module

    def setUp(self):
        self.frappe.reset()
        self.migration._link = self.frappe.link
        self.migration.confirmed_project_company = type(self)._confirmed_project_company
        self.migration.synchronize_home_phone = lambda customer, phone: None
        self.migration.home_phone_for_customer = lambda customer: None
        self.migration.active_mapping_plan = fixture_plan
        self.migration._synchronize_customer_relations = lambda *args: None
        self.migration.notify_conflict_once = lambda *args, **kwargs: False

    def test_confirmed_company_creates_project_with_customer_and_stable_link(self):
        record = _project_title_inputs(id=201, customer_id=101, project_title="Synthetic HERO Project")

        first = self.migration._migrate_project(record, SimpleNamespace(mode="Execute"))
        second = self.migration._migrate_project(record, SimpleNamespace(mode="Execute"))

        self.assertEqual(first.code, "CREATED")
        self.assertEqual(second.code, "SKIPPED")
        self.assertEqual(len(self.frappe.projects), 1)
        self.assertEqual(
            self.frappe.projects[0],
            {
                "name": "PROJ-0001",
                "project_name": "PV-1111 | Photovoltaik, Herr Max Mustermann, Musterstraße 10, 77777 Musterstadt",
                "customer": "Synthetic Customer",
                "company": "Executing Company",
                "custom_hero_source_id": "default:201",
                "custom_hero_project_number": "1111",
                "custom_hero_customer_title": "Herr",
                "custom_hero_customer_full_name": "Max Mustermann",
            },
        )
        self.assertEqual(self.frappe.links[("project", "201")].target_name, "PROJ-0001")

    def test_customer_contact_values_are_dry_run_safe_and_idempotent(self):
        record = {
            "id": 301, "full_name": "Synthetic Ada", "first_name": "Ada", "last_name": "Lovelace",
            "email": "ada@synthetic.invalid", "phone_mobile": "+49 555 101",
        }

        preview = self.migration._migrate_customer(record, SimpleNamespace(mode="Preview"))
        self.assertEqual(preview.code, "PREVIEW_OK")
        self.assertFalse(self.frappe.customers)

        created = self.migration._migrate_customer(record, SimpleNamespace(mode="Execute"))
        repeated = self.migration._migrate_customer(record, SimpleNamespace(mode="Execute"))

        self.assertEqual((created.code, repeated.code), ("CREATED", "SKIPPED"))
        self.assertEqual(self.frappe.customers[0], {
            "name": "CUST-0001", "customer_name": "Synthetic Ada", "customer_type": "Individual",
            "first_name": "Ada", "last_name": "Lovelace", "email_id": "ada@synthetic.invalid",
            "mobile_no": "+49 555 101", "custom_hero_source_id": "301",
        })
        link = self.frappe.links[("customer", "301")]
        self.assertTrue(link.mapping_version.startswith("hero-mapping-"))

    def test_transfer_persists_every_custom_field_value_without_system_field_writes(self):
        customer_record = {
            "id": 401, "nr": "C-401", "full_name": "Synthetic Ada", "first_name": "Ada",
            "last_name": "Lovelace", "email": "ada@synthetic.invalid", "phone_mobile": "+49 555 401",
            "phone_home": "+49 556 401", "company_name": "", "category": "Synthetic",
            "type": "Prospect", "is_deleted": False,
        }
        project_record = _project_title_inputs(**{
            "id": 501, "customer_id": 401, "project_id": 601, "project_title": "Synthetic Roof",
            "display_id": "P-501", "created": "2026-10-18T10:00:00+00:00",
            "modified": "2026-10-18T11:00:00+00:00",
        })

        self.assertEqual(self.migration._migrate_customer(customer_record, SimpleNamespace(mode="Execute")).code, "CREATED")
        self.assertEqual(self.migration._migrate_project(project_record, SimpleNamespace(mode="Execute")).code, "CREATED")

        customer = self.frappe.customers[0]
        project = self.frappe.projects[0]
        self.assertEqual(customer["custom_hero_source_id"], "401")
        self.assertEqual(customer["custom_hero_customer_number"], "C-401")
        self.assertEqual(customer["custom_hero_customer_category"], "Synthetic")
        self.assertEqual(customer["custom_hero_customer_type"], "Prospect")
        self.assertEqual(project["custom_hero_source_id"], "default:501")
        self.assertEqual(project["custom_hero_related_project_id"], "601")
        self.assertEqual(project["custom_hero_display_id"], "P-501")
        self.assertEqual(project["custom_hero_source_created"], "2026-10-18T10:00:00+00:00")
        self.assertEqual(project["custom_hero_source_modified"], "2026-10-18T11:00:00+00:00")
        self.assertNotIn("creation", project)
        self.assertNotIn("modified", project)

    def test_explicit_fixture_mapping_output_is_used_by_customer_import(self):
        result = self.migration._migrate_customer(
            {"id": 402, "full_name": "Synthetic Ada", "company_name": "", "category": "Synthetic Territory"},
            SimpleNamespace(mode="Execute"),
        )

        self.assertEqual(result.code, "CREATED")
        self.assertEqual(self.frappe.customers[0]["custom_hero_customer_category"], "Synthetic Territory")

    def test_hero_only_customer_contact_change_updates_through_native_validation(self):
        record = {"id": 301, "full_name": "Synthetic Ada", "email": "ada@synthetic.invalid"}
        self.migration._migrate_customer(record, SimpleNamespace(mode="Execute"))

        result = self.migration._migrate_customer(
            {**record, "email": "changed@synthetic.invalid"}, SimpleNamespace(mode="Execute")
        )

        self.assertEqual(result.code, "UPDATED")
        self.assertEqual(self.frappe.customers[0]["email_id"], "changed@synthetic.invalid")
        self.assertEqual(self.frappe.links[("customer", "301")].sync_status, "Synchronized")

    def test_same_field_changed_on_both_sides_conflicts_without_overwrite(self):
        record = {"id": 301, "full_name": "Synthetic Ada", "email": "ada@synthetic.invalid"}
        self.migration._migrate_customer(record, SimpleNamespace(mode="Execute"))
        self.frappe.customers[0]["email_id"] = "local@synthetic.invalid"

        result = self.migration._migrate_customer(
            {**record, "email": "remote@synthetic.invalid"}, SimpleNamespace(mode="Execute")
        )

        self.assertEqual(result.code, "CONFLICT")
        self.assertEqual(self.frappe.customers[0]["email_id"], "local@synthetic.invalid")
        self.assertIn("email_id", self.frappe.links[("customer", "301")].conflict_detail)

    def test_preview_existing_change_does_not_write_link_or_target_state(self):
        record = {"id": 301, "full_name": "Synthetic Ada", "email": "ada@synthetic.invalid"}
        self.migration._migrate_customer(record, SimpleNamespace(mode="Execute"))
        link = self.frappe.links[("customer", "301")]
        before = (link.sync_status, link.conflict_detail, link.source_payload_hash, link.target_payload_hash)

        result = self.migration._migrate_customer(
            {**record, "email": "preview@synthetic.invalid"}, SimpleNamespace(mode="Preview")
        )

        self.assertEqual(result.code, "PREVIEW_OK")
        self.assertEqual(self.frappe.customers[0]["email_id"], "ada@synthetic.invalid")
        self.assertEqual((link.sync_status, link.conflict_detail, link.source_payload_hash, link.target_payload_hash), before)

    def test_v1_customer_link_requires_review_before_v2_contact_enrichment(self):
        record = {"id": 301, "full_name": "Synthetic Ada", "email": "ada@synthetic.invalid"}
        self.frappe.customers.append({
            "name": "CUST-0001", "customer_name": "Synthetic Ada", "customer_type": "Individual",
        })
        self.frappe.links[("customer", "301")] = SimpleNamespace(
            target_doctype="Customer",
            target_name="CUST-0001",
            source_payload_hash=self.migration._digest(record),
            target_payload_hash=self.migration._digest({
                "customer_name": "Synthetic Ada", "customer_type": "Individual",
            }),
            mapping_version="hero-customer-to-erpnext-customer-v1",
        )

        result = self.migration._migrate_customer(record, SimpleNamespace(mode="Execute"))

        self.assertEqual(result.code, "CONFLICT")
        self.assertEqual(self.frappe.customers[0].get("email_id"), None)

    def test_deleted_customer_never_deletes_a_target(self):
        record = {"id": 301, "full_name": "Synthetic Ada"}
        self.migration._migrate_customer(record, SimpleNamespace(mode="Execute"))

        result = self.migration._migrate_customer(
            {**record, "is_deleted": True}, SimpleNamespace(mode="Execute")
        )

        self.assertEqual(result.code, "SKIPPED_SOURCE_DELETED")
        self.assertEqual(len(self.frappe.customers), 1)
        self.assertEqual(self.frappe.links[("customer", "301")].sync_status, "Source Deleted")

    def test_contact_person_is_recorded_without_creating_a_customer_duplicate(self):
        result = self.migration._migrate_customer(
            {"id": 302, "full_name": "Synthetic Contact", "is_contact_person": True},
            SimpleNamespace(mode="Execute"),
        )

        self.assertEqual(result.code, "SKIPPED_CONTACT_PERSON")
        self.assertFalse(self.frappe.customers)

    def test_customer_primary_address_accepts_only_the_proven_relation_shape(self):
        self.assertEqual(
            self.migration._customer_primary_address(
                {"address": {"id": 801, "street": "Synthetic Street 1", "zipcode": "77777", "city": "Synthetic City"}}
            ),
            {"id": "801", "street": "Synthetic Street 1", "zipcode": "77777", "city": "Synthetic City"},
        )
        with self.assertRaises(self.migration.MappingPlanError):
            self.migration._customer_primary_address({"address": {"id": 801, "street": "", "zipcode": "77777", "city": "Synthetic City"}})

    def test_project_action_always_runs_the_customer_phase_first_and_stops_at_its_gate(self):
        events = []
        run = SimpleNamespace(action="Project Migration", source_instance="", save=lambda: None)
        source = SimpleNamespace(source_instance="synthetic", customers=object(), projects=object())
        original = self.migration._process_phase
        try:
            self.migration._process_phase = lambda item, phase, reader: events.append(phase) or phase == "customer"
            self.migration._process_requested_phases(run, source)
        finally:
            self.migration._process_phase = original

        self.assertEqual(events, ["customer"])

    def test_project_action_starts_projects_only_after_a_completed_customer_phase(self):
        events = []
        run = SimpleNamespace(
            action="Project Migration", source_instance="", customer_phase_state="Queued", state="Running",
            save=lambda: None,
        )
        source = SimpleNamespace(source_instance="synthetic", customers=object(), projects=object())
        original = self.migration._process_phase
        try:
            def phase(item, name, reader):
                events.append(name)
                if name == "customer":
                    item.customer_phase_state = "Completed"
                return True
            self.migration._process_phase = phase
            self.migration._process_requested_phases(run, source)
        finally:
            self.migration._process_phase = original

        self.assertEqual(events, ["customer", "project"])

    def test_customer_phase_paginates_to_completion_and_persists_a_resume_checkpoint(self):
        run = SimpleNamespace(
            customer_phase_state="Queued", customer_phase_processed=0, customer_phase_created=0,
            customer_phase_skipped=0, customer_phase_blocked=0, resume_phase="", resume_offset=0,
            resume_record_index=0, state="Running", save=lambda: None, reload=lambda: None,
        )
        pages = {
            0: SourcePage(({"id": 1}, {"id": 2}), 2),
            2: SourcePage(({"id": 3},), None),
        }
        calls, recorded = [], []
        original_migrate, original_record = self.migration._migrate_customer, self.migration._record_result
        try:
            self.migration._migrate_customer = lambda record, item: self.migration.Result("CREATED", "synthetic")
            self.migration._record_result = lambda item, phase, record, result: recorded.append(record["id"])
            self.assertTrue(self.migration._process_phase(run, "customer", lambda offset: calls.append(offset) or pages[offset]))
        finally:
            self.migration._migrate_customer, self.migration._record_result = original_migrate, original_record

        self.assertEqual((calls, recorded), ([0, 2], [1, 2, 3]))
        self.assertEqual(run.customer_phase_state, "Completed")
        self.assertEqual((run.customer_phase_processed, run.customer_phase_created), (3, 3))
        self.assertEqual((run.resume_phase, run.resume_offset, run.resume_record_index), ("customer", 0, 0))

    def test_missing_or_invalid_company_blocks_before_project_insert(self):
        self.frappe.settings.erpnext_company = ""

        result = self.migration._migrate_project(
            _project_title_inputs(id=202, customer_id=101, project_title="Blocked Project"),
            SimpleNamespace(mode="Execute"),
        )

        self.assertEqual(result.code, "BLOCKED_RECORD")
        self.assertFalse(self.frappe.projects)
        self.assertFalse(self.frappe.links.get(("project", "202")))

    def test_non_customer_link_cannot_be_used_as_a_project_customer(self):
        self.frappe.links[("customer", "101")] = SimpleNamespace(
            target_name="Not a Customer", target_doctype="Project"
        )

        result = self.migration._migrate_project(
            _project_title_inputs(id=203, customer_id=101, project_title="Blocked Reference"),
            SimpleNamespace(mode="Execute"),
        )

        self.assertEqual(result.code, "BLOCKED_RECORD")
        self.assertFalse(self.frappe.projects)

    def test_unlinked_customer_relation_is_blocked_without_target_mutation(self):
        result = self.migration._migrate_project(AMBIGUOUS_PROJECT, SimpleNamespace(mode="Execute"))

        self.assertEqual(result.code, "BLOCKED_RECORD")
        self.assertFalse(self.frappe.projects)
        self.assertNotIn(("project", str(AMBIGUOUS_PROJECT["id"])), self.frappe.links)

    def test_same_project_match_id_from_distinct_source_instances_creates_distinct_projects(self):
        self.frappe.links[("customer", "101", "one")] = SimpleNamespace(
            target_name="Synthetic Customer One", target_doctype="Customer"
        )
        self.frappe.links[("customer", "101", "two")] = SimpleNamespace(
            target_name="Synthetic Customer Two", target_doctype="Customer"
        )
        first = _project_title_inputs(id=777, customer_id=101, project_nr="0001", _hero_source_instance="one")
        second = _project_title_inputs(id=777, customer_id=101, project_nr="0002", _hero_source_instance="two")

        self.assertEqual(self.migration._migrate_project(first, SimpleNamespace(mode="Execute")).code, "CREATED")
        self.assertEqual(self.migration._migrate_project(second, SimpleNamespace(mode="Execute")).code, "CREATED")

        self.assertEqual(len(self.frappe.projects), 2)
        self.assertIn(("project", "777", "one"), self.frappe.links)
        self.assertIn(("project", "777", "two"), self.frappe.links)

    def test_changed_title_contract_updates_when_the_target_field_is_unchanged(self):
        record = _project_title_inputs(id=778, customer_id=101, project_nr="0001")
        self.assertEqual(self.migration._migrate_project(record, SimpleNamespace(mode="Execute")).code, "CREATED")
        original = self.frappe.projects[0]["project_name"]

        result = self.migration._migrate_project(
            _project_title_inputs(id=778, customer_id=101, project_nr="0002"), SimpleNamespace(mode="Execute")
        )

        self.assertEqual(result.code, "UPDATED")
        self.assertNotEqual(self.frappe.projects[0]["project_name"], original)

    def test_missing_active_mapping_blocks_target_mutation(self):
        self.migration.active_mapping_plan = lambda operation: (_ for _ in ()).throw(
            self.migration.MappingPlanError("No active mapping definitions exist for this operation.")
        )

        customer = self.migration._migrate_customer({"id": 301, "full_name": "Synthetic Ada"}, SimpleNamespace(mode="Execute"))
        project = self.migration._migrate_project(_project_title_inputs(id=201, customer_id=101, project_title="Synthetic Project"), SimpleNamespace(mode="Execute"))

        self.assertEqual((customer.code, project.code), ("BLOCKED_RECORD", "BLOCKED_RECORD"))
        self.assertFalse(self.frappe.customers)
        self.assertFalse(self.frappe.projects)

    def test_dataset_transfer_maps_relations_and_detects_repeats_and_updates(self):
        customers = [
            self.migration._migrate_customer(record, SimpleNamespace(mode="Execute")) for record in CUSTOMERS
        ]
        projects = [
            self.migration._migrate_project(record, SimpleNamespace(mode="Execute")) for record in PROJECTS
        ]
        repeated = [
            self.migration._migrate_project(record, SimpleNamespace(mode="Execute")) for record in PROJECTS
        ]
        updated = self.migration._migrate_customer(UPDATED_CUSTOMER, SimpleNamespace(mode="Execute"))

        self.assertEqual(sum(result.code == "CREATED" for result in customers), 24)
        self.assertEqual(sum(result.code == "SKIPPED_SOURCE_DELETED" for result in customers), 1)
        self.assertEqual(sum(result.code == "CREATED" for result in projects), 40)
        self.assertTrue(all(result.code == "SKIPPED" for result in repeated))
        self.assertEqual(updated.code, "UPDATED")
        self.assertEqual(len(self.frappe.customers), 24)
        self.assertEqual(len(self.frappe.projects), 40)
        self.assertEqual(self.frappe.customers[0]["email_id"], "person1-updated@synthetic.invalid")
        self.assertTrue(all(project["company"] == "Executing Company" for project in self.frappe.projects))
        self.assertTrue(all(project["customer"].startswith("CUST-") for project in self.frappe.projects))


class _FakeFrappe:
    def __init__(self):
        self.db = _FakeDatabase(self)
        self.reset()

    def reset(self):
        self.projects = []
        self.customers = []
        self.settings = SimpleNamespace(erpnext_company="Executing Company", erpnext_company_confirmed=1)
        self.schema_fields = {
            "Customer": {
                "customer_name", "customer_type", "first_name", "last_name", "email_id", "mobile_no",
                "custom_hero_source_id", "custom_hero_customer_number", "custom_hero_customer_category",
                "custom_hero_customer_type",
            },
            "Project": {
                "project_name", "customer", "company", "custom_hero_source_id",
                "custom_hero_related_project_id", "custom_hero_display_id", "custom_hero_source_created",
                "custom_hero_source_modified",
            },
        }
        self.links = {
            ("customer", "101"): SimpleNamespace(target_name="Synthetic Customer", target_doctype="Customer"),
        }
    def get_meta(self, doctype):
        if doctype == "Company":
            return SimpleNamespace(has_field=lambda fieldname: fieldname == "disabled")
        return SimpleNamespace(
            has_field=lambda fieldname: fieldname in self.schema_fields[doctype],
            get_field=lambda fieldname: SimpleNamespace(length=140) if fieldname == "project_name" else None,
        )

    def get_single(self, doctype):
        if doctype != "HERO Integration Settings":
            raise AssertionError(f"unexpected singleton: {doctype}")
        return self.settings

    @staticmethod
    def has_permission(doctype, permission_type, name):
        return doctype == "Company" and permission_type == "read" and name == "Executing Company"

    def get_doc(self, *args):
        if len(args) == 2:
            doctype, name = args
            if doctype in {"Customer", "Project"}:
                records = self.customers if doctype == "Customer" else self.projects
                return _ExistingDocument(name, records)
            raise AssertionError(f"unexpected document lookup: {args}")
        values = args[0]
        if values["doctype"] in {"Customer", "Project"}:
            return _TargetDocument(self, values)
        if values["doctype"] == "HERO Integration Link":
            return _LinkDocument(self, values)
        raise AssertionError(f"unexpected document creation: {values}")

    def link(self, source_type, source_id, source_instance="default"):
        return self.links.get((source_type, source_id, source_instance)) or self.links.get((source_type, source_id))


class _FakeDatabase:
    def __init__(self, frappe_api):
        self.frappe_api = frappe_api

    def exists(self, doctype, filters):
        if doctype == "DocType":
            return filters in {"Customer", "Project"}
        if doctype == "Company":
            return filters == "Executing Company"
        if doctype in {"Customer", "Project"}:
            if isinstance(filters, str):
                records = self.frappe_api.customers if doctype == "Customer" else self.frappe_api.projects
                return any(record.get("name") == filters for record in records)
            fieldname = "customer_name" if doctype == "Customer" else "project_name"
            records = self.frappe_api.customers if doctype == "Customer" else self.frappe_api.projects
            return next((record["name"] for record in records if record.get(fieldname) == filters[fieldname]), None)
        raise AssertionError(f"unexpected exists: {doctype}, {filters}")

    @staticmethod
    def get_value(doctype, name, fieldname):
        if (doctype, name, fieldname) != ("Company", "Executing Company", "disabled"):
            raise AssertionError(f"unexpected get_value: {doctype}, {name}, {fieldname}")
        return 0


class _TargetDocument:
    def __init__(self, frappe_api, values):
        self.frappe_api = frappe_api
        self.values = {key: value for key, value in values.items() if key != "doctype"}
        self.name = ""

    def insert(self):
        is_customer = self.values.get("customer_name") is not None
        records = self.frappe_api.customers if is_customer else self.frappe_api.projects
        prefix = "CUST" if is_customer else "PROJ"
        self.name = f"{prefix}-{len(records) + 1:04d}"
        self.values["name"] = self.name
        records.append(self.values)

    def get(self, fieldname):
        return self.values.get(fieldname)

    def set(self, fieldname, value):
        self.values[fieldname] = value

    def save(self):
        return None


class _ExistingDocument:
    def __init__(self, name, projects):
        self.name = name
        self.values = next(project for project in projects if project["name"] == name)

    def get(self, fieldname):
        return self.values.get(fieldname)

    def set(self, fieldname, value):
        self.values[fieldname] = value

    def save(self):
        return None


class _LinkDocument:
    def __init__(self, frappe_api, values):
        self.frappe_api = frappe_api
        self.values = values

    def insert(self):
        key = (self.values["source_type"], self.values["source_id"])
        if self.values.get("source_instance", "default") != "default":
            key = (*key, self.values["source_instance"])
        self.frappe_api.links[key] = SimpleNamespace(**self.values)
