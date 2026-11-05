"""Import before credentials: only confirmed live schema proof binds provenance."""
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch
import json

from graphql import build_schema, introspection_from_schema
from hero_integration.services import schema_refresh as refresh, config_exchange as exchange
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.introspection import endpoint_fingerprint

RAW = introspection_from_schema(build_schema('type Query { value: String }'))
FP = analyze_snapshot(RAW).fingerprint


class TestOfflineSnapshotBinding(TestCase):
    def setUp(self):
        self.config = NS(source_instance='source-a', endpoint=NS(url='https://hero.example.invalid/graphql'),
                         api_token='synthetic-token', timeout_seconds=10, source_binding_confirmed=True)
        self.offline = refresh.offline_endpoint_fingerprint('source-a')
        self.expected = endpoint_fingerprint(self.config.endpoint.url, 'source-a')
        self.snapshot = NS(name='offline-snapshot', source_instance='source-a', endpoint_fingerprint=self.offline,
            schema_fingerprint=FP, snapshot_json=json.dumps(RAW), analysis_status='Validated',
            execution_contract_status='Compatible', save=Mock())

    def fake_site(self):
        def rows(doctype, filters, **kwargs):
            return ['offline-snapshot'] if filters.get('endpoint_fingerprint') == self.offline else []
        return NS(get_all=Mock(side_effect=rows), get_doc=Mock(return_value=self.snapshot))

    def test_live_capture_binds_the_existing_offline_snapshot_without_replacing_definitions(self):
        fake = self.fake_site()
        client = NS(introspect=Mock(return_value={'data': RAW}))
        with patch.object(refresh, 'frappe', fake), \
             patch('hero_integration.client.settings.get_configuration', return_value=self.config), \
             patch('hero_integration.client.graphql.HeroGraphQLClient', return_value=client):
            result = refresh._capture('Administrator')
        self.assertEqual(result['snapshot_name'], 'offline-snapshot')
        self.assertTrue(result['endpoint_bound'])
        self.assertEqual(self.snapshot.endpoint_fingerprint, self.expected)
        self.snapshot.save.assert_called_once_with()
        fake.get_doc.assert_called_once_with(refresh.SNAPSHOT, 'offline-snapshot', for_update=True)
        client.introspect.assert_called_once()

    def test_unconfirmed_or_different_real_endpoint_is_never_rebound(self):
        fake = self.fake_site()
        self.config.source_binding_confirmed = False
        with patch.object(refresh, 'frappe', fake):
            self.assertIsNone(refresh._bind_offline_snapshot(self.config, FP, 1))
        fake.get_all.assert_not_called()
        self.config.source_binding_confirmed = True
        self.snapshot.endpoint_fingerprint = 'different-real-endpoint'
        with patch.object(refresh, 'frappe', fake), self.assertRaises(refresh.SchemaRefreshError):
            refresh._bind_offline_snapshot(self.config, FP, 1)
        self.snapshot.save.assert_not_called()

    def test_changed_imported_schema_payload_is_not_acknowledged(self):
        self.snapshot.snapshot_json = json.dumps(introspection_from_schema(build_schema('type Query { other: Int }')))
        with patch.object(refresh, 'frappe', self.fake_site()), self.assertRaisesRegex(refresh.SchemaRefreshError, 'fingerprint'):
            refresh._bind_offline_snapshot(self.config, FP, 1)
        self.snapshot.save.assert_not_called()

    def test_offline_capture_cannot_masquerade_as_live_source_proof(self):
        fake = NS(get_all=Mock(return_value=[]), get_doc=Mock(return_value=NS(name='new', insert=Mock(), schema_fingerprint=FP, execution_contract_status='Compatible')))
        with patch.object(refresh, 'frappe', fake), patch.object(refresh, '_offline_configuration', return_value=self.config), \
             patch.object(refresh, '_bind_offline_snapshot', side_effect=AssertionError('offline binding forbidden')), \
             patch('hero_integration.services.snapshot_inheritance.parent_for', return_value=None):
            result = refresh._capture('Administrator', response={'data': RAW})
        self.assertEqual(result['status'], 'CAPTURED')
        self.assertEqual(self.snapshot.endpoint_fingerprint, self.offline)

    def test_import_lookup_prefers_local_endpoint_and_keeps_offline_pending(self):
        binding = {'source_instance': 'source-a', 'endpoint_fingerprint': self.expected}
        def rows(doctype, filters, **kwargs):
            self.assertEqual(filters['source_instance'], 'source-a')
            if filters.get('endpoint_fingerprint') == self.expected: return []
            if filters.get('endpoint_fingerprint') == self.offline: return ['offline-snapshot']
            raise AssertionError('unscoped snapshot query')
        with patch.object(exchange, 'frappe', NS(get_all=Mock(side_effect=rows))):
            names, state = exchange._package_snapshot_names(FP, binding)
        self.assertEqual(names, ['offline-snapshot'])
        self.assertEqual(state, 'Pending source verification')

    def test_foreign_bound_snapshot_is_not_reused_by_package_or_explicit_selection(self):
        binding = {'source_instance': 'source-a', 'endpoint_fingerprint': self.expected}
        fake = NS(get_all=lambda dt, filters, **kw: [] if 'endpoint_fingerprint' in filters else ['foreign'])
        with patch.object(exchange, 'frappe', fake):
            self.assertEqual(exchange._package_snapshot_names(FP, binding), ([], 'New'))
        self.snapshot.endpoint_fingerprint = 'different-real-endpoint'
        with patch.object(exchange, 'frappe', NS(get_doc=Mock(return_value=self.snapshot))), \
             patch.object(exchange, '_snapshot_binding', return_value=binding), self.assertRaisesRegex(exchange.ConfigExchangeError, 'endpoint'):
            exchange._source_snapshot('offline-snapshot', {}, 'source-a')

    def test_reader_catalog_skips_a_newer_snapshot_for_the_wrong_endpoint(self):
        from hero_integration import api
        candidates = [{'name':'foreign-reader','read_mapping':'foreign-map','schema_snapshot':'foreign','modified':'2'},
                      {'name':'local-reader','read_mapping':'local-map','schema_snapshot':'local','modified':'1'}]
        def value(dt, name, fields, as_dict=True):
            if dt == 'HERO GraphQL Mapping':
                return {'status':'Active','mapping_json':json.dumps({'target_operation':'HeroCustomers'})}
            return {'source_instance':'source-a','endpoint_fingerprint':self.expected if name=='local' else self.offline,
                    'creation':'2' if name=='foreign' else '1'}
        fake = NS(get_all=Mock(return_value=candidates),db=NS(get_value=value),get_doc=Mock(side_effect=lambda dt,name:NS(name=name)))
        binding = {'source_instance':'source-a','endpoint_fingerprint':self.expected}
        with patch.object(api,'frappe',fake),patch.object(refresh,'local_snapshot_binding',return_value=binding), \
             patch('hero_integration.services.read_contract.from_documents') as validate:
            result = api._sync_area_read_services('source-a')
        self.assertEqual(result['HeroCustomers'][0]['service'],'local-reader')
        self.assertEqual(validate.call_count,1)
        self.assertEqual(validate.call_args.args[0].name,'local-reader')

    def test_employee_and_service_metrics_count_only_the_configured_source_targets(self):
        from hero_integration import api
        counts = []
        def count(dt, filters):
            counts.append((dt,filters))
            if dt == 'File': return 0
            self.assertEqual(filters['source_instance'],'source-a')
            self.assertEqual(filters['sync_status'],['!=','Source Deleted'])
            if filters['source_type']=='partner':
                self.assertEqual(filters['target_doctype'],'Employee')
                return 5
            if filters['source_type']=='supply_service':return 3
            return 1
        with patch.object(api,'frappe',NS(db=NS(count=count))):
            records = api._direct_area_records('source-a')
        self.assertEqual((records['employees'],records['services']),(5,3))
        self.assertEqual(len(counts),5)
