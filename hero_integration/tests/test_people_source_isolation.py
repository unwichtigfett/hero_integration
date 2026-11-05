"""HERO IDs belong to a source, never to the global legacy User ID columns."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.integrations import hero_people as people
from hero_integration.tests import test_employee_migration as employee_fixtures


class TestPeopleSourceIsolation(unittest.TestCase):
    def setUp(self):
        self.links = []
        self.employees = {}
        self.users = {}
        for source, user in [('source-a', 'alice@example.invalid'), ('source-b', 'bob@example.invalid')]:
            employee = 'EMP-' + source
            self.employees[employee] = user
            self.users[user] = {'enabled': 1, 'email': user,
                                people.PARTNER_FIELD: '7', people.USER_ID_FIELD: '70'}
            self.links.extend([
                dict(source_system='HERO', source_instance=source, source_type='partner', source_id='7',
                     target_doctype='Employee', target_name=employee, sync_status='Synchronized'),
                dict(source_system='HERO', source_instance=source, source_type='User', source_id='70',
                     target_doctype='User', target_name=user, sync_status='Synchronized'),
            ])

        def get_all(doctype, *, filters, fields, limit_page_length):
            self.assertEqual(doctype, 'HERO Integration Link')
            self.assertTrue(filters.get('source_instance'))
            return [SimpleNamespace(**row) for row in self.links
                    if all(row.get(key) == value for key, value in filters.items())][:limit_page_length]

        def get_value(doctype, name, field):
            if doctype == 'Employee':
                self.assertEqual(field, 'user_id')
                return self.employees.get(name)
            self.assertEqual((doctype, field), ('User', 'enabled'))
            return self.users.get(name, {}).get('enabled')

        def sql(query, values):
            self.assertNotIn('custom_hero_', query)
            self.assertIn('LIMIT 2', query)
            return [(name,) for name, row in self.users.items()
                    if row['email'].lower() == values[0] or name.lower() == values[1]][:2]

        self.fake = SimpleNamespace(get_all=Mock(side_effect=get_all),
                                    db=SimpleNamespace(get_value=Mock(side_effect=get_value), sql=Mock(side_effect=sql)))
        self.scope = patch.object(people, 'frappe', self.fake)
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def test_equal_partner_and_user_ids_resolve_only_inside_their_source(self):
        for source, expected in [('source-a', 'alice@example.invalid'), ('source-b', 'bob@example.invalid')]:
            with self.subTest(source=source):
                self.assertEqual(people.user_by_partner(7, source_instance=source), expected)
                self.assertEqual(people.user_by_hero_user(70, source_instance=source), expected)
        self.fake.db.sql.assert_not_called()

    def test_source_b_without_binding_does_not_reuse_source_a_legacy_id(self):
        self.links = [row for row in self.links if row['source_instance'] == 'source-a']
        self.assertIsNone(people.user_by_partner(7, source_instance='source-b'))
        self.assertIsNone(people.user_by_hero_user(70, source_instance='source-b'))
        self.assertEqual(people._existing_user('bob@example.invalid', '7', source_instance='source-b'),
                         'bob@example.invalid')

    def test_missing_source_and_invalid_ids_never_query_global_columns(self):
        for resolver, identity in [(people.user_by_partner, 7), (people.user_by_hero_user, 70)]:
            self.assertIsNone(resolver(identity))
            for value in (None, False, 0, -1, '7'):
                with self.subTest(resolver=resolver.__name__, identity=value):
                    self.assertIsNone(resolver(value, source_instance='source-a'))
        self.fake.get_all.assert_not_called()
        self.fake.db.sql.assert_not_called()

    def test_held_or_ambiguous_bindings_are_not_assignment_authority(self):
        original = [dict(row) for row in self.links]
        for status in ('Pending Review', 'Rejected', 'Conflict', 'Source Deleted'):
            with self.subTest(status=status):
                self.links = [{**row, 'sync_status': status} for row in original]
                self.assertIsNone(people.user_by_partner(7, source_instance='source-a'))
                self.assertIsNone(people.user_by_hero_user(70, source_instance='source-a'))
        self.links = original + [dict(original[0]), dict(original[1])]
        self.assertIsNone(people.user_by_partner(7, source_instance='source-a'))
        self.assertIsNone(people.user_by_hero_user(70, source_instance='source-a'))

    def test_partner_requires_companion_user_binding_in_same_source(self):
        self.links = [row for row in self.links
                      if not (row['source_instance'] == 'source-a' and row['source_type'] == 'User')]
        self.assertIsNone(people.user_by_partner(7, source_instance='source-a'))
        self.assertEqual(people.user_by_partner(7, source_instance='source-b'), 'bob@example.invalid')

    def test_missing_disabled_or_wrong_doctype_targets_are_rejected(self):
        self.users['alice@example.invalid']['enabled'] = 0
        self.assertIsNone(people.user_by_partner(7, source_instance='source-a'))
        self.assertIsNone(people.user_by_hero_user(70, source_instance='source-a'))
        del self.users['alice@example.invalid']
        self.assertIsNone(people.user_by_hero_user(70, source_instance='source-a'))
        self.links[2]['target_doctype'] = 'Customer'
        self.links[3]['target_doctype'] = 'Employee'
        self.assertIsNone(people.user_by_partner(7, source_instance='source-b'))
        self.assertIsNone(people.user_by_hero_user(70, source_instance='source-b'))

    def test_legacy_adoption_uses_exact_unique_email_never_partner_id(self):
        self.links = []
        self.assertEqual(people._existing_user('bob@example.invalid', '7'), 'bob@example.invalid')
        self.assertIsNone(people._existing_user('new@example.invalid', '7'))
        self.users['duplicate@example.invalid'] = {'email': 'bob@example.invalid', 'enabled': 1}
        self.assertIsNone(people._existing_user('bob@example.invalid', '7'))

    def test_existing_scoped_partner_binding_survives_changed_source_email(self):
        self.assertEqual(people._existing_user('new@example.invalid', '7', source_instance='source-b'),
                         'bob@example.invalid')
        self.fake.db.sql.assert_not_called()


class TestEmployeeSourcePropagation(unittest.TestCase):
    def test_employee_import_passes_source_to_existing_user_lookup(self):
        fixture = employee_fixtures.TestEmployeeWrites()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.run_import()
        self.assertEqual(fixture.find_user.call_args.kwargs, {'source_instance': 'source-a'})

    def test_exact_email_already_bound_to_another_source_cannot_be_adopted(self):
        from hero_integration.services import employee_migration

        fixture = employee_fixtures.TestEmployeeWrites()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.find_user.return_value = fixture.user.name
        fixture.fake.get_all = Mock(return_value=[SimpleNamespace(name='foreign-user-binding')])
        before = dict(fixture.user)
        with self.assertRaises(employee_migration.EmployeeMigrationBlocked):
            fixture.run_import()
        fixture.write_link.assert_not_called()
        self.assertEqual(fixture.user, before)
        self.assertEqual(fixture.new_docs, [])


if __name__ == '__main__':
    unittest.main()


class TestPeopleMappingSourceContext(unittest.TestCase):
    def test_legacy_and_v2_project_rules_pass_each_record_source(self):
        from hero_integration.services.direct_mapping import MappingSpec
        from hero_integration.services.mapping_runtime import MappingPlan
        rule = MappingSpec('HeroProjectMatches', 'project_partner_id', 'Project',
                           'custom_responsible', 'int', transformation='User By Partner ID')
        identities = {('source-a', 7): 'alice@example.invalid', ('source-b', 7): 'bob@example.invalid'}
        for options in ({}, {'transform_options': {}}):
            plan = MappingPlan('HeroProjectMatches', (rule,), adapter_options=options)
            with patch.object(people, 'user_by_partner', side_effect=lambda identity, *, source_instance=None:
                              identities.get((source_instance, identity))) as resolve:
                for source in ('source-a', 'source-b'):
                    result = plan.values({'project_partner_id': 7, '_hero_source_instance': source})
                    self.assertEqual(result['custom_responsible'], identities[source, 7])
                    resolve.assert_called_with(7, source_instance=source)
                self.assertNotIn('custom_responsible', plan.values({'project_partner_id': 7}))

    def test_legacy_reader_keeps_default_source_explicit_and_does_not_mutate_payload(self):
        from hero_integration.sources.hero import HeroSource
        payload = {'project_matches': [{'id': 1, 'project_partner_id': 7}]}
        source = HeroSource(SimpleNamespace(query=Mock(return_value=payload)))
        page = source.projects()
        self.assertEqual(page.records[0]['_hero_source_instance'], 'default')
        self.assertNotIn('_hero_source_instance', payload['project_matches'][0])
