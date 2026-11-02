"""Mapping-owned native field selection, transforms and saved-query approval."""
from dataclasses import replace
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

from graphql import build_schema, introspection_from_schema

from hero_integration.services.direct_mapping import MappingSpec
from hero_integration.services.mapping_runtime import plan_from_definitions
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services import query_mapping as qm


def rule(identity, path, target, field, *, operation='HeroTasks', kind='string|null', purpose='Field', transform='Trim', relation=''):
    return MappingSpec(operation, path, target, field, kind, purpose, transform, relation, definition_id=identity)


def task_rules():
    return [rule('id', 'id', 'HERO Integration Link', 'source_id', kind='int', purpose='Source Identity', transform='Positive Int To Text'),
            rule('title', 'title', 'Task', 'subject', purpose='Record Name')]


def plan(rules=None, options=None, operation='HeroTasks'):
    return plan_from_definitions(operation, rules or task_rules(), target_operation=operation,
                                 adapter_options=options or {'version': 2})


def fixture():
    schema = build_schema('''
      scalar DateTime
      type Query { tasks(first:Int!,offset:Int!,orderBy:String):[Task!]!
        project_matches(first:Int!,offset:Int!,orderBy:String):[ProjectMatch!]! }
      type Task { id:Int! title:String is_deleted:Boolean! created:DateTime due_date:DateTime done_date:DateTime }
      type ProjectMatch { id:Int! project_title:String! project_nr:String! project_type:String
        customer_id:Int customer:Customer address:Address current_project_match_status:Status
        histories(first:Int,offset:Int):[History!]! }
      type Customer { title:String full_name:String }
      type Address { full_address:String }
      type Status { step:Step }
      type Step { name:String }
      type History { id:Int! custom_text:String custom_title:String author_name:String created:DateTime }
    ''')
    raw = introspection_from_schema(schema)
    snapshot = NS(name='snapshot', snapshot_json=raw, schema_fingerprint=analyze_snapshot(raw).fingerprint,
                  source_instance='source-a', analysis_status='Validated')
    service = NS(query_document='query tasks($first:Int!,$offset:Int!){tasks(first:$first,offset:$offset,orderBy:"id"){id title is_deleted}}',
                 operation_name='tasks')
    config = {'target_operation': 'HeroTasks', 'records_path': 'tasks', 'identity_path': 'id',
              'variables': {'first': '$page_size', 'offset': '$offset'}, 'page_size': 25,
              'query_adapter': {'version': 2}}
    return service, snapshot, config


class TestQueryAdapterV2(TestCase):
    def test_options_participate_in_mapping_version(self):
        p = plan()
        changed = replace(p, adapter_options={'version': 2, 'transform_options': {'title': {'default': 'configured'}}})
        self.assertNotEqual(p.mapping_version, changed.mapping_version)
        self.assertEqual(p.mapping_version, plan().mapping_version)

    def test_task_unselected_dates_and_assignee_never_become_values(self):
        values = plan().values({'id': 42, 'title': 'Mapped', 'created': '2025-01-01T00:00:00Z',
                                'due_date': '2025-01-02T00:00:00Z', 'done_date': '2025-01-02T00:00:00Z',
                                'target_user_id': 7})
        self.assertEqual(values, {'subject': 'Mapped'})

    def test_selected_task_date_uses_configured_source_and_timezone(self):
        p = plan(task_rules() + [rule('start', 'created', 'Task', 'exp_start_date', transform='Local Date')],
                 {'version': 2, 'transform_options': {'start': {'timezone': 'Europe/Berlin'}}})
        self.assertEqual(p.values({'id': 42, 'title': 'Mapped', 'created': '2025-01-01T23:30:00Z'})['exp_start_date'], '2025-01-02')

    def test_value_table_is_explicit_and_unknown_value_never_guesses(self):
        spec = rule('type', 'company_name', 'Customer', 'customer_type', operation='HeroCustomers', transform='Value Map')
        options = {'version': 2, 'transform_options': {'type': {'map': {'ACME': 'Company', 'null': 'Individual'}}}}
        self.assertEqual(qm.transform(spec, 'ACME', options), 'Company')
        self.assertEqual(qm.transform(spec, None, options), 'Individual')
        with self.assertRaises(ValueError): qm.transform(spec, 'OTHER', options)

    def test_rule_options_cannot_refer_to_unselected_identity(self):
        with self.assertRaises(ValueError):
            plan(options={'version': 2, 'transform_options': {'missing': {'map': {}}}})

    def test_native_status_privacy_and_system_fields_are_not_mapping_targets(self):
        for doctype, field in [('Task', 'status'), ('Event', 'event_type'), ('Project', 'owner'), ('Customer', 'modified')]:
            operation = {'Task': 'HeroTasks', 'Event': 'HeroCalendarEvents', 'Project': 'HeroProjectMatches', 'Customer': 'HeroCustomers'}[doctype]
            with self.subTest(doctype=doctype, field=field), self.assertRaises(ValueError):
                qm.validate_v2_spec(rule('bad', 'title', doctype, field, operation=operation), None, {'version': 2})

    def test_unselected_address_relation_is_not_imported(self):
        self.assertIsNone(qm.address_values(plan(), {'address': {'id': 1, 'street': 'Synthetic'}}))

    def test_query_seal_rejects_missing_rules_unbounded_pages_and_other_roots(self):
        service, snapshot, config = fixture()
        qm.seal_query(service, snapshot, config, plan())
        bad_documents = [service.query_document.replace('id title', 'id'),
                         service.query_document.replace('first:$first', 'first:25'),
                         service.query_document.replace('tasks(first:', 'project_matches(first:')]
        for document in bad_documents:
            with self.subTest(document=document), self.assertRaises(ValueError):
                qm.seal_query(NS(query_document=document, operation_name='tasks'), snapshot, config, plan())

    def test_query_seal_changes_with_saved_query_options_or_variables(self):
        service, snapshot, config = fixture()
        seal = qm.seal_query(service, snapshot, config, plan())
        changed = NS(query_document=service.query_document.replace('id title', 'id title created'), operation_name='tasks')
        self.assertNotEqual(seal, qm.seal_query(changed, snapshot, config, plan()))
        changed_config = {**config, 'variables': {**config['variables'], 'unused': 'bounded'}}
        self.assertNotEqual(seal, qm.seal_query(service, snapshot, changed_config, plan()))

    def test_projection_can_select_only_its_explicit_native_fields(self):
        rules = [rule('id', 'id', 'Event', 'custom_hero_source_id', operation='HeroCalendarEvents', kind='int', purpose='Source Identity', transform='Positive Int To Text'),
                 rule('title', 'title', 'Event', 'subject', operation='HeroCalendarEvents', purpose='Record Name'),
                 rule('start', 'start', 'Event', 'starts_on', operation='HeroCalendarEvents', transform='Native Adapter'),
                 rule('all', 'all_day', 'Event', 'all_day', operation='HeroCalendarEvents', kind='boolean|null', transform='Native Adapter')]
        p = plan(rules, operation='HeroCalendarEvents')
        values = p.values({'id': 1, 'title': 'Synthetic event', 'start': '2025-01-01T09:00:00Z', 'all_day': False},
                          native_value=lambda field: {'starts_on': '2025-01-01 09:00:00', 'all_day': 0}[field])
        self.assertEqual(set(values), {'custom_hero_source_id', 'subject', 'starts_on', 'all_day'})
        self.assertNotIn('description', values)

    def test_phase_can_use_a_selected_nested_name(self):
        spec = rule('phase', 'current_project_match_status.step.name', 'Project', 'custom_hero_phase', operation='HeroProjectMatches')
        fake = NS(target_doctype='Project', definitions=(spec,), adapter_options={'version': 2})
        self.assertEqual(qm.mapped_values(fake, {'current_project_match_status': {'step': {'name': 'Configured'}}}), {'custom_hero_phase': 'Configured'})

    def test_unsupported_configuration_and_link_type_fail_closed(self):
        with self.assertRaises(ValueError): qm.validate_query_adapter({'version': 2, 'execute': True})
        metadata = NS(ensure_write=lambda dt: None, field=lambda dt, f: NS(fieldtype='Link', options='Department', read_only=0))
        spec = rule('trade', 'project_type', 'Project', 'department', operation='HeroProjectMatches', transform='Link Map')
        with self.assertRaises(ValueError):
            qm.validate_v2_spec(spec, metadata, {'version': 2, 'transform_options': {'trade': {'map': {'PV': 'Synthetic'}, 'link_doctype': 'Country'}}})


def project_rules(with_logbook=False):
    op = 'HeroProjectMatches'
    rules = [rule('id', 'id', 'HERO Integration Link', 'source_id', operation=op, kind='int', purpose='Source Identity', transform='Positive Int To Text'),
             rule('title', 'project_title', 'Project', 'project_name', operation=op, kind='string', purpose='Record Name'),
             rule('customer', 'customer_id', 'Project', 'customer', operation=op, kind='int|null', purpose='Customer Relation', transform='HERO Customer Link'),
             rule('company', 'project_type', 'Project', 'company', operation=op, purpose='Confirmed Company', transform='Confirmed ERPNext Company')]
    if with_logbook:
        rules += [rule('text', 'histories.custom_text', 'Comment', 'content', operation=op, transform='Safe HTML'),
                  rule('created', 'histories.created', 'Comment', 'creation', operation=op, transform='Local Datetime')]
    return rules


class TestMappedCollectionsAndRelations(TestCase):
    def test_logbook_query_seals_explicit_child_fields_and_limit(self):
        service, snapshot, _ = fixture()
        options = {'version': 2, 'transform_options': {'created': {'timezone': 'UTC'}},
                   'logbook': {'path': 'histories', 'identity_path': 'id', 'limit': 20, 'heading_paths': ['histories.author_name']}}
        p = plan(project_rules(True), options, operation='HeroProjectMatches')
        config = {'target_operation': 'HeroProjectMatches', 'records_path': 'project_matches', 'identity_path': 'id',
                  'variables': {'first': '$page_size', 'offset': '$offset'}, 'query_adapter': options}
        service = NS(operation_name='projects', query_document='''query projects($first:Int!,$offset:Int!){
            project_matches(first:$first,offset:$offset){id project_title project_nr project_type customer_id
              customer {title full_name} address {full_address}
              histories(first:20){id custom_text author_name created}}}''')
        qm.seal_query(service, snapshot, config, p)
        for bad in (service.query_document.replace('first:20', 'first:21'), service.query_document.replace(' author_name', ''),
                    service.query_document.replace('histories(first:20)', 'histories')):
            with self.subTest(document=bad), self.assertRaises(ValueError):
                qm.seal_query(NS(query_document=bad, operation_name='projects'), snapshot, config, p)

    def test_nested_list_source_types_and_nullable_parent_are_derived(self):
        _, snapshot, _ = fixture()
        self.assertEqual(qm.source_type(snapshot.snapshot_json, 'HeroProjectMatches', 'histories.custom_text'), 'string|null')
        self.assertEqual(qm.source_type(snapshot.snapshot_json, 'tasks', 'id'), 'int')
        with self.assertRaises(ValueError): qm.source_type(snapshot.snapshot_json, 'tasks', 'absent')

    def test_address_country_requires_selected_mapping_without_germany_fallback(self):
        op = 'HeroCustomers'
        rules = [rule('street', 'address.street', 'Address', 'address_line1', operation=op, relation='Customer Primary Address'),
                 rule('zip', 'address.zipcode', 'Address', 'pincode', operation=op, relation='Customer Primary Address'),
                 rule('city', 'address.city', 'Address', 'city', operation=op, relation='Customer Primary Address'),
                 rule('country', 'address.country.code', 'Address', 'country', operation=op, transform='Link Map', relation='Customer Primary Address')]
        p = NS(definitions=rules, target_doctype='Customer', adapter_options={'version': 2,
            'transform_options': {'country': {'map': {'de': 'Germany'}, 'link_doctype': 'Country'}}})
        record = {'address': {'id': 7, 'street': 'Synthetic street', 'zipcode': '12345', 'city': 'Synthetic', 'country': {'code': 'de'}}}
        with patch.object(qm, '_reference', side_effect=lambda dt, name: name):
            self.assertEqual(qm.address_values(p, record)['country_name'], 'Germany')
            with self.assertRaises(ValueError):
                qm.address_values(p, {'address': {k: v for k, v in record['address'].items() if k != 'country'}})

    def test_mapped_logbook_is_scoped_deduplicated_and_requires_project_write(self):
        from html import escape
        from hero_integration.services import project_logbook
        options = {'version': 2, 'transform_options': {'created': {'timezone': 'UTC'}},
                   'logbook': {'path': 'histories', 'identity_path': 'id', 'limit': 20}}
        p = plan(project_rules(True), options, operation='HeroProjectMatches')
        comments, links = {}, []
        def get_doc(*args):
            if isinstance(args[0], dict):
                values = dict(args[0]); doctype = values['doctype']
                if doctype == 'Comment':
                    values['name'] = 'comment-' + str(len(comments) + 1)
                    doc = NS(**values)
                    def insert(**kw): comments[doc.name] = doc; return doc
                else:
                    doc = NS(name='link-' + str(len(links) + 1), **values)
                    def insert(**kw): links.append(doc); return doc
                doc.insert = insert
                return doc
            if args[0] == 'Project': return NS(name=args[1])
            return comments[args[1]]
        def get_all(doctype, filters, **kwargs):
            return [link for link in links if all(getattr(link, key) == value for key, value in filters.items())]
        fake = NS(get_doc=get_doc, get_all=get_all, has_permission=lambda *a, **kw: True,
                  db=NS(exists=lambda dt, name: name in comments, set_value=lambda *a, **kw: None),
                  utils=NS(escape_html=escape, now_datetime=lambda: '2025-01-02 00:00:00'))
        record = {'id': 42, '_hero_source_instance': 'source-a',
                  'histories': [{'id': 5, 'custom_text': '<p>Synthetic history</p>', 'created': '2025-01-01T00:00:00Z'}]}
        with patch.object(project_logbook, 'frappe', fake):
            self.assertEqual(project_logbook.sync_mapped_entries(p, record, 'P1'), 1)
            self.assertEqual(project_logbook.sync_mapped_entries(p, record, 'P1'), 0)
            self.assertEqual(project_logbook.sync_mapped_entries(p, {**record, 'id': 43}, 'P2'), 1)
            self.assertEqual(project_logbook.sync_mapped_entries(p, {**record, '_hero_source_instance': 'source-b'}, 'P3'), 1)
            self.assertEqual(len(comments), 3)
            self.assertEqual(len({link.source_id for link in links}), 3)
            fake.has_permission = lambda *a, **kw: (_ for _ in ()).throw(PermissionError('synthetic denial'))
            with self.assertRaises(PermissionError):
                project_logbook.sync_mapped_entries(p, {**record, 'id': 44}, 'P4')
            self.assertEqual(len(comments), 3)

    def test_collection_bound_is_rejected_before_any_write(self):
        from hero_integration.services import project_logbook
        p = NS(adapter_options={'logbook': {'path': 'histories', 'identity_path': 'id', 'limit': 1}}, definitions=())
        fake = NS(get_doc=lambda *a, **k: self.fail('No document may be written at an ambiguous collection boundary.'))
        with patch.object(project_logbook, 'frappe', fake), self.assertRaisesRegex(ValueError, 'collection bound'):
            project_logbook.sync_mapped_entries(p, {'histories': [{'id': 1}]}, 'P1')

    def test_v2_task_writer_does_not_read_hidden_user_directory(self):
        from hero_integration.services import migration
        p = plan()
        record = {'id': 42, 'title': 'Synthetic', '_hero_source_instance': 'source-a', 'target_user_id': 7, 'is_deleted': False,
                  'created': '2025-01-01T00:00:00Z'}
        with patch.object(migration, '_link', return_value=None), patch.object(migration, '_upsert') as upsert, \
                patch.object(migration, '_task_user_emails', side_effect=AssertionError('Hidden directory query')):
            migration._migrate_mapped_task(record, NS(mode='Execute'), p,
                {'confirmed': True, 'source_instance': 'source-a', 'source_type': 'Task'})
        self.assertEqual(upsert.call_args.args[3], {'subject': 'Synthetic'})


class TestMappedReadContractApproval(TestCase):
    def context(self):
        service, snapshot, config = fixture()
        p = plan()
        config['mapping_definition_ids'] = [spec.definition_id for spec in p.definitions]
        config['native_task_onboarding'] = {'confirmed': True, 'source_instance': 'source-a', 'source_type': 'Task'}
        config['query_adapter_seal'] = qm.seal_query(service, snapshot, config, p)
        service.name = 'reader'
        service.status, service.execution_status = 'Validated', 'Approved'
        service.service_revision = service.approved_service_revision = service.approved_mapping_revision = 1
        service.schema_snapshot, service.schema_fingerprint, service.read_mapping = snapshot.name, snapshot.schema_fingerprint, 'mapping'
        mapping = NS(name='mapping', status='Active', direction='QUERY_TO_FRAPPE', operation_kind='QUERY',
                     schema_snapshot=snapshot.name, schema_fingerprint=snapshot.schema_fingerprint,
                     source_instance=snapshot.source_instance, mapping_revision=1, mapping_json=json.dumps(config))
        return service, mapping, snapshot, config, p

    def test_approved_query_reconstructs_exact_seal(self):
        from hero_integration.services.read_contract import from_documents
        service, mapping, snapshot, config, p = self.context()
        with patch('hero_integration.services.mapping_runtime.active_mapping_plan', return_value=p):
            contract = from_documents(service, mapping, snapshot)
        self.assertEqual(contract.adapter_options, {'version': 2})
        self.assertIsNone(contract.materialized_request)
        self.assertEqual(contract.definition_ids, ('id', 'title'))

    def test_changed_page_size_rejects_old_approval(self):
        from hero_integration.services.read_contract import from_documents, ReadContractError
        service, mapping, snapshot, config, p = self.context()
        config['page_size'] = 100
        mapping.mapping_json = json.dumps(config)
        with patch('hero_integration.services.mapping_runtime.active_mapping_plan', return_value=p), self.assertRaises(ReadContractError):
            from_documents(service, mapping, snapshot)

    def test_changed_mapping_options_rejects_old_approval(self):
        from hero_integration.services.read_contract import from_documents, ReadContractError
        service, mapping, snapshot, config, p = self.context()
        config['query_adapter'] = {'version': 2, 'transform_options': {'title': {'default': 'configured'}}}
        mapping.mapping_json = json.dumps(config)
        changed = replace(p, adapter_options=config['query_adapter'])
        with patch('hero_integration.services.mapping_runtime.active_mapping_plan', return_value=changed), self.assertRaises(ReadContractError):
            from_documents(service, mapping, snapshot)

    def test_unconfirmed_task_source_never_reaches_adapter(self):
        from hero_integration.services.read_contract import from_documents, ReadContractError
        service, mapping, snapshot, config, p = self.context()
        mapping.source_instance = 'other-source'
        with patch('hero_integration.services.mapping_runtime.active_mapping_plan') as load, self.assertRaises(ReadContractError):
            from_documents(service, mapping, snapshot)
        load.assert_not_called()

    def test_lookup_query_is_read_only_and_has_no_business_rule_selection(self):
        from hero_integration.services.read_contract import from_documents
        service, mapping, snapshot, config, p = self.context()
        config.update(target_operation='HeroLookup', mapping_definition_ids=[], query_adapter={'version': 2, 'role': 'LOOKUP', 'max_records': 100})
        config.pop('native_task_onboarding')
        config['query_adapter_seal'] = qm.seal_lookup_query(service, snapshot, config)
        mapping.mapping_json = json.dumps(config)
        with patch('hero_integration.services.mapping_runtime.active_mapping_plan') as load:
            contract = from_documents(service, mapping, snapshot)
        load.assert_not_called()
        self.assertEqual(contract.target_operation, 'HeroLookup')
        self.assertEqual(contract.definition_ids, ())
        with self.assertRaises(ValueError): plan(options={'version': 2, 'role': 'LOOKUP', 'max_records': 100})
        for change in ({'target_operation': 'HeroTasks'}, {'mapping_definition_ids': ['title']},
                       {'field_rules': {'title': {}}}, {'additional_mappings': [{'id': 'extra'}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                qm.seal_lookup_query(service, snapshot, dict(config, **change))


class TestMappedMetadataExceptions(TestCase):
    def test_only_logbook_creation_uses_the_known_standard_datetime_slot(self):
        checked = []
        metadata = NS(ensure_write=lambda dt: checked.append(dt), field=lambda dt, field: None)
        spec = rule('created', 'histories.created', 'Comment', 'creation', operation='HeroProjectMatches', transform='Local Datetime')
        options = {'version': 2, 'logbook': {'path': 'histories', 'identity_path': 'id', 'limit': 20},
                   'transform_options': {'created': {'timezone': 'UTC'}}}
        qm.validate_v2_spec(spec, metadata, options)
        self.assertEqual(checked, ['Project'])
        with self.assertRaises(ValueError): qm.validate_v2_spec(replace(spec, target_doctype='Project'), metadata, options)
        with self.assertRaises(ValueError): qm.validate_v2_spec(replace(spec, target_field='owner'), metadata, options)


class TestMappedAssignmentAdmission(TestCase):
    def test_explicit_recipient_must_match_the_existing_guarded_user_binding(self):
        from hero_integration.services import migration
        assignment = rule('assignee', 'target_user_id', 'ToDo', 'allocated_to', kind='int|null', transform='Link Map')
        options = {'version': 2, 'transform_options': {'assignee': {'map': {'7': 'synthetic@example.invalid'}, 'link_doctype': 'User'}}}
        p = plan(task_rules() + [assignment], options)
        record = {'id': 42, 'title': 'Synthetic', '_hero_source_instance': 'source-a', 'target_user_id': 7,
                  'is_deleted': False, 'author_user_id': None}
        with patch.object(qm, '_reference', side_effect=lambda dt, name: name), \
                patch.object(migration, '_upsert', return_value=migration.Result('CREATED', '', 'Task', 'T1')), \
                patch.object(migration, '_plan_task_assignment', return_value=migration.TaskAssignmentDecision('CREATE', '', recipient='other@example.invalid')), \
                patch.object(migration, '_record_task_assignment_pending') as execute:
            result = migration._migrate_mapped_task(record, NS(mode='Execute'), p,
                {'confirmed': True, 'source_instance': 'source-a', 'source_type': 'Task'})
        self.assertEqual(result.code, 'CONFLICT')
        execute.assert_not_called()

    def test_matching_recipient_uses_existing_admission_and_execution_boundary(self):
        from hero_integration.services import migration
        assignment = rule('assignee', 'target_user_id', 'ToDo', 'allocated_to', kind='int|null', transform='Link Map')
        options = {'version': 2, 'transform_options': {'assignee': {'map': {'7': 'synthetic@example.invalid'}, 'link_doctype': 'User'}}}
        p = plan(task_rules() + [assignment], options)
        record = {'id': 42, 'title': 'Synthetic', '_hero_source_instance': 'source-a', 'target_user_id': 7,
                  'is_deleted': False, 'author_user_id': None}
        with patch.object(qm, '_reference', side_effect=lambda dt, name: name), \
                patch.object(migration, '_upsert', return_value=migration.Result('CREATED', '', 'Task', 'T1')) as upsert, \
                patch.object(migration, '_plan_task_assignment', return_value=migration.TaskAssignmentDecision('CREATE', '', recipient='synthetic@example.invalid')), \
                patch.object(migration, '_record_task_assignment_pending') as execute, \
                patch.object(migration, '_ack_mapped_assignment', return_value=False):
            run = NS(mode='Execute')
            result = migration._migrate_mapped_task(record, run, p,
                {'confirmed': True, 'source_instance': 'source-a', 'source_type': 'Task'})
        self.assertEqual(result.code, 'CREATED')
        execute.assert_called_once_with(run, record, 'T1', 'source-a')
        self.assertEqual(upsert.call_args.args[3], {'subject': 'Synthetic'})


class TestPrimaryContactProjectionAliases(TestCase):
    def test_customer_proxy_and_contact_cannot_write_the_same_primary_field(self):
        rules = [rule('id', 'id', 'HERO Integration Link', 'source_id', operation='HeroCustomers',
                      kind='int', purpose='Source Identity', transform='Positive Int To Text'),
                 rule('title', 'company_name', 'Customer', 'customer_name', operation='HeroCustomers', purpose='Record Name'),
                 rule('type', 'company_name', 'Customer', 'customer_type', operation='HeroCustomers'),
                 rule('email', 'email', 'Customer', 'email_id', operation='HeroCustomers', purpose='Customer Primary Contact'),
                 rule('alias', 'other_email', 'Contact', 'email_id', operation='HeroCustomers', purpose='Customer Primary Contact')]
        with self.assertRaisesRegex(ValueError, 'one target field twice'):
            plan(rules, operation='HeroCustomers')

    def test_readonly_email_projection_uses_only_the_primary_contact_adapter(self):
        metadata = NS(ensure_write=lambda dt: None,
                      field=lambda dt, field: NS(fieldtype='Data', read_only=1, fetch_from='email_ids.email_id', hidden=0))
        spec = rule('email', 'email', 'Contact', 'email_id', operation='HeroCustomers', purpose='Customer Primary Contact')
        qm.validate_v2_spec(spec, metadata, {'version': 2})
        with self.assertRaises(ValueError): qm.validate_v2_spec(replace(spec, purpose='Field'), metadata, {'version': 2})


class TestMappingOwnedTaskIntake(TestCase):
    def test_sealed_v2_task_query_does_not_require_a_legacy_onboarding_marker(self):
        from hero_integration.services.read_contract import from_documents
        helper = TestMappedReadContractApproval()
        service, mapping, snapshot, config, p = helper.context()
        config.pop('native_task_onboarding')
        config['query_adapter_seal'] = qm.seal_query(service, snapshot, config, p)
        mapping.mapping_json = json.dumps(config)
        with patch('hero_integration.services.mapping_runtime.active_mapping_plan', return_value=p):
            contract = from_documents(service, mapping, snapshot)
        self.assertEqual(contract.target_operation, 'HeroTasks')
        self.assertEqual(contract.adapter_options, {'version': 2})


class TestNativeSemanticGroups(TestCase):
    def test_event_time_mapping_cannot_bypass_the_timezone_adapter(self):
        for field in ('starts_on', 'ends_on', 'all_day'):
            spec = rule('time', 'start', 'Event', field, operation='HeroCalendarEvents', transform='Identity')
            with self.subTest(field=field), self.assertRaises(ValueError):
                qm.validate_v2_spec(spec, None, {'version': 2})

    def test_project_title_builder_cannot_be_bypassed_by_mislabeling_a_rule(self):
        spec = rule('title', 'project_title', 'Project', 'project_name', operation='HeroProjectMatches', purpose='Field')
        with self.assertRaises(ValueError): qm.validate_v2_spec(spec, None, {'version': 2})
        with self.assertRaises(ValueError): qm.validate_v2_spec(replace(spec, target_field='custom_hero_alternate', purpose='Record Name'), None, {'version': 2})


class TestLegacyLogbookIdentityBridge(TestCase):
    def setUp(self):
        from html import escape
        self.options = {'version': 2, 'transform_options': {'created': {'timezone': 'UTC'}},
            'logbook': {'path': 'histories', 'identity_path': 'id', 'limit': 20}}
        self.plan = plan(project_rules(True), self.options, operation='HeroProjectMatches')
        self.record = {'id': 42, '_hero_source_instance': 'source-a', 'histories': [
            {'id': 5, 'custom_text': 'Remote content has changed', 'created': '2026-01-01T00:00:00Z'}]}
        self.comment = NS(name='legacy-comment', reference_doctype='Project', reference_name='P1',
                          content='Original imported content', creation='2020-01-02 03:04:05')
        self.legacy = NS(name='legacy-link', source_system='HERO', source_type='project_history', source_instance='default',
            source_id='5', target_doctype='Comment', target_name='legacy-comment', mapping_version='project-logbook-v1',
            source_payload_hash='old-source-hash', target_payload_hash='old-target-hash', last_synced_on='2025-01-01 00:00:00')
        self.links = [self.legacy]
        self.project_bindings = [NS(name='project-link', source_system='HERO', source_type='project', source_id='42',
            source_instance='source-a', target_doctype='Project', target_name='P1')]
        self.created_comments = []
        self.updated = []
        def get_all(doctype, filters, **kwargs):
            rows = self.project_bindings if filters.get('source_type') == 'project' else self.links
            return [row for row in rows if all(getattr(row, key, None) == value for key, value in filters.items())]
        def get_doc(*args):
            if isinstance(args[0], dict):
                values = dict(args[0]); doc = NS(name='new-' + str(len(self.links)), **values)
                def insert(**kwargs):
                    (self.created_comments if values['doctype'] == 'Comment' else self.links).append(doc)
                    return doc
                doc.insert = insert
                return doc
            if args[0] == 'Project': return NS(name=args[1])
            return self.comment
        self.fake = NS(get_doc=get_doc, get_all=get_all, has_permission=lambda *a, **kwargs: True,
            db=NS(exists=lambda dt, name: name == self.comment.name, set_value=lambda *a, **kwargs: self.updated.append(a)),
            utils=NS(escape_html=escape, now_datetime=lambda: '2026-01-01 00:00:00'))

    def sync(self):
        from hero_integration.services import project_logbook
        with patch.object(project_logbook, 'frappe', self.fake):
            return project_logbook.sync_mapped_entries(self.plan, self.record, 'P1')

    def test_upgrade_adopts_legacy_identity_without_duplicate_or_content_date_changes(self):
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.sync(), 0)
        self.assertEqual(len(self.links), 2)
        self.assertIs(self.links[0], self.legacy)
        alias = self.links[1]
        self.assertEqual(alias.target_name, self.comment.name)
        self.assertEqual(alias.source_instance, 'source-a')
        self.assertEqual(alias.mapping_version, 'project-logbook-v1')
        self.assertEqual((alias.source_payload_hash, alias.target_payload_hash, alias.last_synced_on),
                         ('old-source-hash', 'old-target-hash', '2025-01-01 00:00:00'))
        self.assertNotEqual(alias.source_id, '5')
        self.assertEqual(self.comment.content, 'Original imported content')
        self.assertEqual(self.comment.creation, '2020-01-02 03:04:05')
        self.assertEqual(self.created_comments, [])
        self.assertEqual(self.updated, [])

    def test_foreign_parent_is_not_adopted(self):
        self.comment.reference_name = 'OTHER-PROJECT'
        with self.assertRaisesRegex(ValueError, 'another Project'): self.sync()
        self.assertEqual(len(self.links), 1)
        self.assertEqual(self.created_comments, [])

    def test_foreign_source_binding_is_not_adopted(self):
        self.project_bindings[0].source_instance = 'other-source'
        with self.assertRaisesRegex(ValueError, 'uniquely proven'): self.sync()
        self.assertEqual(len(self.links), 1)
        self.assertEqual(self.created_comments, [])

    def test_ambiguous_project_or_history_binding_is_not_adopted(self):
        self.project_bindings.append(NS(name='foreign-binding', source_system='HERO', source_type='project',
            source_id='42', source_instance='other-source', target_doctype='Project', target_name='P1'))
        with self.assertRaisesRegex(ValueError, 'uniquely proven'): self.sync()
        self.project_bindings.pop()
        self.links.append(self.legacy)
        with self.assertRaisesRegex(ValueError, 'ambiguous'): self.sync()
        self.assertEqual(self.created_comments, [])

    def test_missing_project_proof_is_not_adopted(self):
        self.project_bindings.clear()
        with self.assertRaisesRegex(ValueError, 'uniquely proven'): self.sync()
        self.assertEqual(len(self.links), 1)
        self.assertEqual(self.created_comments, [])


class TestMappedTargetTypes(TestCase):
    def metadata(self, fieldtype):
        return NS(ensure_write=lambda dt: None, field=lambda dt, field: NS(fieldtype=fieldtype, read_only=0, hidden=0, options=''))

    def test_string_identity_cannot_silently_become_currency_zero(self):
        spec = rule('amount', 'title', 'Task', 'custom_hero_amount', transform='Identity')
        for options in ({'version': 2}, {'version': 2, 'transform_options': {'amount': {'target_type': 'Currency'}}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                qm.validate_v2_spec(spec, self.metadata('Currency'), options)

    def test_integer_source_to_float_requires_bound_target_type_and_hashes_canonically(self):
        from hero_integration.services.change_detection import field_hashes
        spec = rule('amount', 'quantity', 'Task', 'custom_hero_amount', kind='float|null', transform='Identity')
        with self.assertRaises(ValueError): qm.validate_v2_spec(spec, self.metadata('Currency'), {'version': 2})
        options = {'version': 2, 'transform_options': {'amount': {'target_type': 'Currency'}}}
        qm.validate_v2_spec(spec, self.metadata('Currency'), options)
        result = qm.transform(spec, 7, options)
        self.assertIs(type(result), float)
        self.assertEqual(field_hashes({'value': result}), field_hashes({'value': 7.0}))
        with self.assertRaises(ValueError): qm.transform(spec, 'not a number', options)
        with self.assertRaises(ValueError): qm.validate_v2_spec(spec, self.metadata('Float'), options)

    def test_explicit_number_rejects_invalid_nonfinite_boolean_and_empty_values(self):
        spec = rule('amount', 'amount', 'Task', 'custom_hero_amount', transform='Number')
        options = {'version': 2, 'transform_options': {'amount': {'target_type': 'Currency'}}}
        qm.validate_v2_spec(spec, self.metadata('Currency'), options)
        self.assertEqual(qm.transform(spec, '12.50', options), 12.5)
        for value in ('not a number', '', ' ', 'nan', 'inf', True, {}, []):
            with self.subTest(value=value), self.assertRaises(ValueError): qm.transform(spec, value, options)
        self.assertEqual(qm.transform(spec, -0.0, options), 0.0)

    def test_integer_requires_an_explicit_nontruncating_transform(self):
        spec = rule('count', 'title', 'Task', 'custom_hero_count', transform='Integer')
        options = {'version': 2, 'transform_options': {'count': {'target_type': 'Int'}}}
        qm.validate_v2_spec(spec, self.metadata('Int'), options)
        self.assertEqual(qm.transform(spec, '12', options), 12)
        for value in ('1.5', 1.5, True, 'NaN', ''):
            with self.subTest(value=value), self.assertRaises(ValueError): qm.transform(spec, value, options)
        with self.assertRaises(ValueError): qm.validate_v2_spec(replace(spec, transformation='Identity'), self.metadata('Int'), options)

    def test_value_map_literals_must_match_target_type_at_activation(self):
        spec = rule('amount', 'title', 'Task', 'custom_hero_amount', transform='Value Map')
        for value in ('not numeric', False, {}, []):
            options = {'version': 2, 'transform_options': {'amount': {'map': {'synthetic': value}, 'target_type': 'Currency'}}}
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                qm.validate_v2_spec(spec, self.metadata('Currency'), options)
        options = {'version': 2, 'transform_options': {'amount': {'map': {'synthetic': 3}, 'target_type': 'Currency'}}}
        qm.validate_v2_spec(spec, self.metadata('Currency'), options)
        self.assertIs(type(qm.transform(spec, 'synthetic', options)), float)

    def test_ambiguous_datetime_cannot_be_silently_stripped_by_frappe(self):
        spec = rule('when', 'created', 'Task', 'custom_hero_datetime', transform='Identity')
        options = {'version': 2, 'transform_options': {'when': {'target_type': 'Datetime'}}}
        qm.validate_v2_spec(spec, self.metadata('Datetime'), options)
        with self.assertRaises(ValueError): qm.transform(spec, '2026-01-01T10:00:00+02:00', options)
        self.assertEqual(qm.transform(spec, '2026-01-01T10:00:00', options), '2026-01-01 10:00:00')


class TestMappedChildTargetTypes(TestCase):
    def context(self, protected=None):
        spec = rule('rows', 'resources', 'Event', 'custom_hero_resources', operation='HeroCalendarEvents', kind='json|null', transform='Table Rows')
        options = {'version': 2, 'transform_options': {'rows': {
            'child_fields': {'id': 'custom_hero_source_id', 'amount': 'custom_hero_amount'},
            'child_types': {'custom_hero_source_id': 'Data', 'custom_hero_amount': 'Currency'},
            'child_transforms': {'custom_hero_source_id': 'Scalar Text', 'custom_hero_amount': 'Number'}}}}
        def field(dt, name):
            if dt == 'Event': return NS(fieldtype='Table', options='Synthetic Child', read_only=0, hidden=0)
            values = {'fieldtype': 'Currency' if name == 'custom_hero_amount' else 'Data', 'read_only': 0,
                      'hidden': 0, 'permlevel': 0, 'is_virtual': 0, 'fetch_from': ''}
            if protected: values.update(protected)
            return NS(**values)
        metadata = NS(ensure_write=lambda dt: None, field=field)
        return spec, options, metadata

    def test_every_protected_child_attribute_is_rejected(self):
        for attribute, value in [('hidden', 1), ('permlevel', 1), ('read_only', 1), ('is_virtual', 1), ('fetch_from', 'parent.secret')]:
            spec, options, metadata = self.context({attribute: value})
            with self.subTest(attribute=attribute), self.assertRaises(ValueError):
                qm.validate_v2_spec(spec, metadata, options)

    def test_rows_have_declared_child_types_and_numeric_canonical_values(self):
        spec, options, metadata = self.context()
        qm.validate_query_adapter(options, (spec,))
        qm.validate_v2_spec(spec, metadata, options)
        result = qm.transform(spec, [{'id': 7, 'amount': '12.50'}], options)
        self.assertEqual(result, [{'custom_hero_source_id': '7', 'custom_hero_amount': 12.5}])
        self.assertIs(type(result[0]['custom_hero_amount']), float)
        for value in ('bad', 'nan', True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                qm.transform(spec, [{'id': 7, 'amount': value}], options)

    def test_untyped_or_mismatched_children_fail_before_transport(self):
        spec, options, metadata = self.context()
        options['transform_options']['rows'].pop('child_types')
        with self.assertRaises(ValueError): qm.validate_query_adapter(options, (spec,))
        spec, options, metadata = self.context()
        options['transform_options']['rows']['child_types']['custom_hero_amount'] = 'Float'
        with self.assertRaises(ValueError): qm.validate_v2_spec(spec, metadata, options)

    def test_direct_address_contract_cannot_borrow_a_nested_customers_identity(self):
        raw = introspection_from_schema(build_schema('''
            type Query { contacts: [Customer!]! }
            type Customer { id:Int! parent_customer:Customer address:Address }
            type Address { id:Int! street:String }
        '''))
        self.assertEqual(qm.source_type(raw, 'HeroCustomers', 'parent_customer.address.street'), 'string|null')
        spec = rule('street', 'parent_customer.address.street', 'Address', 'address_line1', operation='HeroCustomers', relation='Customer Primary Address')
        with self.assertRaises(ValueError): qm.validate_v2_spec(spec, None, {'version': 2})
        fake = NS(definitions=(spec,), adapter_options={'version': 2})
        with self.assertRaises(ValueError):
            qm.address_values(fake, {'parent_customer': {'id': 123, 'address': {'id': 777, 'street': 'Synthetic'}}})


class TestChildSchemaTypeProof(TestCase):
    def test_selected_child_scalar_type_and_transform_are_checked_against_schema(self):
        raw = introspection_from_schema(build_schema('''
            scalar DateTime
            type Query { calendar_events(first:Int!,offset:Int!):[Event!]! }
            type Event { id:Int! title:String start:DateTime all_day:Boolean deleted:Boolean! is_recurring:Boolean! resources:[Resource!] }
            type Resource { id:Int! amount:String }
        '''))
        snapshot = NS(name='snapshot', snapshot_json=raw, schema_fingerprint=analyze_snapshot(raw).fingerprint,
                      source_instance='source-a', analysis_status='Validated')
        service = NS(operation_name='events', query_document='''query events($first:Int!,$offset:Int!){
            calendar_events(first:$first,offset:$offset){id title start all_day deleted is_recurring resources{id amount}}}''')
        op = 'HeroCalendarEvents'
        rules = [rule('id', 'id', 'Event', 'custom_hero_source_id', operation=op, kind='int', purpose='Source Identity', transform='Positive Int To Text'),
                 rule('title', 'title', 'Event', 'subject', operation=op, purpose='Record Name'),
                 rule('start', 'start', 'Event', 'starts_on', operation=op, transform='Native Adapter'),
                 rule('all', 'all_day', 'Event', 'all_day', operation=op, kind='boolean|null', transform='Native Adapter')]
        child, options, metadata = TestMappedChildTargetTypes().context()
        rules.append(child)
        config = {'target_operation': op, 'records_path': 'calendar_events', 'identity_path': 'id',
                  'variables': {'first': '$page_size', 'offset': '$offset'}, 'query_adapter': options}
        qm.seal_query(service, snapshot, config, plan(rules, options, operation=op))
        options['transform_options']['rows']['child_transforms']['custom_hero_amount'] = 'Identity'
        with self.assertRaisesRegex(ValueError, 'incompatible'):
            qm.seal_query(service, snapshot, config, plan(rules, options, operation=op))


class TestVerifiedMappedAssignmentBaseline(TestCase):
    def context(self):
        from hero_integration.services import migration
        source = migration.field_hashes({'subject': 'Synthetic title', 'custom_hero_due_date': '2026-01-01'})
        target = migration.field_hashes({'subject': 'Synthetic title', 'custom_hero_due_date': '2026-01-01'})
        link = NS(name='task-link', source_system='HERO', source_type='Task', source_id='42', source_instance='source-a',
                  target_doctype='Task', target_name='T1', source_field_hashes=migration.encode_hashes(source),
                  target_field_hashes=migration.encode_hashes(target))
        writes = []
        fake = NS(get_doc=lambda *args, **kwargs: link,
                  db=NS(set_value=lambda *args, **kwargs: writes.append((args, kwargs))))
        return migration, link, source, target, writes, fake

    def test_keep_under_existing_policy_merges_only_namespaced_user_baseline(self):
        migration, link, source, target, writes, fake = self.context()
        decision = migration.TaskAssignmentDecision('KEEP', '', recipient='synthetic@example.invalid')
        with patch.object(migration, 'frappe', fake), patch.object(migration, '_link', return_value=link), \
                patch.object(migration, '_plan_task_assignment', return_value=decision):
            acknowledged = migration._ack_mapped_assignment(NS(mode='Execute'), {'id': 42}, 'T1', 'source-a', 'synthetic@example.invalid')
        self.assertTrue(acknowledged)
        self.assertEqual(len(writes), 1)
        args, kwargs = writes[0]
        self.assertEqual(args[:2], ('HERO Integration Link', 'task-link'))
        actual_source = migration.decode_hashes(args[2]['source_field_hashes'])
        actual_target = migration.decode_hashes(args[2]['target_field_hashes'])
        self.assertEqual({key: actual_source[key] for key in source}, source)
        self.assertEqual({key: actual_target[key] for key in target}, target)
        expected = migration.field_hashes({'ToDo.allocated_to': 'synthetic@example.invalid'})['ToDo.allocated_to']
        self.assertEqual(actual_source['ToDo.allocated_to'], expected)
        self.assertEqual(actual_target['ToDo.allocated_to'], expected)
        self.assertEqual(kwargs, {'update_modified': False})

    def test_local_conflict_unproven_transfer_and_preview_never_advance_baselines(self):
        migration, link, source, target, writes, fake = self.context()
        for mode, action, recipient in [('Preview', 'KEEP', 'synthetic@example.invalid'),
                ('Execute', 'CONFLICT', 'synthetic@example.invalid'), ('Execute', 'LOCAL', 'synthetic@example.invalid'),
                ('Execute', 'BLOCKED', 'synthetic@example.invalid'), ('Execute', 'CREATE', 'synthetic@example.invalid'),
                ('Execute', 'KEEP', 'other@example.invalid')]:
            decision = migration.TaskAssignmentDecision(action, '', recipient=recipient)
            with self.subTest(mode=mode, action=action, recipient=recipient), patch.object(migration, 'frappe', fake), \
                    patch.object(migration, '_link', side_effect=AssertionError('Unverified assignment cannot lock/write a baseline')), \
                    patch.object(migration, '_plan_task_assignment', return_value=decision):
                self.assertFalse(migration._ack_mapped_assignment(NS(mode=mode), {'id': 42}, 'T1', 'source-a', 'synthetic@example.invalid'))
        self.assertEqual(writes, [])

    def test_foreign_parent_binding_cannot_receive_an_assignment_acknowledgement(self):
        migration, link, source, target, writes, fake = self.context()
        link.source_instance = 'foreign-source'
        decision = migration.TaskAssignmentDecision('KEEP', '', recipient='synthetic@example.invalid')
        with patch.object(migration, 'frappe', fake), patch.object(migration, '_link', return_value=link), \
                patch.object(migration, '_plan_task_assignment', return_value=decision), self.assertRaises(migration.MappingPlanError):
            migration._ack_mapped_assignment(NS(mode='Execute'), {'id': 42}, 'T1', 'source-a', 'synthetic@example.invalid')
        self.assertEqual(writes, [])


class TestLookupPaginationBoundary(TestCase):
    def source(self, *, variables=None, records=None, page_size=1, maximum=5, materialized=None, role='LOOKUP', target='HeroLookup'):
        from hero_integration.services.read_contract import ReadContract
        from hero_integration.sources.hero import GraphQLServiceSource
        opts = {'version': 2, 'role': role, 'max_records': maximum}
        contract = ReadContract('service', 1, 'mapping', 1, target, 'company.partners', 'user.id', (),
            variables or {'include_deleted': True}, page_size, materialized_request=materialized, adapter_options=opts)
        calls = []
        class Client:
            def query(self, document, supplied):
                calls.append((document, supplied))
                return {'company': {'partners': records if records is not None else [{'user': {'id': 7}}, {'user': {'id': 8}}]}}
        service = NS(query_document='query users { company { partners { user { id } } } }')
        source = GraphQLServiceSource(Client(), service, contract, 'source-a', revalidate=lambda: contract)
        return source, contract, calls

    def test_nonpaginated_lookup_returns_one_terminal_page_without_inventing_offset(self):
        source, contract, calls = self.source()
        page = source._page(0, 'HeroLookup')
        self.assertEqual(len(page.records), 2)
        self.assertIsNone(page.next_offset)
        self.assertEqual(calls[0][1], {'include_deleted': True})
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError): source._page(1, 'HeroLookup')
        self.assertEqual(len(calls), 1)

    def test_nonpaginated_lookup_uses_max_records_instead_of_unrelated_native_page_size(self):
        source, contract, calls = self.source(page_size=1, maximum=2)
        self.assertEqual(len(source._page(0, 'HeroLookup').records), 2)
        source, contract, calls = self.source(page_size=100, maximum=1)
        with self.assertRaisesRegex(ValueError, 'lookup bound'): source._page(0, 'HeroLookup')

    def test_offset_lookup_retains_existing_progress_and_page_size_checks(self):
        source, contract, calls = self.source(variables={'first': '$page_size', 'offset': '$offset'}, page_size=2)
        self.assertEqual(source._page(0, 'HeroLookup').next_offset, 2)
        self.assertEqual(source._page(2, 'HeroLookup').next_offset, 4)
        self.assertEqual(calls[1][1], {'first': 2, 'offset': 2})
        source, contract, calls = self.source(variables={'first': '$page_size', 'offset': '$offset'}, page_size=1)
        with self.assertRaises(ValueError): source._page(0, 'HeroLookup')

    def test_missing_role_revalidation_or_bound_blocks_before_transport(self):
        for role, maximum in [('QUERY', 5), ('LOOKUP', None), ('LOOKUP', 0), ('LOOKUP', True)]:
            source, contract, calls = self.source(role=role, maximum=maximum)
            with self.subTest(role=role, maximum=maximum), self.assertRaises(ValueError): source._page(0, 'HeroLookup')
            self.assertEqual(calls, [])
        source, contract, calls = self.source()
        source.revalidate = None
        with self.assertRaises(ValueError): source._page(0, 'HeroLookup')
        self.assertEqual(calls, [])

    def test_source_change_during_lookup_is_still_rejected(self):
        source, contract, calls = self.source()
        sequence = iter([contract, replace(contract, mapping_revision=2)])
        source.revalidate = lambda: next(sequence)
        with self.assertRaisesRegex(ValueError, 'during transport'): source._page(0, 'HeroLookup')
        self.assertEqual(len(calls), 1)

    def test_materialized_missing_offset_exception_is_restricted_to_lookup(self):
        request = NS(variables={}, argument_bindings=())
        source, contract, calls = self.source(materialized=request)
        with patch('hero_integration.graphql_protocol.graphql_result_decoder.decode_graphql_result') as decode:
            self.assertIsNone(source._page(0, 'HeroLookup').next_offset)
        decode.assert_called_once()
        source, contract, calls = self.source(materialized=request, target='HeroTasks')
        with self.assertRaisesRegex(ValueError, 'offset binding'): source._page(0, 'HeroTasks')
        self.assertEqual(calls, [])


class TestExplicitNullDefaults(TestCase):
    def test_default_applies_to_explicit_null_but_never_to_missing_data(self):
        from hero_integration.services.direct_mapping import MISSING
        spec = rule('text', 'title', 'Task', 'subject', transform='Trim')
        options = {'version': 2, 'transform_options': {'text': {'default': 'Configured', 'target_type': 'Data'}}}
        self.assertEqual(qm.transform(spec, None, options), 'Configured')
        self.assertIs(qm.transform(spec, MISSING, options), MISSING)
        self.assertEqual(qm.transform(spec, ' Original ', options), 'Original')
        self.assertIs(qm.mapped_values(NS(target_doctype='Task', definitions=(spec,), adapter_options=options), {}).missing.__contains__('subject'), True)

    def test_defaults_are_typed_output_values_not_implicit_numeric_coercions(self):
        spec = rule('amount', 'amount', 'Task', 'custom_hero_amount', kind='float|null', transform='Identity')
        options = {'version': 2, 'transform_options': {'amount': {'default': 7, 'target_type': 'Currency'}}}
        self.assertEqual(qm.transform(spec, None, options), 7.0)
        metadata = NS(ensure_write=lambda dt: None, field=lambda dt, f: NS(fieldtype='Currency', options='', read_only=0, hidden=0))
        qm.validate_v2_spec(spec, metadata, options)
        options['transform_options']['amount']['default'] = 'not numeric'
        with self.assertRaises(ValueError): qm.validate_v2_spec(spec, metadata, options)
        with self.assertRaises(ValueError): qm.transform(spec, None, options)

    def test_value_map_null_entry_precedes_default_and_unknown_uses_default(self):
        spec = rule('type', 'type', 'Customer', 'customer_type', operation='HeroCustomers', transform='Value Map')
        options = {'version': 2, 'transform_options': {'type': {'map': {'null': 'Individual'}, 'default': 'Company', 'target_type': 'Select'}}}
        self.assertEqual(qm.transform(spec, None, options), 'Individual')
        self.assertEqual(qm.transform(spec, 'unknown', options), 'Company')

    def test_safe_html_default_is_sanitized(self):
        spec = rule('body', 'comment', 'Task', 'description', transform='Safe HTML')
        options = {'version': 2, 'transform_options': {'body': {'default': '<p onclick="alert(1)">Configured</p>', 'target_type': 'Text Editor'}}}
        body = qm.transform(spec, None, options)
        self.assertIn('Configured', body)
        self.assertNotIn('onclick', body)

    def test_technical_resolvers_identity_and_collections_cannot_have_defaults(self):
        cases = [replace(task_rules()[0], transformation='Positive Int To Text'),
                 rule('company', 'type', 'Project', 'company', operation='HeroProjectMatches', purpose='Confirmed Company', transform='Confirmed ERPNext Company'),
                 rule('customer', 'customer_id', 'Project', 'customer', operation='HeroProjectMatches', purpose='Customer Relation', transform='HERO Customer Link'),
                 rule('project', 'project_id', 'Task', 'project', purpose='Project Relation', transform='HERO Project Link'),
                 rule('time', 'start', 'Event', 'starts_on', operation='HeroCalendarEvents', transform='Native Adapter'),
                 rule('rows', 'resources', 'Event', 'custom_hero_resources', operation='HeroCalendarEvents', kind='json|null', transform='Table Rows')]
        for spec in cases:
            options = {'version': 2, 'transform_options': {spec.definition_id: {'default': None}}}
            with self.subTest(transform=spec.transformation), self.assertRaises(ValueError):
                qm.validate_v2_spec(spec, None, options)

    def test_unused_transform_option_keys_are_rejected(self):
        for transformation, invalid in [('Identity', {'map': {}}), ('Trim', {'timezone': 'UTC'}),
                ('Number', {'child_fields': {'x': 'custom_hero_x'}}), ('Value Map', {'link_doctype': 'User'})]:
            spec = rule('field', 'title', 'Task', 'custom_hero_value', transform=transformation)
            options = {'version': 2, 'transform_options': {'field': invalid}}
            with self.subTest(transformation=transformation), self.assertRaises(ValueError):
                qm.validate_v2_spec(spec, None, options)


class TestNullableLogbookBodies(TestCase):
    def context(self):
        fixture = TestLegacyLogbookIdentityBridge()
        fixture.setUp()
        fixture.legacy.source_id = 'unrelated-history'
        fixture.record['histories'][0]['custom_text'] = None
        return fixture

    def test_null_body_with_configured_heading_imports_the_heading(self):
        fixture = self.context()
        fixture.record['histories'][0]['custom_title'] = 'Configured heading'
        fixture.plan.adapter_options['logbook']['heading_paths'] = ['histories.custom_title']
        self.assertEqual(fixture.sync(), 1)
        self.assertEqual(len(fixture.created_comments), 1)
        self.assertIn('Configured heading', fixture.created_comments[0].content)
        self.assertNotIn('HERO Logbuch', fixture.created_comments[0].content)

    def test_null_body_without_heading_or_default_still_blocks_without_writes(self):
        fixture = self.context()
        with self.assertRaisesRegex(ValueError, 'headings are empty'): fixture.sync()
        self.assertEqual(fixture.created_comments, [])
        self.assertEqual(len(fixture.links), 1)

    def test_null_body_uses_only_an_explicitly_configured_default(self):
        fixture = self.context()
        fixture.plan.adapter_options.setdefault('transform_options', {})['text'] = {
            'default': '<p>Configured fallback</p>', 'target_type': 'Text Editor'}
        self.assertEqual(fixture.sync(), 1)
        self.assertEqual(fixture.created_comments[0].content, '<p>Configured fallback</p>')


class TestTechnicalRoleConsumesDeclaredOptions(TestCase):
    def test_coded_project_title_does_not_accept_unconsumed_defaults(self):
        spec = rule('title', 'project_title', 'Project', 'project_name', operation='HeroProjectMatches', purpose='Record Name')
        with self.assertRaises(ValueError):
            qm.validate_v2_spec(spec, None, {'version': 2, 'transform_options': {'title': {'default': 'Ignored'}}})

    def test_mislabeled_technical_roles_cannot_bypass_value_map_options(self):
        cases = [rule('project', 'project_id', 'Task', 'project', purpose='Project Relation', transform='Link Map'),
                 rule('company', 'type', 'Project', 'custom_hero_other', operation='HeroProjectMatches', purpose='Confirmed Company', transform='Value Map'),
                 rule('contact', 'type', 'Customer', 'customer_type', operation='HeroCustomers', purpose='Customer Primary Contact', transform='Value Map')]
        for spec in cases:
            with self.subTest(purpose=spec.purpose), self.assertRaises(ValueError):
                qm.validate_v2_spec(spec, None, {'version': 2})
