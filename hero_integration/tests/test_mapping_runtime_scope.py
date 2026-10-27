import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import mapping_runtime


class TestMappingRuntimeScope(unittest.TestCase):
    def test_personal_versions_require_their_mapping_and_do_not_change_global_plan(self):
        def row(identity, scope):
            return {'definition_id': identity, 'personal_graphql_mapping': scope, 'operation': 'HeroCustomers',
                    'source_path': 'nr', 'source_type': 'string|null', 'target_doctype': 'Customer',
                    'target_field': identity, 'purpose': 'Field', 'transformation': 'Identity',
                    'relation': '', 'priority': 5}
        fake = SimpleNamespace(get_all=lambda *_args, **_kwargs: [row('shared', None), row('version', 'mapping-a')])
        with patch.dict(sys.modules, {'frappe': fake}), patch.object(
            mapping_runtime, 'plan_from_definitions', side_effect=lambda _operation, values, **_kwargs: values
        ):
            global_plan = mapping_runtime.active_mapping_plan('HeroCustomers')
            self.assertEqual(['shared'], [item.definition_id for item in global_plan])
            selected = mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('shared', 'version'),
                                                           mapping_name='mapping-a')
            self.assertEqual(['shared', 'version'], [item.definition_id for item in selected])
            with self.assertRaises(mapping_runtime.MappingPlanError):
                mapping_runtime.active_mapping_plan('HeroCustomers', definition_ids=('version',), mapping_name='mapping-b')
