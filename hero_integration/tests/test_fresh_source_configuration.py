"""Fresh source reads own a short primary snapshot, never the request transaction."""
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.client import settings


def rows():
    values = {'hero_api_url': 'https://example.invalid/graphql', 'allowed_hero_hosts': 'example.invalid',
              'timeout_seconds': '30', 'hero_source_instance': 'source-a',
              'source_binding_revision': '4', 'source_binding_confirmed_revision': '4'}
    return [NS(field=key, value=value, encrypted_token='encrypted-synthetic') for key, value in values.items()]


class TestFreshSourceConfiguration(TestCase):
    def setUp(self):
        self.events = []
        self.connection = NS(begin=Mock(side_effect=lambda **kw: self.events.append('begin')),
            sql=Mock(side_effect=lambda *a, **kw: (self.events.append('read') or rows())),
            close=Mock(side_effect=lambda: self.events.append('close')))
        self.request = NS(commit=Mock(), rollback=Mock(), sql=Mock())
        self.frappe = NS(db=self.request, get_single=Mock(side_effect=AssertionError('stale request reader')),
                         local=NS(conf={'db_name': 'site-db', 'db_user': 'site-user', 'db_password': 'synthetic',
                              'db_host': 'primary.invalid', 'db_port': 3306, 'replica_host': 'replica.invalid',
                              'db_socket': None, 'encryption_key': 'synthetic-key'}))

    def load(self):
        with patch.object(settings, 'frappe', self.frappe), \
                patch.object(settings, '_configuration_connection', return_value=self.connection), \
                patch('frappe.utils.password.decrypt', side_effect=self.decrypt):
            return settings.get_configuration(fresh=True)

    def decrypt(self, value, **kwargs):
        self.assertEqual(self.events[-1], 'close')
        self.assertEqual(kwargs['encryption_key'], 'synthetic-key')
        return 'synthetic-token'

    def test_one_consistent_read_is_closed_before_configuration_can_reach_transport(self):
        configuration = self.load()
        self.assertEqual(self.events, ['begin', 'read', 'close'])
        self.assertEqual(configuration.source_instance, 'source-a')
        self.assertEqual(configuration.source_binding_revision, 4)
        self.assertTrue(configuration.source_binding_confirmed)
        self.assertNotIn(configuration.api_token, repr(configuration))
        self.connection.begin.assert_called_once_with(read_only=True)
        query, args = self.connection.sql.call_args.args
        self.assertIn('tabSingles', query); self.assertIn('__Auth', query)
        self.assertNotIn('FOR UPDATE', query.upper())
        self.assertNotIn('LOCK IN SHARE MODE', query.upper())
        self.assertEqual(args[0], 'HERO Integration Settings')
        self.connection.sql.assert_called_once()
        self.request.commit.assert_not_called(); self.request.rollback.assert_not_called()
        self.request.sql.assert_not_called(); self.frappe.get_single.assert_not_called()

    def test_read_failure_closes_only_its_own_connection_and_fails_closed(self):
        self.connection.sql.side_effect = RuntimeError('private connection details')
        with self.assertRaisesRegex(settings.HeroConfigurationError, '^The current HERO connection could not be verified.$'):
            self.load()
        self.connection.close.assert_called_once()
        self.request.commit.assert_not_called(); self.request.rollback.assert_not_called()

    def test_primary_factory_ignores_the_request_replica_and_keeps_request_identity(self):
        with patch.object(settings, 'frappe', self.frappe), patch('frappe.database.get_db') as factory:
            settings._configuration_connection()
        factory.assert_called_once_with(socket=None, host='primary.invalid', port=3306,
            user='site-user', password='synthetic', cur_db_name='site-db')
        self.assertIs(self.frappe.db, self.request)

    def test_missing_token_or_ambiguous_settings_never_return_a_configuration(self):
        for returned in ([], rows() + rows()[:1], [NS(field=r.field,value=r.value,encrypted_token=None) for r in rows()]):
            self.connection.sql.side_effect = None; self.connection.sql.return_value = returned
            with self.subTest(count=len(returned)), self.assertRaises(settings.HeroConfigurationError):
                self.load()
        self.assertEqual(self.connection.close.call_count, 3)
