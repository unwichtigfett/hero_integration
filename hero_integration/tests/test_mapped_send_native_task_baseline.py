from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from hero_integration.services import mapped_send
from hero_integration.services.direct_mapping import MappingSpec


class TestNativeTaskSendBaseline(TestCase):
    def resolve(self, path, target, observed, **overrides):
        rule = MappingSpec(operation='HeroTasks', definition_id='rule', source_path=path,
            source_type='string|null' if path == 'comment' else 'int|null', target_doctype='Task',
            target_field=target, purpose='Native Binding', transformation='Native Adapter', relation='')
        rule = replace(rule, **overrides)
        ctx = SimpleNamespace(proof=SimpleNamespace(source_instance='same-source'))
        with patch.object(mapped_send, '_query_plan', return_value=SimpleNamespace(adapter_options=None)), \
             patch.object(mapped_send, '_matching_query_rule', return_value=rule):
            return mapped_send._baseline_remote(ctx, {}, {'read_path': path}, observed)

    def test_native_description_uses_exact_inbound_html_projection(self):
        self.assertEqual(self.resolve('comment', 'description', ' A < B\nC '), '<p>A &lt; B<br>C</p>')
        self.assertIsNone(self.resolve('comment', 'description', '  '))
        with self.assertRaises(ValueError):
            self.resolve('comment', 'description', {'not': 'text'})

    def test_native_project_resolves_the_same_bound_source_namespace(self):
        with patch('hero_integration.services.migration._project_name_for_source', return_value='PROJECT-1') as resolve:
            self.assertEqual(self.resolve('target_project_match_id', 'project', 123), 'PROJECT-1')
            resolve.assert_called_once_with(123, 'same-source')

    def test_modified_native_binding_remains_blocked(self):
        with self.assertRaises(ValueError):
            self.resolve('comment', 'subject', 'text')
