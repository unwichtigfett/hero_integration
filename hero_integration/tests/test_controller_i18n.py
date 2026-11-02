from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.hero_integration.doctype.hero_integration_settings import hero_integration_settings as settings
from hero_integration.hero_integration.doctype.hero_mutation_contract import hero_mutation_contract as contract
from hero_integration.services.graphql_contract import GraphQLContractError


class TestControllerTranslations(unittest.TestCase):
    def test_settings_validation_translates_error_and_keeps_validation(self):
        messages = {'Schema refresh interval must be between {0} and {1} minutes.': 'Intervall {0} bis {1}.'}
        fake = SimpleNamespace(only_for=lambda role: None, _=lambda value: messages.get(value, value))
        document = SimpleNamespace(_update_source_binding=lambda: None, hero_api_url='')
        with patch.object(settings, 'frappe', fake), patch.object(settings, 'refresh_interval', return_value=0):
            with self.assertRaisesRegex(ValueError, '^Intervall '):
                settings.HEROIntegrationSettings.validate(document)

    def test_contract_translates_nested_graphql_error_without_changing_validation(self):
        source = 'Required GraphQL variable ${0} is missing.'
        messages = {'Invalid HERO mutation contract: {0}': 'Ungültiger Vertrag: {0}',
                    source: 'GraphQL-Variable ${0} fehlt.'}
        service = SimpleNamespace(schema_snapshot='SNAP-1', query_document='mutation X { x }')
        snapshot = SimpleNamespace(analysis_status='Validated', execution_contract_status='Ready')
        def get_doc(doctype, name): return service if doctype == 'HERO GraphQL Service' else snapshot
        def throw(message): raise ValueError(message)
        fake = SimpleNamespace(only_for=lambda role: None, get_doc=get_doc,
                               _=lambda value: messages.get(value, value), throw=throw)
        document = SimpleNamespace(graphql_service='SERVICE-1', mapping_json='{}')
        with patch.object(contract, 'frappe', fake), patch.object(contract, 'parse_mutation',
                side_effect=GraphQLContractError(source, 'target_id')):
            with self.assertRaisesRegex(ValueError, r'^Ungültiger Vertrag: GraphQL-Variable \$target_id fehlt\.$'):
                contract.HEROMutationContract.validate(document)
