"""Compatible schema extensions retain the approved mapping context."""
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from graphql import build_schema, introspection_from_schema
from hero_integration.services import mapping_import_context as context
from hero_integration.services.mapping_schema_compatibility import MappingSchemaCompatibilityError
from hero_integration.services.mapping_snapshot_rebase import SnapshotRebaseError


def schema(extra='', title='String'):
    return introspection_from_schema(build_schema('''
        type Task { id: Int title: '''+title+''' }
        input TaskInput { id: Int title: String }
        type Query { tasks: [Task] '''+extra+''' }
        type Mutation { update_task(task: TaskInput): Task }
    '''))['__schema']


def profile():
    return {'usage':'MAPPED_UPDATE_V2','target':{'operation':'update_task'},
            'identity':{'input_path':'task.id'},'assignments':[{'input_path':'task.title'}],'required_readback':[]}


class TestCompatibleImportContext(TestCase):
    def setUp(self):
        self.schemas={'old':schema(),'new':schema('automation: Boolean')}
        self.docs={name: {'endpoint_fingerprint':'same-endpoint'} for name in self.schemas}
        self.rows=[NS(name='map',schema_snapshot='old',direction='FRAPPE_TO_MUTATION',mapping_json=json.dumps(profile()))]
        def snapshot(name,*args,**kwargs):
            from frappe import _dict
            return _dict(self.docs[name]),self.schemas[name],name+'-fingerprint'
        def documents(doctype,filters):
            if doctype==context.MAPPING:return self.rows
            if doctype==context.SERVICE:return []
            if doctype==context.CONTRACT:return []
            raise AssertionError(doctype)
        self.fake=NS(only_for=Mock(),get_all=Mock(return_value=self.rows))
        patches=[patch.object(context,'frappe',self.fake),patch.object(context,'_snapshot',side_effect=snapshot),
                 patch.object(context,'_documents',side_effect=documents),
                 patch('hero_integration.services.mapping_draft_exchange._lock_context')]
        for p in patches:p.start();self.addCleanup(p.stop)

    def payload(self,fp='old-fingerprint'):
        return json.dumps({'format_version':'hero-personal-mapping-portable-v2','direction':'FRAPPE_TO_MUTATION',
                           'schema_fingerprint':fp,'mappings':[{'content':profile()}]})

    def test_files_from_unknown_snapshots_and_endpoints_use_current_target(self):
        self.docs['new']['endpoint_fingerprint'] = 'another-customer'
        for fp in ('old-fingerprint', 'new-fingerprint', 'unknown-exporting-snapshot'):
            resolved = context.resolve('new', 'source', self.payload(fp))
            self.assertEqual(resolved['snapshot_name'], 'new')
            self.assertEqual(json.loads(resolved['payload'])['schema_fingerprint'], 'new-fingerprint')
            self.assertEqual(resolved['validated_against_snapshot'], 'new')
        self.fake.get_all.assert_not_called()

    def test_incoming_query_is_bound_to_current_schema_for_owning_validation(self):
        self.schemas['new'] = schema('automation: Boolean', title='Int')
        package = {'format_version': 'hero-integration-mappings-v1', 'schema_fingerprint': 'foreign',
            'mapping_package': {'schema_fingerprint': 'foreign'},
            'read_services': [{'query': 'query{tasks{id title}}'}]}
        resolved = context.resolve('new', 'source', json.dumps(package))
        payload = json.loads(resolved['payload'])
        self.assertEqual(resolved['snapshot_name'], 'new')
        self.assertEqual(payload['schema_fingerprint'], 'new-fingerprint')
        self.assertEqual(payload['mapping_package']['schema_fingerprint'], 'new-fingerprint')
        self.assertEqual(payload['read_services'], package['read_services'])

    def test_schema_dependency_changes_still_block_export_of_old_context(self):
        self.schemas['new'] = schema(title='Int')
        with patch.object(context, 'check_compatibility', side_effect=MappingSchemaCompatibilityError(['Task.title'])):
            with self.assertRaises(MappingSchemaCompatibilityError):
                context.resolve('new', 'source')

    def test_no_active_mapping_or_same_snapshot_import_still_binds_file_to_target(self):
        resolved = context.resolve('old', 'source', self.payload('foreign'))
        self.assertEqual(json.loads(resolved['payload'])['schema_fingerprint'], 'old-fingerprint')
        self.assertEqual(resolved['snapshot_name'], 'old')

    def test_export_resolves_the_same_approved_context(self):
        self.assertEqual(context.resolve('new','source')['snapshot_name'],'old')

    def test_non_manager_cannot_probe_schema_context(self):
        self.fake.only_for.side_effect=PermissionError('denied')
        with patch.object(context,'_snapshot') as snapshot,self.assertRaises(PermissionError):
            context.resolve('new','source',self.payload())
        snapshot.assert_not_called()
