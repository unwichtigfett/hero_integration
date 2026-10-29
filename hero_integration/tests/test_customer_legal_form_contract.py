from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import json

from hero_integration.services.direct_mapping import DirectMappingError, MappingSpec, validate_spec
from hero_integration.services.mapping_runtime import plan_from_definitions
from hero_integration.services.read_contract import ReadContractError, from_documents
from hero_integration.sources.hero import CUSTOMER_QUERY_V1, CUSTOMER_QUERY_V2, CUSTOMER_QUERY_V3
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
    return SimpleNamespace(name="MAP", status="Active", direction="QUERY_TO_FRAPPE", operation_kind="QUERY", source_instance='source', mapping_revision=1, schema_snapshot="S", schema_fingerprint="f" * 64,
        mapping_json=json.dumps({"target_operation":"HeroCustomers", "records_path":"contacts", "identity_path":"id", "page_size":100,
            "customer_contract_version":version, "mapping_definition_ids":ids,
            "variables":{"first":"$page_size","offset":"$offset","orderBy":"id","showDeleted":True}}))


class TestCustomerLegalFormContract(unittest.TestCase):
    def setUp(self):
        def rule(_doctype, filters, **_kwargs):
            identity = filters['definition_id']
            value = {'definition_id':identity, 'personal_graphql_mapping':'', 'status':'Active', 'operation':'HeroCustomers',
                     'schema_snapshot':'S', 'schema_fingerprint':'f'*64}
            if identity == LINE2_ID:
                value.update(source_path='address.line_2', source_type='string|null', target_doctype='Address',
                    target_field='address_line2', purpose='Native Binding', transformation='Trim', relation='Customer Primary Address')
            elif identity == LEGAL_FORM_ID:
                value.update(source_path='company_legal_form', source_type='string|null', target_doctype='Customer',
                    target_field='custom_hero_company_legal_form', purpose='Field', transformation='Trim', relation='')
            return [value]
        fake = SimpleNamespace(db=SimpleNamespace(has_column=lambda *_:False), get_all=rule)
        patcher = patch.dict(sys.modules, {'frappe':fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reserved_source_paths_cannot_bypass_literal_ids_via_other_targets(self):
        from hero_integration.services.mapping_rule_lineage import customer_legacy_literals
        mapping = _mapping(CUSTOMER_QUERY_V1, ['custom-rule'], 'v1')
        for source_path in ('address.line_2', 'company_legal_form'):
            row = {'definition_id':'custom-rule', 'personal_graphql_mapping':'', 'status':'Active',
                   'operation':'HeroCustomers', 'schema_snapshot':'S', 'schema_fingerprint':'f'*64,
                   'source_path':source_path, 'target_doctype':'Customer', 'target_field':'custom_other',
                   'purpose':'Field', 'transformation':'Trim'}
            with self.subTest(source_path=source_path), self.assertRaises(ValueError):
                customer_legacy_literals(['custom-rule'], mapping, lambda _: [row], lambda _: [])

    def test_scoped_lineage_preserves_literal_selection_and_rejects_ambiguity(self):
        from hero_integration.services.mapping_rule_lineage import next_lineage
        from hero_integration.services.personal_mapping_content import RULE_FIELDS
        root_id = 'a16b15fa-7ca8-4722-b63c-ac60cc46629a'
        version_id = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        root = {'definition_id':LINE2_ID, 'portable_definition_id':root_id, 'portable_lineage_json':'',
                'status':'Active', 'operation':'HeroCustomers', 'schema_snapshot':'S', 'schema_fingerprint':'f'*64,
                'personal_graphql_mapping':'', 'schema_manifest':'', 'schema_dependencies':'[]',
                'source_path':'address.line_2', 'source_type':'string|null', 'target_doctype':'Address',
                'target_field':'address_line2', 'purpose':'Native Binding', 'transformation':'Trim',
                'relation':'Customer Primary Address', 'priority':5}
        class Row(dict):
            __getattr__ = dict.__getitem__
        revised = {**root, 'definition_id':version_id, 'portable_definition_id':version_id,
                   'personal_graphql_mapping':'MAP'}
        revised['portable_lineage_json'] = next_lineage(Row(root), version_id, {key:revised[key] for key in RULE_FIELDS})
        records = [root, revised]
        def get_all(_doctype, filters, **_kwargs):
            return [dict(row) for row in records if all(row.get(key) == value for key, value in filters.items())]
        fake = SimpleNamespace(db=SimpleNamespace(has_column=lambda *_:True), get_all=get_all)
        snapshot = SimpleNamespace(name='S', schema_fingerprint='f'*64, source_instance='source')
        with patch.dict(sys.modules, {'frappe':fake}):
            mapping = _mapping(CUSTOMER_QUERY_V2, [version_id], 'v2')
            self.assertEqual(from_documents(_service(CUSTOMER_QUERY_V2), mapping, snapshot).definition_ids, (version_id,))
            legal_root_id = '8d2c984b-d898-463b-9fea-23be6102ad83'
            legal_version_id = '9d8fe02d-9eb0-49b1-a599-e0630f421010'
            legal_root = {**root, 'definition_id':LEGAL_FORM_ID, 'portable_definition_id':legal_root_id,
                'source_path':'company_legal_form', 'target_doctype':'Customer', 'target_field':'custom_hero_company_legal_form',
                'purpose':'Field', 'relation':''}
            legal_version = {**legal_root, 'definition_id':legal_version_id,
                             'portable_definition_id':legal_version_id, 'personal_graphql_mapping':'MAP'}
            legal_version['portable_lineage_json'] = next_lineage(Row(legal_root), legal_version_id,
                {key:legal_version[key] for key in RULE_FIELDS})
            records.extend([legal_root, legal_version])
            both = [version_id, legal_version_id]
            self.assertEqual(from_documents(_service(CUSTOMER_QUERY_V3), _mapping(CUSTOMER_QUERY_V3, both, 'v3'), snapshot).definition_ids,
                             tuple(both))
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V2), _mapping(CUSTOMER_QUERY_V2, both, 'v2'), snapshot)
            records.remove(legal_root); records.remove(legal_version)
            revised['definition_id'] = 'target-local-rule'
            remapped = _mapping(CUSTOMER_QUERY_V2, ['target-local-rule'], 'v2')
            self.assertEqual(from_documents(_service(CUSTOMER_QUERY_V2), remapped, snapshot).definition_ids, ('target-local-rule',))
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V1), _mapping(CUSTOMER_QUERY_V1, ['target-local-rule'], 'v1'), snapshot)
            revised['definition_id'] = version_id
            root['definition_id'] = 'unrelated-root'
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V1), _mapping(CUSTOMER_QUERY_V1, [version_id], 'v1'), snapshot)
            root['definition_id'] = LINE2_ID
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V1), _mapping(CUSTOMER_QUERY_V1, [version_id], 'v1'), snapshot)
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V2), _mapping(CUSTOMER_QUERY_V2, [LINE2_ID, version_id], 'v2'), snapshot)
            revised['personal_graphql_mapping'] = 'FOREIGN'
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V2), mapping, snapshot)
            revised['personal_graphql_mapping'] = 'MAP'
            lineage = revised['portable_lineage_json']
            revised['portable_lineage_json'] = ''
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V1), _mapping(CUSTOMER_QUERY_V1, [version_id], 'v1'), snapshot)
            revised['portable_lineage_json'] = lineage
            revised['target_field'] = 'other'
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V2), mapping, snapshot)
            revised['target_field'] = 'address_line2'
            records.remove(root)
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V2), mapping, snapshot)
            records.append(root)
            root['schema_snapshot'] = 'FOREIGN-SNAPSHOT'
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V2), mapping, snapshot)
            root['schema_snapshot'] = 'S'
            root['personal_graphql_mapping'] = 'FOREIGN'
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V2), _mapping(CUSTOMER_QUERY_V2, [LINE2_ID], 'v2'), snapshot)
            root['personal_graphql_mapping'] = ''
            root['status'] = 'Draft'
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V2), _mapping(CUSTOMER_QUERY_V2, [LINE2_ID], 'v2'), snapshot)
            root['status'] = 'Active'
            records.append(dict(root))
            with self.assertRaises(ReadContractError):
                from_documents(_service(CUSTOMER_QUERY_V2), mapping, snapshot)

    def test_customer_scoped_uuid_without_lineage_column_is_not_reinterpreted(self):
        identity = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        rows = [{'definition_id':identity, 'personal_graphql_mapping':'MAP'}]
        fake = SimpleNamespace(db=SimpleNamespace(has_column=lambda *_:False), get_all=lambda *_args, **_kwargs:rows)
        snapshot = SimpleNamespace(name='S', schema_fingerprint='f'*64, source_instance='source')
        with patch.dict(sys.modules, {'frappe':fake}):
            with self.assertRaisesRegex(ReadContractError, 'schema is missing'):
                from_documents(_service(CUSTOMER_QUERY_V1), _mapping(CUSTOMER_QUERY_V1, [identity], 'v1'), snapshot)
            rows[:] = [{'definition_id':identity, 'personal_graphql_mapping':'', 'status':'Active',
                'operation':'HeroCustomers', 'schema_snapshot':'S', 'schema_fingerprint':'f'*64,
                'source_path':'address.line_2', 'source_type':'string|null', 'target_doctype':'Address',
                'target_field':'address_line2', 'purpose':'Native Binding', 'transformation':'Trim',
                'relation':'Customer Primary Address'}]
            with self.assertRaisesRegex(ReadContractError, 'schema is missing'):
                from_documents(_service(CUSTOMER_QUERY_V1), _mapping(CUSTOMER_QUERY_V1, [identity], 'v1'), snapshot)
    def test_draft_mapping_is_a_read_only_custom_field_observation(self):
        validate_spec(_spec(), _Metadata())
        definitions = [MappingSpec(**{key: item[key] for key in MappingSpec.__dataclass_fields__}) for item in fixture_payload()["mappings"]]
        plan = plan_from_definitions("HeroCustomers", [*definitions, _spec()])
        record = {"id": 1, "company_name": "Legal GmbH", "company_legal_form":"  GmbH  "}
        self.assertEqual(plan.values(record)["custom_hero_company_legal_form"], "GmbH")
        self.assertNotIn("custom_hero_company_legal_form", plan.values({**record, "company_legal_form":"   "}))
        self.assertNotIn("custom_hero_company_legal_form", plan.values({**record, "company_legal_form":None}))

    def test_v3_requires_legal_form_and_v2_rejects_it(self):
        snapshot = SimpleNamespace(name="S", schema_fingerprint="f" * 64, source_instance='source')
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
