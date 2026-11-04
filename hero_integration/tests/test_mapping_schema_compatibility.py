"""Used-field schema compatibility is independent of unrelated introspection changes."""
from copy import deepcopy
from unittest import TestCase

from graphql import build_schema, introspection_from_schema

from hero_integration.services.mapping_schema_compatibility import check_compatibility, MappingSchemaCompatibilityError


SDL = '''
enum State { OPEN CLOSED }
enum Target { project_match contact }
input AddressInput { id: Int! city: String country: String }
input TaskInput { id: Int! title: String state: State address: AddressInput }
input LogbookInput { target: Target! target_id: Int! custom_text: String! }
type Task { id: Int! title: String state: State extra: String }
type Entry { id: Int }
type Query { tasks(limit: Int = 100): [Task!]! other: String }
type Mutation { update_task(task: TaskInput!): Task add_logbook_entry(logbook_entry: LogbookInput!): Entry }
'''


def raw(sdl=SDL): return introspection_from_schema(build_schema(sdl))


def profile():
    return {'usage': 'MAPPED_UPDATE_V2', 'target': {'operation': 'update_task'},
            'identity': {'input_path': 'task.id'},
            'assignments': [{'input_path': 'task.title'}, {'input_path': 'task.address.city'}],
            'required_readback': [{'input_path': 'task.address.id'}]}


class TestUsedMappingSchemaCompatibility(TestCase):
    def check(self, new, *, old=None, reads=None, profiles=None, mutations=None):
        return check_compatibility(old or raw(), new,
            read_queries=reads if reads is not None else ['query Read {tasks {id title}}'],
            mutation_profiles=profiles if profiles is not None else [profile()],
            mutation_queries=mutations if mutations is not None else ['mutation Update($task:TaskInput!){update_task(task:$task){id}}'])

    def test_unrelated_operations_types_directives_and_fields_do_not_block(self):
        changed = SDL.replace('extra: String', 'extra: Int unused: Boolean') + '''
          type Automation_WebhookSyncStatus { id: Int status: String }
          extend type Mutation { Automation_SyncWebhooks: Automation_WebhookSyncStatus }
          directive @unused(flag: Boolean = true) on FIELD
        '''
        result = self.check(raw(changed))
        self.assertTrue(result['compatible'])
        self.assertEqual(result['dependency_fingerprint'], self.check(raw())['dependency_fingerprint'])

    def test_descriptions_and_introspection_order_are_not_dependencies(self):
        schema = raw()
        schema['__schema']['types'].reverse()
        for kind in schema['__schema']['types']:
            kind['description'] = 'Changed documentation'
            for key in ('fields', 'inputFields', 'enumValues', 'interfaces', 'possibleTypes'):
                if isinstance(kind.get(key), list): kind[key].reverse()
        schema['__schema']['directives'].reverse()
        self.assertTrue(self.check(schema)['compatible'])

    def test_default_input_object_field_order_is_not_semantic(self):
        old = raw(SDL.replace('limit: Int = 100', 'limit: Int = 100, address: AddressInput = {id:1,city:"Berlin"}'))
        changed = deepcopy(old)
        next(kind for kind in changed['__schema']['types'] if kind['name'] == 'AddressInput')['inputFields'].reverse()
        self.assertTrue(self.check(changed, old=old)['compatible'])

    def test_omitted_object_default_member_type_is_still_used(self):
        definition = SDL.replace('limit: Int = 100', 'limit: Int = 100, address: AddressInput = {id:1}')
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(raw(definition.replace('id: Int! city', 'id: Float! city')), old=raw(definition), profiles=[], mutations=[])

    def test_selected_output_type_and_wrapper_changes_block(self):
        for before, after in (('title: String state', 'title: Int state'), ('[Task!]!', '[Task]')):
            with self.subTest(change=after), self.assertRaises(MappingSchemaCompatibilityError):
                self.check(raw(SDL.replace(before, after)))

    def test_missing_selected_output_and_argument_block_with_paths(self):
        with self.assertRaises(MappingSchemaCompatibilityError) as caught:
            self.check(raw(SDL.replace('title: String state', 'renamed: String state')))
        self.assertTrue(any('title' in path for path in caught.exception.changed_paths))
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(raw(SDL.replace('limit: Int = 100', 'offset: Int')), reads=['{tasks(limit: 5){id}}'])

    def test_used_argument_type_and_omitted_default_changes_block(self):
        for replacement in ('limit: String = "100"', 'limit: Int = 99'):
            with self.subTest(replacement=replacement), self.assertRaises(MappingSchemaCompatibilityError):
                self.check(raw(SDL.replace('limit: Int = 100', replacement)))

    def test_new_optional_or_defaulted_arguments_are_not_used(self):
        for addition in ('offset: Int', 'tenant: Int! = 1'):
            with self.subTest(addition=addition):
                self.assertTrue(self.check(raw(SDL.replace('limit: Int = 100', 'limit: Int = 100, ' + addition)))['compatible'])

    def test_new_required_root_argument_blocks(self):
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(raw(SDL.replace('limit: Int = 100', 'limit: Int = 100, tenant: Int!')))

    def test_configured_identity_assignment_and_readback_input_changes_block(self):
        for before, after in (('id: Int! city', 'id: ID! city'), ('city: String', 'city: Int'),
                              ('input TaskInput { id: Int!', 'input TaskInput { id: ID!')):
            with self.subTest(after=after), self.assertRaises(MappingSchemaCompatibilityError):
                self.check(raw(SDL.replace(before, after)))

    def test_unused_input_optional_change_or_addition_is_allowed(self):
        self.assertTrue(self.check(raw(SDL.replace('country: String', 'country: Int zip: Int')))['compatible'])

    def test_new_required_input_member_blocks_without_explicit_supplied_path(self):
        for addition in ('country: String required_zip: Int!', 'country: String nested: TaskInput!'):
            with self.subTest(addition=addition), self.assertRaises(MappingSchemaCompatibilityError) as caught:
                self.check(raw(SDL.replace('country: String', addition)))
            self.assertTrue(any('required input is not supplied' in path for path in caught.exception.changed_paths))

    def test_new_required_defaulted_member_is_compatible(self):
        self.assertTrue(self.check(raw(SDL.replace('country: String', 'country: String zip: Int! = 42')))['compatible'])

    def test_input_defaults_of_selected_members_are_checked(self):
        old = raw(SDL.replace('city: String', 'city: String = "Berlin"'))
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(raw(SDL.replace('city: String', 'city: String = "Hamburg"')), old=old)

    def test_existing_omitted_profile_member_defaults_cannot_change_or_disappear(self):
        old = raw(SDL.replace('country: String', 'country: String = "DE"'))
        for replacement in ('country: String = "US"', 'country: String'):
            with self.subTest(replacement=replacement), self.assertRaises(MappingSchemaCompatibilityError):
                self.check(raw(SDL.replace('country: String', replacement)), old=old)
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(old)

    def test_existing_omitted_nested_default_object_members_are_checked(self):
        definition = SDL.replace('country: String', 'country: CountryInput = {}') + ' input CountryInput { code: String = "DE" }'
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(raw(definition.replace('"DE"', '"US"')), old=raw(definition))

    def test_profile_only_omitted_root_argument_defaults_are_checked(self):
        definition = SDL.replace('update_task(task: TaskInput!)', 'update_task(task: TaskInput!, mode: String = "safe")')
        for replacement in ('mode: String = "fast"', 'mode: String'):
            with self.subTest(replacement=replacement), self.assertRaises(MappingSchemaCompatibilityError):
                self.check(raw(definition.replace('mode: String = "safe"', replacement)), old=raw(definition), mutations=[])
        self.assertTrue(self.check(raw(definition), mutations=[])['compatible'])

    def test_existing_omitted_query_argument_cannot_gain_a_default(self):
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(raw(), old=raw(SDL.replace('limit: Int = 100', 'limit: Int')), profiles=[], mutations=[])

    def test_literal_input_omitted_defaults_are_checked(self):
        definition = SDL.replace('limit: Int = 100', 'limit: Int = 100, address: AddressInput')
        query = '{tasks(address:{id:1}){id}}'
        old = raw(definition.replace('country: String', 'country: String = "DE"'))
        for replacement in ('country: String = "US"', 'country: String'):
            with self.subTest(replacement=replacement), self.assertRaises(MappingSchemaCompatibilityError):
                self.check(raw(definition.replace('country: String', replacement)), old=old, reads=[query], profiles=[], mutations=[])
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(old, old=raw(definition), reads=[query], profiles=[], mutations=[])
        self.assertTrue(self.check(raw(definition.replace('country: String', 'country: String zip: Int! = 42')),
                                   old=raw(definition), reads=[query], profiles=[], mutations=[])['compatible'])

    def test_required_input_coverage_is_checked_per_profile_not_union(self):
        changed = raw(SDL.replace('city: String', 'city: String!'))
        baseline = changed
        full = profile(); partial = profile();partial['assignments'] = [{'input_path': 'task.title'}]
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(changed, old=baseline, profiles=[full, partial])

    def test_logbook_checks_server_owned_inputs_without_client_assignments(self):
        logbook = {'usage': 'PROJECT_LOGBOOK_V1', 'target': {'operation': 'add_logbook_entry'},
                   'assignments': [{'input_path': 'logbook_entry.custom_text'}]}
        for replacement in ('target_id: ID!', 'target_id: String!'):
            with self.subTest(replacement=replacement), self.assertRaises(MappingSchemaCompatibilityError):
                self.check(raw(SDL.replace('target_id: Int!', replacement)), profiles=[logbook], mutations=[])
        self.assertTrue(self.check(raw(), profiles=[logbook], mutations=[])['compatible'])
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(raw(SDL.replace('type Entry { id: Int }', 'type Entry { id: String }')), profiles=[logbook], mutations=[])

    def test_used_directive_default_is_a_dependency(self):
        directive = ' directive @choose(mode: Int = 1) on FIELD'
        old = raw(SDL + directive)
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(raw(SDL + directive.replace('= 1', '= 2')), old=old, reads=['{tasks @choose {id}}'])

    def test_fragment_alias_and_directive_selected_fields_are_checked(self):
        query = 'query Q($yes:Boolean!){alias:tasks{...Names @include(if:$yes)}} fragment Names on Task{id title}'
        self.assertTrue(self.check(raw(), reads=[query])['compatible'])
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(raw(SDL.replace('title: String state', 'title: Int state')), reads=[query])

    def test_used_enum_members_are_part_of_the_type_contract(self):
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(raw(SDL.replace('OPEN CLOSED', 'OPEN CLOSED PAUSED')), reads=['{tasks{id state}}'])
        self.assertTrue(self.check(raw(SDL.replace('OPEN CLOSED', 'OPEN CLOSED PAUSED')))['compatible'])

    def test_used_union_members_are_part_of_the_result_contract(self):
        union = ' union SearchResult = Task | Entry extend type Query { search: [SearchResult] }'
        old = raw(SDL + union)
        changed = raw(SDL + union.replace('Task | Entry', 'Task | Entry | Extra') + ' type Extra { id: Int }')
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(changed, old=old, reads=['{search {__typename ... on Task {id title} ... on Entry {id}}}'])
        self.assertTrue(self.check(changed, old=old)['compatible'])

    def test_mutation_without_profile_conservatively_checks_opaque_input_type(self):
        with self.assertRaises(MappingSchemaCompatibilityError):
            self.check(raw(SDL.replace('country: String', 'country: Int')), profiles=[])

    def test_wrong_direction_and_unknown_profile_are_rejected(self):
        with self.assertRaises(MappingSchemaCompatibilityError): self.check(raw(), reads=['mutation {update_task(task:{id:1}){id}}'])
        with self.assertRaises(MappingSchemaCompatibilityError): self.check(raw(), profiles=[{'usage':'CUSTOM','target':{'operation':'update_task'}}])
        with self.assertRaises(MappingSchemaCompatibilityError): self.check(raw(), reads=['query {'])
