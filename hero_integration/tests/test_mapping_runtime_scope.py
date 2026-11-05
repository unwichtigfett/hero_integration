import sys
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import mapping_runtime
from hero_integration.services.mapping_rule_lineage import semantic_fingerprint
from hero_integration.services.personal_mapping_content import RULE_FIELDS


def lineage_db(row):
    revision = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
    payload = {'version':1, 'logical_id':'a16b15fa-7ca8-4722-b63c-ac60cc46629a', 'revision_id':revision,
               'predecessor_id':'a16b15fa-7ca8-4722-b63c-ac60cc46629a',
               'content_hash':semantic_fingerprint(row['operation'], row['schema_fingerprint'],
                                                    {key:row.get(key) for key in RULE_FIELDS})}
    return SimpleNamespace(has_column=lambda *_: True, get_value=lambda *_args, **_kwargs:
                           {'portable_definition_id':revision, 'portable_lineage_json':json.dumps(payload)})


class TestMappingRuntimeScope(unittest.TestCase):
    def test_personal_versions_require_their_mapping_and_do_not_change_global_plan(self):
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as controller

        def row(identity, scope):
            return {'definition_id': identity, 'personal_graphql_mapping': scope, 'operation': 'HeroCustomers',
                    'source_path': 'nr', 'source_type': 'string|null', 'target_doctype': 'Customer',
                    'target_field': identity, 'purpose': 'Field', 'transformation': 'Identity',
                    'relation': '', 'priority': 5, 'schema_snapshot':'snapshot', 'schema_fingerprint':'fingerprint'}
        fake = SimpleNamespace(db=lineage_db(row('version', 'mapping-a')), get_all=lambda *_args, **_kwargs: [row('shared', None), row('version', 'mapping-a')],
                               get_doc=lambda *_args: SimpleNamespace(status='Active', direction='QUERY_TO_FRAPPE', schema_snapshot='snapshot', schema_fingerprint='fingerprint',
                                   mapping_json=json.dumps({'target_operation':'HeroCustomers', 'mapping_definition_ids':['shared','version']})),
                               get_meta=lambda _name: SimpleNamespace(get_field=lambda _field: SimpleNamespace(is_custom_field=True)))
        metadata = SimpleNamespace(field=lambda *_args: SimpleNamespace(is_custom_field=True))
        with patch.dict(sys.modules, {'frappe': fake}), patch.object(controller, '_FrappeMetadata', return_value=metadata), patch.object(
            mapping_runtime, 'plan_from_definitions', side_effect=lambda _operation, values, **_kwargs: values
        ):
            global_plan = mapping_runtime.active_mapping_plan('HeroCustomers')
            self.assertEqual(['shared'], [item.definition_id for item in global_plan])
            selected = mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('shared', 'version'),
                                                           mapping_name='mapping-a')
            self.assertEqual(['shared', 'version'], [item.definition_id for item in selected])
            with self.assertRaises(mapping_runtime.MappingPlanError):
                mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('version',), mapping_name='mapping-b')

    def test_unwritable_native_field_blocks_previously_active_rule(self):
        from hero_integration.services.direct_mapping import SOURCE_CONTRACTS
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as controller

        row = {'definition_id': 'fixture-rule', 'personal_graphql_mapping': 'fixture-mapping',
               'operation': 'HeroCustomers', 'source_path': 'full_name', 'source_type': 'string|null',
               'target_doctype': 'Customer', 'target_field': 'alias', 'purpose': 'Field',
               'transformation': 'Identity', 'relation': '', 'priority': 5,
               'schema_snapshot':'snapshot', 'schema_fingerprint':'fingerprint'}
        fake = SimpleNamespace(db=lineage_db(row), get_all=lambda *_args, **_kwargs: [row],
                               get_doc=lambda *_args: SimpleNamespace(status='Active', direction='QUERY_TO_FRAPPE', schema_snapshot='snapshot', schema_fingerprint='fingerprint',
                                   mapping_json=json.dumps({'target_operation':'HeroCustomers', 'mapping_definition_ids':['fixture-rule']})))
        field = SimpleNamespace(fieldtype='Data', read_only=0, is_custom_field=0, hidden=0, fetch_from='', is_virtual=0)
        metadata = SimpleNamespace(field=lambda *_args: field, ensure_write=lambda _doctype: None)
        binding = ('full_name', 'string|null', 'Customer', 'alias', 'Data', 'Identity')
        with patch.dict(sys.modules, {'frappe': fake}), patch.object(controller, '_FrappeMetadata', return_value=metadata), patch.object(
            mapping_runtime, 'plan_from_definitions', side_effect=lambda _operation, values, **_kwargs: values
        ):
            with patch.dict(SOURCE_CONTRACTS['HeroCustomers'], {'native_field_bindings': frozenset({binding})}):
                result = mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('fixture-rule',), mapping_name='fixture-mapping')
                self.assertEqual(result[0].target_field, 'alias')
                shared = dict(row, personal_graphql_mapping=None)
                with patch.object(fake, 'get_all', return_value=[shared]):
                    with self.assertRaises(mapping_runtime.MappingPlanError):
                        mapping_runtime.active_mapping_plan('HeroCustomers')
            # A writable native field needs no hard-coded binding; a field that became read-only blocks the rule.
            field.read_only = 1
            with self.assertRaises(mapping_runtime.MappingPlanError):
                mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('fixture-rule',), mapping_name='fixture-mapping')

    def test_explicit_rule_from_other_snapshot_fails_closed(self):
        row = {'definition_id':'shared', 'personal_graphql_mapping':None, 'operation':'HeroCustomers',
               'source_path':'nr', 'source_type':'string|null', 'target_doctype':'Customer', 'target_field':'email',
               'purpose':'Field', 'transformation':'Identity', 'relation':'', 'priority':5,
               'schema_snapshot':'other', 'schema_fingerprint':'other-fingerprint'}
        fake = SimpleNamespace(get_all=lambda *_args, **_kwargs: [row],
                               get_doc=lambda *_args: SimpleNamespace(status='Active', direction='QUERY_TO_FRAPPE', schema_snapshot='snapshot', schema_fingerprint='fingerprint',
                                   mapping_json=json.dumps({'target_operation':'HeroCustomers', 'mapping_definition_ids':['shared']})))
        with patch.dict(sys.modules, {'frappe':fake}):
            with self.assertRaisesRegex(mapping_runtime.MappingPlanError, 'snapshot'):
                mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('shared',), mapping_name='mapping-a')

    def test_explicit_rule_ids_require_an_active_mapping_binding(self):
        row = {'definition_id':'shared', 'personal_graphql_mapping':None, 'operation':'HeroCustomers',
               'source_path':'nr', 'source_type':'string|null', 'target_doctype':'Customer', 'target_field':'email',
               'purpose':'Field', 'transformation':'Identity', 'relation':'', 'priority':5,
               'schema_snapshot':'snapshot', 'schema_fingerprint':'fingerprint'}
        fake = SimpleNamespace(get_all=lambda *_args, **_kwargs: [row],
            get_meta=lambda *_: SimpleNamespace(get_field=lambda *_: SimpleNamespace(is_custom_field=True)))
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(
            mapping_runtime, 'plan_from_definitions', return_value=object()
        ):
            with self.assertRaisesRegex(mapping_runtime.MappingPlanError, 'explicit active mapping'):
                mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('shared',))

    def test_duplicate_active_rows_cannot_satisfy_one_pinned_identity(self):
        row = {'definition_id':'shared', 'personal_graphql_mapping':None, 'operation':'HeroCustomers',
               'source_path':'nr', 'source_type':'string|null', 'target_doctype':'Customer', 'target_field':'email',
               'purpose':'Field', 'transformation':'Identity', 'relation':'', 'priority':5,
               'schema_snapshot':'snapshot', 'schema_fingerprint':'fingerprint'}
        fake = SimpleNamespace(get_all=lambda *_args, **_kwargs: [row, dict(row)],
                               get_doc=lambda *_: SimpleNamespace(status='Active', direction='QUERY_TO_FRAPPE',
                                   schema_snapshot='snapshot', schema_fingerprint='fingerprint',
                                   mapping_json=json.dumps({'target_operation':'HeroCustomers', 'mapping_definition_ids':['shared']})))
        with patch.dict(sys.modules, {'frappe':fake}):
            with self.assertRaisesRegex(mapping_runtime.MappingPlanError, 'unavailable or changed'):
                mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('shared',), mapping_name='mapping-a')

    def test_explicit_binding_rejects_a_different_target_operation(self):
        row = {'definition_id':'shared', 'personal_graphql_mapping':None, 'operation':'HeroCustomers',
               'source_path':'nr', 'source_type':'string|null', 'target_doctype':'Customer', 'target_field':'email',
               'purpose':'Field', 'transformation':'Identity', 'relation':'', 'priority':5,
               'schema_snapshot':'snapshot', 'schema_fingerprint':'fingerprint'}
        mapping = SimpleNamespace(status='Active', direction='QUERY_TO_FRAPPE', schema_snapshot='snapshot',
            schema_fingerprint='fingerprint', mapping_json=json.dumps({'mapping_definition_ids':['shared'],
                                                                        'target_operation':'HeroProjectMatches'}))
        fake = SimpleNamespace(get_all=lambda *_args, **_kwargs: [row], get_doc=lambda *_: mapping,
            get_meta=lambda *_: SimpleNamespace(get_field=lambda *_: SimpleNamespace(is_custom_field=True)))
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(
            mapping_runtime, 'plan_from_definitions', return_value=object()
        ):
            with self.assertRaisesRegex(mapping_runtime.MappingPlanError, 'target operation'):
                mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('shared',), mapping_name='mapping-a')

    def test_duplicate_json_binding_is_rejected(self):
        row = {'definition_id':'shared', 'personal_graphql_mapping':None, 'operation':'HeroCustomers',
               'source_path':'nr', 'source_type':'string|null', 'target_doctype':'Customer', 'target_field':'email',
               'purpose':'Field', 'transformation':'Identity', 'relation':'', 'priority':5,
               'schema_snapshot':'snapshot', 'schema_fingerprint':'fingerprint'}
        fake = SimpleNamespace(get_all=lambda *_args, **_kwargs: [row],
            get_doc=lambda *_args: SimpleNamespace(status='Active', direction='QUERY_TO_FRAPPE', schema_snapshot='snapshot', schema_fingerprint='fingerprint',
                mapping_json='{"mapping_definition_ids":["other"],"mapping_definition_ids":["shared"]}'))
        with patch.dict(sys.modules, {'frappe':fake}):
            with self.assertRaisesRegex(mapping_runtime.MappingPlanError, 'binding is invalid'):
                mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('shared',), mapping_name='mapping-a')

    def test_explicit_rule_rejects_inactive_or_wrong_direction_mapping(self):
        row = {'definition_id':'shared', 'personal_graphql_mapping':None, 'operation':'HeroCustomers',
               'source_path':'nr', 'source_type':'string|null', 'target_doctype':'Customer', 'target_field':'email',
               'purpose':'Field', 'transformation':'Identity', 'relation':'', 'priority':5,
               'schema_snapshot':'snapshot', 'schema_fingerprint':'fingerprint'}
        mapping = SimpleNamespace(status='Draft', direction='QUERY_TO_FRAPPE')
        fake = SimpleNamespace(get_all=lambda *_args, **_kwargs: [row], get_doc=lambda *_args: mapping)
        with patch.dict(sys.modules, {'frappe':fake}):
            for status, direction in [('Draft','QUERY_TO_FRAPPE'), ('Active','FRAPPE_TO_MUTATION')]:
                mapping.status, mapping.direction = status, direction
                with self.assertRaisesRegex(mapping_runtime.MappingPlanError, 'active Query'):
                    mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('shared',), mapping_name='mapping-a')

    def test_scoped_revision_rejects_tampered_content_fingerprint(self):
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as controller
        row = {'definition_id':'version', 'personal_graphql_mapping':'mapping-a', 'operation':'HeroCustomers',
               'source_path':'nr', 'source_type':'string|null', 'target_doctype':'Customer', 'target_field':'email',
               'purpose':'Field', 'transformation':'Identity', 'relation':'', 'priority':5,
               'schema_snapshot':'snapshot', 'schema_fingerprint':'fingerprint'}
        db = lineage_db(row)
        tampered = dict(row, target_field='other')
        fake = SimpleNamespace(db=db, get_all=lambda *_args, **_kwargs: [tampered],
            get_doc=lambda *_args: SimpleNamespace(status='Active', direction='QUERY_TO_FRAPPE',
                schema_snapshot='snapshot', schema_fingerprint='fingerprint',
                mapping_json=json.dumps({'target_operation':'HeroCustomers', 'mapping_definition_ids':['version']})))
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(controller, '_FrappeMetadata'):
            with self.assertRaisesRegex(mapping_runtime.MappingPlanError, 'lineage is missing or inconsistent'):
                mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('version',), mapping_name='mapping-a')
