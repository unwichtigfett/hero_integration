import builtins
import unittest
from unittest.mock import patch

from hero_integration.client.graphql import HeroClientError, HeroGraphQLClient, HeroEndpoint, assert_read_only


class TestGraphQLReadBoundary(unittest.TestCase):
    def test_rejects_non_query_and_multi_operation_documents_before_transport(self):
        documents = (
            'mutation($id:ID!){delete(id:$id)}',
            'subscription($id:ID!){updates(id:$id){id}}',
            'query Q { viewer { id } } mutation M($id:ID!){delete(id:$id)}',
            'query A { viewer { id } } query B { viewer { id } }',
            'fragment Fields on User { id }',
            'query Q {',
        )
        client = HeroGraphQLClient(HeroEndpoint('https://example.invalid/', 'example.invalid'), lambda: 'test')
        for document in documents:
            with self.subTest(document=document), patch.object(client, '_post') as post:
                with self.assertRaises(HeroClientError):
                    client.query(document, {})
                post.assert_not_called()

    def test_accepts_query_with_fragments_and_operation_words_in_string(self):
        assert_read_only('query Q { viewer(filter:"mutation") { ...Fields } } fragment Fields on User { id }')
        assert_read_only('{ viewer { id } }')

    def test_missing_graphql_parser_fails_closed_before_transport(self):
        original_import = builtins.__import__

        def import_without_parser(name, *args, **kwargs):
            if name == "graphql":
                raise ImportError("synthetic missing parser")
            return original_import(name, *args, **kwargs)

        client = HeroGraphQLClient(HeroEndpoint('https://example.invalid/', 'example.invalid'), lambda: 'test')
        with patch('builtins.__import__', side_effect=import_without_parser), patch.object(client, '_post') as post:
            with self.assertRaisesRegex(HeroClientError, 'validation is unavailable'):
                client.query('query Q { viewer { id } }', {})
        post.assert_not_called()
