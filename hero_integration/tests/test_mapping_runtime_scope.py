import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import mapping_runtime


class TestMappingRuntimeScope(unittest.TestCase):
    def test_personal_versions_require_their_mapping_and_do_not_change_global_plan(self):
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as controller

        def row(identity, scope):
            return {'definition_id': identity, 'personal_graphql_mapping': scope, 'operation': 'HeroCustomers',
                    'source_path': 'nr', 'source_type': 'string|null', 'target_doctype': 'Customer',
                    'target_field': identity, 'purpose': 'Field', 'transformation': 'Identity',
                    'relation': '', 'priority': 5}
        fake = SimpleNamespace(get_all=lambda *_args, **_kwargs: [row('shared', None), row('version', 'mapping-a')],
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

    def test_revoked_native_binding_blocks_previously_active_rule(self):
        from hero_integration.services.direct_mapping import SOURCE_CONTRACTS
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as controller

        row = {'definition_id': 'fixture-rule', 'personal_graphql_mapping': 'fixture-mapping',
               'operation': 'HeroCustomers', 'source_path': 'full_name', 'source_type': 'string|null',
               'target_doctype': 'Customer', 'target_field': 'alias', 'purpose': 'Field',
               'transformation': 'Identity', 'relation': '', 'priority': 5}
        fake = SimpleNamespace(get_all=lambda *_args, **_kwargs: [row])
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
            with patch.dict(SOURCE_CONTRACTS['HeroCustomers'], {'native_field_bindings': frozenset()}):
                with self.assertRaises(mapping_runtime.MappingPlanError):
                    mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('fixture-rule',), mapping_name='fixture-mapping')
