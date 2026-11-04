"""Mapping routes cannot import or export system configuration."""
from copy import deepcopy
from types import SimpleNamespace as NS
import json
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import mapping_only_exchange as mapping
from hero_integration.services import config_exchange as config
from hero_integration import api

FP='a'*64


def package():
    return {'format_version':mapping.FORMAT_VERSION,'schema_fingerprint':FP,'replace_mappings':True,
        'mapping_package':{'schema_fingerprint':FP,'schema':{},'mappings':[]},
        'read_services':[{'target_operation':'HeroCustomers','service_name':'Customers','operation_name':'contacts',
            'mapping_operation_name':'contacts','query':'query{contacts{id}}','mapping':{}}]}


class TestMappingOnlyFormat(unittest.TestCase):
    def test_forbidden_configuration_sections_never_reach_import(self):
        value=package()
        self.assertEqual(mapping.parse_file(json.dumps(value)),value)
        for key in ('settings','schema_snapshot','mutation_contracts','mutation_mappings','hero_api_token','headers'):
            modified=deepcopy(value);modified[key]={'secret':'canary'}
            with self.subTest(key=key), self.assertRaises(mapping.MappingOnlyError):mapping.parse_file(json.dumps(modified))
        modified=deepcopy(value);modified['format_version']=config.FORMAT_VERSION
        with self.assertRaises(mapping.MappingOnlyError):mapping.parse_file(json.dumps(modified))

    def test_schema_identity_duplicates_and_nonfinite_values_are_rejected(self):
        for raw in (json.dumps({**package(),'schema_fingerprint':'b'*64}),
                    json.dumps(package()).replace('"replace_mappings": true','"replace_mappings": true,"replace_mappings": true'),
                    json.dumps(package()).replace('true','NaN')):
            with self.assertRaises(mapping.MappingOnlyError):mapping.parse_file(raw)

    def test_internal_configuration_has_no_settings_and_uses_only_target_schema(self):
        snapshot=NS(schema_fingerprint=FP,snapshot_json=json.dumps({'__schema':{}}))
        with patch.object(config,'_parse'):
            prepared=json.loads(mapping._configuration(package(),snapshot))
        self.assertEqual(prepared['settings'],{})
        self.assertNotIn('mutation_mappings',prepared)
        self.assertNotIn('mutation_contracts',prepared)
        with self.assertRaises(mapping.MappingOnlyError):mapping._configuration(package(),NS(schema_fingerprint='b'*64))


class TestMappingOnlyWorkflow(unittest.TestCase):
    def setUp(self):
        self.snapshot=NS(name='snapshot',schema_fingerprint=FP,snapshot_json=json.dumps({'__schema':{}}))
        self.fake=NS(only_for=Mock())
        for p in [patch.object(mapping,'frappe',self.fake),patch.object(mapping,'_snapshot',return_value=self.snapshot),
                  patch.object(config,'_parse'),patch.object(config,'preview_config',return_value={'settings':[], 'read_services':[], 'replace_mappings':True}),
                  patch.object(mapping,'_target_state',return_value='state'),
                  patch('hero_integration.services.mapping_draft_exchange._preview_binding',return_value='proof')]:
            p.start();self.addCleanup(p.stop)

    def test_preview_is_mapping_only_and_never_writes(self):
        with patch.object(config,'import_config') as save:
            result=mapping.preview(json.dumps(package()),'snapshot','source')
        self.assertNotIn('settings',result)
        self.assertEqual(result['format_version'],mapping.FORMAT_VERSION)
        self.assertEqual(result['preview_fingerprint'],'proof')
        self.assertFalse(result['execution']);save.assert_not_called()

    def test_apply_requires_exact_confirmation_and_current_fingerprint(self):
        with patch.object(config,'import_config',return_value={'settings':[], 'read_services':[]}) as save:
            with self.assertRaises(mapping.MappingOnlyError):mapping.apply(json.dumps(package()),'snapshot','source','proof')
            with self.assertRaises(mapping.MappingOnlyError):mapping.apply(json.dumps(package()),'snapshot','source','old',confirmed=True)
            save.assert_not_called()
            result=mapping.apply(json.dumps(package()),'snapshot','source','proof',confirmed=True)
        self.assertTrue(result['activation']);self.assertNotIn('settings',result)
        self.assertEqual(json.loads(save.call_args.args[1])['settings'],{})

    def test_export_uses_mapping_only_path_without_settings_reader(self):
        with patch.object(config,'export_config',return_value='mapping') as export:
            self.assertEqual(mapping.export('snapshot','source'),'mapping')
        export.assert_called_once_with('snapshot','source',mappings_only=True)


class TestMappingOnlyAPIRoutes(unittest.TestCase):
    def test_query_uses_mapping_service_instead_of_settings_import(self):
        fake=NS(form_dict={},only_for=Mock())
        with patch.object(api,'frappe',fake),patch.object(api,'_configured_source_instance',return_value='source'), \
             patch('hero_integration.services.mapping_only_exchange.preview',return_value={'only':'mapping'}) as preview:
            self.assertEqual(api.simple_mapping_import_preview('file','snapshot','QUERY_TO_FRAPPE'),{'only':'mapping'})
        preview.assert_called_once_with('file','snapshot','source')


class TestConfigurationSecretBoundary(unittest.TestCase):
    def test_allowlisted_settings_export_excludes_declared_password_fields(self):
        values={key:'ordinary' for key in config.SETTINGS_KEYS}
        values.update(hero_api_token='secret-token-canary',password='password-canary',encryption_key='encryption-canary')
        values['timeout_seconds']='custom-password-canary'
        settings=NS(get=lambda key:values.get(key),meta=NS(get_field=lambda key:NS(fieldtype='Password' if key=='timeout_seconds' else 'Data')))
        exported=config._export_settings(settings)
        self.assertNotIn('timeout_seconds',exported)
        self.assertFalse({'hero_api_token','password','encryption_key'}&set(exported))
        self.assertNotIn('canary',json.dumps(exported))

    def test_mapping_services_cannot_smuggle_configuration_sections(self):
        for key in ('settings','headers','password','mutation_contracts'):
            value=package();value['read_services'][0]['mapping'][key]='secret-canary'
            with self.subTest(key=key),self.assertRaises(mapping.MappingOnlyError):mapping.parse_file(json.dumps(value))


class TestMappingConfirmationConcurrency(TestMappingOnlyWorkflow):
    def test_changed_target_after_preview_is_rejected_under_write_lock(self):
        def state(_source, _operations, *, lock=False):return 'changed' if lock else 'reviewed'
        written=[]
        def save(_snapshot, _payload, _source, *, before_mapping_write):
            before_mapping_write(self.snapshot)
            written.append(True)
            return {}
        with patch.object(mapping,'_target_state',side_effect=state),patch.object(config,'import_config',side_effect=save):
            with self.assertRaisesRegex(mapping.MappingOnlyError,'preview changed'):
                mapping.apply(json.dumps(package()),'snapshot','source','proof',confirmed=True)
        self.assertEqual(written,[])

    def test_locked_state_reads_are_parameterized_current_reads(self):
        sql=Mock(return_value=[])
        fake=NS(db=NS(sql=sql))
        with patch.object(mapping,'frappe',fake):
            mapping._state_rows('HERO GraphQL Mapping',['name'],{'source_instance':"source'probe"},lock=True)
        query,values=sql.call_args.args
        self.assertIn('FOR UPDATE',query)
        self.assertNotIn("source'probe",query)
        self.assertEqual(values,["source'probe"])
