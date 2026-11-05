"""Combined configuration keeps writeback definitions portable and unapproved."""
from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from hero_integration.services import config_mutation_drafts as drafts
from hero_integration.services import mapping_draft_exchange as exchange
from hero_integration.services import config_exchange as config
from hero_integration.tests.test_mapped_update_v2 import mapping, SCHEMA


def package():
    mutation={'definition_id':str(uuid4()),'operation_kind':'MUTATION','operation_name':'update_task',
              'direction':'FRAPPE_TO_MUTATION','content':mapping(),'dependencies':[]}
    return {'format_version':config.FORMAT_VERSION,'replace_mappings':True,
            'schema_snapshot':{'schema_fingerprint':'0'*64,'schema':SCHEMA},'settings':{},'mapping_package':{},
            'read_services':[{'target_operation':'HeroTasks','service_name':'Tasks','operation_name':'tasks',
                'mapping_operation_name':'tasks','query':'query {tasks{id name}}','mapping':{}}],
            'mutation_mappings':[mutation]}


class TestCombinedMutationConfiguration(TestCase):
    def test_closed_configuration_preserves_prepared_updates(self):
        value=package()
        self.assertEqual(config._parse(json.dumps(value)),value)
        self.assertEqual(drafts.summary(value)['mutation_mappings'],{'count':1,'state':'Active','execution':False,'assignments':1})
        for change in ('duplicate','foreign_query','not_replacement','execution_flag'):
            data=deepcopy(value)
            if change=='duplicate':
                child=deepcopy(data['mutation_mappings'][0]);child['definition_id']=str(uuid4());data['mutation_mappings'].append(child)
            elif change=='foreign_query':data['read_services'][0]['target_operation']='HeroCustomers'
            elif change=='not_replacement':data['replace_mappings']=False
            else:data['mutation_mappings'][0]['content']['execute']=True
            with self.subTest(change=change),self.assertRaises(ValueError):config._parse(json.dumps(data))

    def test_import_publishes_reviewed_mappings_without_execution(self):
        value=package();identity=value['mutation_mappings'][0]['definition_id']
        expected={'generation':0,'draft_revision':0,'active_revision':0,'reconciliation_revision':0}
        preview={'can_save':True,'preview_fingerprint':'reviewed','items':[{'definition_id':identity,'expected':expected}]}
        with patch.object(exchange,'preview_import',return_value=preview),patch.object(exchange,'save_import',return_value={'activation':False}) as save, \
             patch('hero_integration.services.simple_mapping_exchange.publish_imported_mappings', return_value=[{'name':'active'}]) as publish:
            result=drafts.save(value,'source-a','snapshot')
        self.assertTrue(result['mutation_import']['activation'])
        self.assertFalse(result['mutation_mappings']['execution'])
        publish.assert_called_once_with(preview['items'])
        args,kwargs=save.call_args
        self.assertEqual(args[4],[{'definition_id':identity,**expected}])
        self.assertEqual(kwargs,{'confirmed':True,'preview_fingerprint':'reviewed'})

    def test_conflicting_draft_cannot_be_silently_overwritten(self):
        with patch.object(exchange,'preview_import',return_value={'can_save':False}),patch.object(exchange,'save_import') as save:
            with self.assertRaisesRegex(ValueError,'conflicts'):drafts.save(package(),'source-a','snapshot')
        save.assert_not_called()

    def test_old_query_only_configuration_stays_query_only(self):
        value=package();value.pop('mutation_mappings')
        self.assertEqual(drafts.summary(value),{})
        self.assertEqual(drafts.save(value,'source','snapshot'),{})
        self.assertEqual(config._parse(json.dumps(value)),value)


class TestCombinedMutationMetadataPreview(TestCase):
    def check(self, field):
        from types import SimpleNamespace as NS
        from hero_integration.services import schema_provisioning
        value=package();value['read_services']=[];value['mapping_package']['schema']={}
        metadata=NS(field=lambda *_args:field)
        with patch.object(schema_provisioning,'preview_schema',return_value={'items':[],'counts':{}}), \
             patch.object(schema_provisioning,'_definitions',return_value={}), \
             patch.object(schema_provisioning,'_PlannedMetadata',return_value=metadata):
            return config._replacement_check(NS(snapshot_json=json.dumps(SCHEMA)),value,{})

    def test_missing_or_readonly_mutation_source_fails_before_provisioning(self):
        from types import SimpleNamespace as NS
        with self.assertRaisesRegex(ValueError,'absent'):self.check(None)
        field=NS(fieldtype='Data',options='',read_only=1)
        with self.assertRaisesRegex(ValueError,'not editable'):self.check(field)

    def test_declared_editable_source_is_checked_using_planned_metadata(self):
        from types import SimpleNamespace as NS
        self.assertEqual(self.check(NS(fieldtype='Data',options='',read_only=0)),{'items':[],'counts':{}})
