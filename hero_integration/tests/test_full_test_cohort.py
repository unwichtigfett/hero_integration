"""The frozen project sample owns only proven related identities."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import file_migration as fm, migration_testing as testing
from hero_integration.tests import test_file_migration as chain_tests


def reader(operation):
    rule = SimpleNamespace(transformation='HERO Customer Link', purpose='Customer Relation', source_path='customer_id', target_doctype='Project')
    return SimpleNamespace(operation=operation, origin='approved', mapping='mapping', mapping_version='v2',
                           plan=SimpleNamespace(source_identity=lambda row: str(row['id']), definitions=[rule]),
                           document='query', records_path='rows', variables={}, page_size=100)


class TestProjectContract(unittest.TestCase):
    def test_project_fifty_and_its_complete_dependency_graph_are_frozen_before_any_child(self):
        projects = [{'id': i, 'customer_id': 901, 'is_deleted': False, 'project_match_assignments': [{'user_id': 9000+i}]}
                    for i in range(1, 52)]
        tasks = [{'id': 701, 'target_project_match_id': 50, 'author_user_id': 9050, 'target_user_id': 9200},
                 {'id': 702, 'target_project_match_id': 51, 'author_user_id': 9051, 'target_user_id': 9201}]
        events = [{'id': 801, 'project_match_id': 50, 'partners': [{'id': 10050}]}]
        employees = [{'id': i+10000, 'user_id': i+9000} for i in range(1, 52)] + [{'id': 10200, 'user_id': 9200}]
        files = [{'id': i, 'uuid': str(i), 'category':'documents'} for i in [300, 301]]
        documents = {
            '300': {'id':300,'uuid':'300','file_upload_matches':[{'project_match_id':50}], 'file_upload_links':[],
                    'customer_document':{'project_match_id':50,'contact_id':902}},
            '301': {'id':301,'uuid':'301','file_upload_matches':[{'project_match_id':51}], 'file_upload_links':[],
                    'customer_document':{'project_match_id':51,'contact_id':901}},
        }
        pages = {'HeroProjectMatches':projects, 'HeroTasks':tasks, 'HeroCalendarEvents':events,
                 'HeroEmployees':employees, 'HeroDocuments':files}
        run = SimpleNamespace(name='FULL', budget_contract_version=2, testing_enabled=1, testing_max_actions=50)
        config = SimpleNamespace(source_instance='fixture', source_binding_revision=1)
        with patch.object(fm, 'bound_configuration', return_value=config), \
             patch.object(fm, 'frappe', SimpleNamespace(db=Mock())), \
             patch('hero_integration.services.file_transfer._client_and_downloader', return_value=('client',None,'fixture')), \
             patch('hero_integration.services.file_transfer.details', side_effect=lambda client,rows,*args,**kwargs: {r['uuid']:documents[r['uuid']] for r in rows}), \
             patch.object(fm.direct_runtime, 'approved_read', side_effect=lambda op, instance: reader(op)), \
             patch.object(fm.direct_runtime, 'fetch_page', side_effect=lambda client,read,offset: pages.get(read.operation, [])[offset:offset+100]):
            cohort = fm.prepare_related_test(run)
            state = {}
            for _ in range(20):
                if fm.discover_related_test_page(run, state, cohort, page_budget=1):
                    break
            else:
                self.fail('discovery did not terminate after bounded resumable slices')
        proof = testing.project_budget_evidence(cohort, 'digest')
        self.assertEqual(len(cohort['project_ids']), 50)
        self.assertNotIn('51', cohort['project_ids'])
        self.assertEqual(set(proof['members']['partner']), set(map(str,range(10001,10051)))|{'10200'})
        self.assertEqual(proof['members']['file_upload'], ['300'])
        self.assertEqual(proof['members']['Task'], ['701'])
        self.assertEqual(proof['members']['CalendarEvent'], ['801'])
        self.assertEqual(proof['members']['customer'], ['901','902'])
        self.assertIn(['file_upload','300','customer','902'],proof['edges'])
        self.assertEqual(proof['file_areas']['HeroDocuments'], 1)
        for edge in [['project','50','file_upload','300'],['project','50','Task','701'],['Task','701','User','9200']]:
            self.assertIn(edge, proof['edges'])
        self.assertEqual(len(set(map(tuple, proof['edges']))),len(proof['edges']))

    def test_identical_project_overlap_consumes_one_slot_and_executes_once(self):
        from hero_integration.sources.hero import SourcePage
        project = lambda i: {'id': i, 'customer_id': 901, 'project_match_assignments': [], 'is_deleted': False}
        rows = [project(1), project(1), project(2), project(3)]
        read = reader('HeroProjectMatches')
        with patch.object(fm.direct_runtime, 'fetch_page', side_effect=lambda client, read, ofs:rows[ofs:ofs+2]):
            cohort = testing.prepare_project_cohort(None, read, 2, version=2)
        self.assertEqual(cohort['project_ids'], ['1', '2'])
        source = SimpleNamespace(projects=lambda ofs: SourcePage(tuple(rows[ofs:ofs+2]), ofs+2))
        scoped = testing.RelatedProjectSource(source, 2, cohort)
        first = scoped.projects()
        second = scoped.projects(first.next_offset)
        self.assertEqual([r['id'] for r in first.records + second.records], [1,2])
        self.assertIsNone(second.next_offset)
        rows[1] = {**rows[1], 'customer_id': 902}
        with patch.object(fm.direct_runtime, 'fetch_page', return_value=rows), self.assertRaisesRegex(ValueError, 'changed'):
            testing.prepare_project_cohort(None, read, 2, version=2)

    def test_manager_binding_adds_only_its_partner_and_revalidates_that_relationship(self):
        from hero_integration.sources.hero import SourcePage
        read = reader('HeroProjectMatches')
        read.plan.definitions.append(SimpleNamespace(transformation='User By Partner ID', purpose='', source_path='partner_id'))
        project = {'id':1,'project_match_assignments':[], 'customer_id':9,'is_deleted':False,'partner_id':30}
        cohort = testing.project_cohort([project],read.plan,1,version=2)
        testing.collect_project_employees(cohort, [{'id':30,'user_id':77},{'id':31,'user_id':78}])
        proof = testing.project_budget_evidence(cohort,'digest')
        self.assertEqual(proof['members']['partner'], ['30'])
        self.assertIn(['project','1','partner','30'], proof['edges'])
        source = SimpleNamespace(projects=lambda offset:SourcePage(({**project,'partner_id':31},),None))
        with self.assertRaisesRegex(ValueError, 'changed its relationships'):
            testing.RelatedProjectSource(source,1,cohort).projects()

    def test_zero_project_and_bad_file_do_not_stop_document_or_picture_discovery(self):
        from hero_integration.services.file_transfer import project_file_proof, FileRelationshipError
        from hero_integration.services.deferred_references import file_dependencies
        for operation in ('HeroDocuments','HeroPictures'):
            cohort = {'project_ids':['7'],'customer_ids':['9'],'files':{},'graph':{'project':{'7':[]}},'unresolved':[]}
            def detail(uuid, project, customer):
                return {'uuid':uuid,'file_upload_matches':None,'file_upload_links':[],
                        'customer_document':{'project_match_id':project,'contact_id':customer}}
            details = {'a':detail('a',0,9),'b':detail('b',-1,9),'c':detail('c',7,None)}
            testing.collect_project_files(cohort, operation, [{'id':i,'uuid':v} for i,v in enumerate(details,1)], details)
            self.assertEqual(set(cohort['files'][operation]),{'3'})
            self.assertEqual(file_dependencies(details['a']),[('customer','9')])
            self.assertEqual(file_dependencies(details['c']),[('project','7')])
            self.assertTrue(project_file_proof(details['a'],cohort)['ambiguous_customer'])
            self.assertEqual([r[1] for r in cohort['unresolved']],['1','2'])
            for bad in (False,-1,{'id':7},'7','',0.5):
                with self.assertRaises(FileRelationshipError):
                    project_file_proof(detail('x',bad,None),cohort)

    def test_attachment_lookup_uses_only_the_project_and_customer_proven_for_this_file(self):
        from hero_integration.services import file_transfer as files
        detail = {'uuid':'fixture', 'file_upload_matches': [{'project_match_id':15}], 'file_upload_links':[
            {'project_match_id':99,'customer_id':88}], 'customer_document':{'project_match_id':15,'contact_id':17}}
        testing_scope = {'project_ids':['15'],'customer_ids':['17','88']}
        proof = files.project_file_proof(detail, testing_scope)
        with patch.object(files,'_bindings', side_effect=[{}, {'17':'own-customer'}]) as lookup:
            target = files.scoped_file_target(detail, 'synthetic', {
                'project_ids':proof['projects'], 'customer_ids':proof['customers']})
        self.assertEqual(target, ('Customer','own-customer'))
        self.assertEqual([call.kwargs['source_ids'] for call in lookup.call_args_list], [['15'],['17']])

    def test_owner_failures_propagate_through_shared_employee_to_all_affected_projects(self):
        selection = {'ids':['1','2','3','4'], 'evidence':{
            'members':{'Task':['t'], 'User':['u'], 'partner':['p']},
            'edges':[['project','1','Task','t'],['Task','t','User','u'],['project','2','User','u'],
                     ['User','u','partner','p'],['partner','p','User','u']],
            'missing_relationships':[]}}
        outcomes = {('project','1'):'processed',('project','2'):'processed',('project','3'):'processed',
                    ('Task','t'):'processed',('partner','p'):'failed'}
        statuses = testing.project_completion_issues(selection, outcomes, [])
        self.assertEqual(statuses, {('partner','p'):'failed', ('project','1'):'failed', ('project','2'):'failed',
                                    ('project','4'):'missing'})

    def test_incremental_full_executes_both_independent_area_steps_and_reports_missing_service(self):
        fixture = chain_tests.TestFullMigrationChain()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.db.runs['FULL'].update(testing_enabled=1, budget_contract_version=3)
        fm._save_full('FULL', {'step':7,'steps':list(fm.NEW_FULL_STEPS),'notes':[],
                               'child':'','completed_children':['project-child']})
        with patch.object(fm, 'bound_configuration', return_value=SimpleNamespace(source_instance='source')), \
                patch.object(fm, '_complete_project_outcomes', return_value=1) as closure, \
                patch('hero_integration.services.deferred_references.reconcile_full_page',return_value=True):
            fm.advance_full('FULL')
            fixture.finish_current()
            fixture.finish_current()
        self.assertEqual(fixture.started, [fm.PRODUCT_ACTION, fm.SERVICE_ACTION])
        closure.assert_called_once()
        self.assertEqual(fixture.db.runs['FULL']['state'],'Completed With Conflicts')
        selection = {'ids':['1'], 'evidence':{'members':{},'edges':[],
            'additional_cohorts':{'supply_product':['p'],'supply_service':['s']}}}
        self.assertEqual(testing.project_completion_issues(selection, {('project','1'):'processed',
            ('supply_product','p'):'processed'}, []), {('supply_service','s'):'missing'})

    def test_unproven_product_service_areas_do_not_dispatch_and_reconciliation_runs(self):
        fixture = chain_tests.TestFullMigrationChain()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.db.runs['FULL'].update(testing_enabled=1,budget_contract_version=2)
        state={'step':7,'steps':list(fm.NEW_FULL_STEPS),'notes':[],'child':'','completed_children':['c']}
        fm._save_full('FULL', state)
        with patch.object(fm, 'bound_configuration', return_value=SimpleNamespace(source_instance='source')), \
                patch.object(fm, '_complete_project_outcomes', return_value=0), \
                patch('hero_integration.services.deferred_references.reconcile_full_page',return_value=True) as reconcile:
            fm.advance_full('FULL')
        reconcile.assert_called_once()
        self.assertEqual(fixture.started, [])
        self.assertEqual(fixture.db.runs['FULL']['state'], 'Blocked')
        self.assertEqual(set(fm._full('FULL')['blocked_areas']),{'Product Migration','Service Migration'})


class TestProjectCohortMariaDB(unittest.TestCase):
    """Disposable-site probe of parent admission, child filtering and shared durable results."""

    def test_preflight_workers_and_closure(self):
        import json
        import os
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from uuid import uuid4

        import frappe
        from hero_integration.services import execution_budget, migration
        from hero_integration.sources.hero import SourcePage

        site = os.environ.get('HERO_EXECUTION_BUDGET_TEST_SITE')
        if not site:
            self.skipTest('requires the configured disposable MariaDB fixture')
        self.assertEqual(frappe.local.site, site)
        self.assertTrue(frappe.conf.allow_tests)
        for limit in (1, 10, 50, 100):
            with self.subTest(limit=limit):
                name = 'full-contract-' + uuid4().hex
                root = frappe.get_doc(dict(doctype='HERO Integration Run', name=name, state='Running',
                    action='Full Migration', mode='Execute', execution_budget_run=name, source_instance='fixture',
                    source_binding_revision=1, budget_contract_version=2, testing_enabled=1, testing_max_actions=limit))
                root.db_insert()
                fm._save_full(name, {'steps':list(fm.NEW_FULL_STEPS),'step':0,'child':'','notes':[]})
                frappe.db.commit()
                budget = execution_budget.for_run(root)
                def cleanup(run, budget_id):
                    frappe.db.rollback()
                    frappe.db.delete('HERO Budget Outcome', {'budget':budget_id})
                    frappe.db.delete('HERO Execution Budget', {'name':budget_id})
                    frappe.db.delete('HERO Integration Run', {'name':run})
                    frappe.db.delete('DefaultValue', {'defkey':fm.FULL_KEY.format(run)})
                    frappe.db.commit()
                self.addCleanup(cleanup, name, budget.budget_id)
                projects = [{'id':i,'customer_id':901,'is_deleted':False,
                    'project_match_assignments':[{'user_id':1000+i}]} for i in range(1, limit+2)]
                directory = [{'id':i+2000, 'user_id':i+1000} for i in range(1,limit+2)]
                inventory = {'HeroProjectMatches':projects, 'HeroEmployees':directory}
                def page(client, read, offset):
                    rows = inventory.get(read.operation, [])
                    return rows if read.operation == 'HeroEmployees' else rows[offset:offset+10]
                config = SimpleNamespace(source_instance='fixture', source_binding_revision=1)
                with patch.object(fm, 'bound_configuration', return_value=config), \
                     patch('hero_integration.services.file_transfer._client_and_downloader',return_value=(Mock(), None,'fixture')), \
                     patch.object(fm.direct_runtime, 'approved_read',side_effect=lambda op, instance: reader(op)), \
                     patch.object(fm.direct_runtime, 'fetch_page',side_effect=page), patch.object(fm,'_enqueue'), \
                     patch.object(fm, '_start_step', return_value='synthetic-child') as dispatch:
                    for _ in range(6):
                        fm.advance_full(name)
                        if dispatch.called:
                            break
                    dispatch.assert_called_once_with(fm.EMPLOYEE_ACTION, name)
                cohort = fm._load_related_test(name, expected_digest=fm._full(name)['related_test_digest'])
                selection = budget.selection()
                self.assertEqual(set(selection['ids']), set(map(str,range(1,limit+1))))
                self.assertEqual(len(cohort['employees']),limit)
                self.assertEqual(cohort['employees'].get(str(2000+limit)),[str(1000+limit)])

                barrier = Barrier(2)
                def child(scope):
                    frappe.init(site=site)
                    try:
                        frappe.connect()
                        barrier.wait(timeout=10)
                        if scope == 'partner':
                            for key, users in cohort['employees'].items():
                                budget.reserve_record('partner', key, [('User', users[0])])
                                budget.outcome('partner', key, 'processed')
                            frappe.db.commit()
                            return list(cohort['employees'])
                        source = SimpleNamespace(customers=lambda offset:SourcePage(({'id':901},{'id':902}), None),
                            projects=lambda offset:SourcePage(tuple(projects[offset:offset+10]),
                                offset+10 if offset+10 < len(projects) else None))
                        scoped = testing.RelatedProjectSource(source, limit, cohort)
                        run = SimpleNamespace(state='Running',mode='Execute',action='Project Migration',
                            name='synthetic-child',graphql_mapping='saved',
                            processed=0,created_records=0,skipped_records=0,conflict_records=0,
                            resume_phase='',resume_record_index=0,resume_offset=0,save=Mock(),reload=Mock())
                        touched = []
                        def write(record, run, plan, kind, callback):
                            touched.append((kind,record['id']))
                            return migration.Result('CREATED','synthetic target')
                        plan = reader('HeroProjectMatches').plan
                        plan.target_doctype = 'Project'
                        with execution_budget.run_scope(root), \
                             patch.object(migration,'_mapping_plan_for_run',return_value=plan), \
                             patch('hero_integration.services.deferred_references.remember'), \
                             patch.object(migration,'record_output'), patch.object(migration,'_migrate_with_binding',side_effect=write):
                            self.assertTrue(migration._process_phase(run, 'customer', scoped.customers))
                            self.assertTrue(migration._process_phase(run, 'project', scoped.projects))
                        frappe.db.commit()
                        return touched
                    finally:
                        frappe.destroy()
                with ThreadPoolExecutor(max_workers=2) as workers:
                    people, written = workers.map(child, ['partner','project'])
                self.assertEqual(set(people),set(map(str,range(2001,2001+limit))))
                self.assertEqual(written,[('customer',901),*(('project', i) for i in range(1,limit+1))])
                frappe.db.rollback()
                self.assertEqual(fm._complete_project_outcomes(name,['synthetic-child']),0)
                with self.assertRaises(execution_budget.BudgetReached):
                    budget.reserve_record('project', limit+1)
                self.assertFalse(budget.reserve_record('partner',2000+limit,[('User',1000+limit)]))
                budget.outcome('customer',901,'conflict')
                self.assertEqual(fm._complete_project_outcomes(name,['synthetic-child']),limit)
                broken = budget.report()
                self.assertEqual((broken['processed'],broken['failed']),(0,limit))
                budget.outcome('customer',901,'processed')
                self.assertEqual(fm._complete_project_outcomes(name,['synthetic-child']),0)
                report = budget.report()
                self.assertEqual((report['selected'],report['processed'],report['failed'],report['limit']),(limit,limit,0,limit))
                self.assertEqual(report['dependencies'],{'User':limit,'customer':1,'partner':limit})
                print('FULL_CONTRACT_RECEIPT', json.dumps({key:report[key] for key in
                    ('main_type','selected','processed','failed','conflicts','missing','dependencies','limit','cohort_digest')},
                    sort_keys=True))

    def test_incremental_full_history_and_independent_catalog_cohorts(self):
        """All three selections precede child dispatch; replay reads the frozen members."""
        import json
        import os
        from uuid import uuid4

        import frappe
        from hero_integration.services import direct_runtime, execution_budget
        from hero_integration.services.test_environment import require_environment

        site = os.environ.get('HERO_EXECUTION_BUDGET_TEST_SITE')
        if not site:
            self.skipTest('requires an explicitly selected disposable MariaDB site')
        self.assertEqual(frappe.local.site, site)
        self.assertTrue(frappe.conf.allow_tests)
        if site.startswith('hero-test-'):
            require_environment()
        else:
            from hero_integration.tests import test_incremental_selection
            fixture = test_incremental_selection.TestIncrementalSelection()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
        source = frappe.db.get_single_value('HERO Integration Settings', 'hero_source_instance') or 'default'
        revision = int(frappe.db.get_single_value('HERO Integration Settings','source_binding_revision') or 0)
        config = SimpleNamespace(source_instance=source, source_binding_revision=revision)
        base = 780000
        ids = list(range(base + 1, base + 116))
        inventory = {
            'HeroProjectMatches':[dict(id=i, customer_id=i+1000, is_deleted=False,
                project_match_assignments=[dict(user_id=i+2000)]) for i in ids[:110]],
            'HeroEmployees':[dict(id=i+3000,user_id=i+2000) for i in ids[:110]],
            'HeroProducts':[dict(product_id=str(i+4000)) for i in ids],
            'HeroServices':[dict(id=i+5000) for i in ids[:113]],
        }
        def approved(operation, instance=source):
            plan = reader(operation).plan
            if operation == 'HeroProducts':
                plan.source_identity = lambda row: str(row['product_id'])
            return direct_runtime.DirectRead(operation, plan, 'query', 'rows', {}, 17,'approved','mapping',
                                             service_revision=3,validate_write=lambda: None)

        def fetch(client, read, offset, size=None):
            rows = inventory.get(read.operation, ())
            return rows if read.operation == 'HeroEmployees' else rows[offset:offset+(size or read.page_size)]

        cleanup_runs, cleanup_budgets = [], []
        def cleanup():
            frappe.db.rollback()
            for run_name in cleanup_runs:
                frappe.db.delete('HERO Integration Run',{'name':run_name})
                for key in (fm.FULL_KEY.format(run_name),fm.CHILD_KEY.format(run_name)):
                    frappe.db.delete('DefaultValue',{'parent':'__default','defkey':key})
            for key in cleanup_budgets:
                frappe.db.delete('HERO Budget Outcome',{'budget':key})
                frappe.db.delete('HERO Execution Budget',{'name':key})
            frappe.db.commit()
        self.addCleanup(cleanup)

        def run_doc(action, maximum, owner=None):
            name = 'test-05-' + uuid4().hex
            run = frappe.get_doc(dict(doctype='HERO Integration Run',name=name,action=action,mode='Execute',
                state='Running',testing_enabled=1,testing_max_actions=maximum,budget_contract_version=3,
                execution_budget_run=owner or name,source_instance=source,source_binding_revision=revision,
                graphql_service='approved',graphql_mapping='mapping',service_revision=3))
            run.db_insert()
            if owner:
                frappe.db.set_default(fm.CHILD_KEY.format(name),owner)
            cleanup_runs.append(name)
            return run

        def emit(client, *, read, **kwargs):
            budget = execution_budget.current_budget()
            kind = {'HeroProducts':'supply_product','HeroServices':'supply_service'}[read.operation]
            for row in read.testing_rows:
                key = read.plan.source_identity(row)
                budget.reserve_record(kind,key)
                budget.outcome(kind,key,'processed')
            return {'counts':{'updated':len(read.testing_rows)},'done':True}
        from contextlib import ExitStack
        with ExitStack() as stack:
            for mock in (patch.object(fm,'bound_configuration',return_value=config),
                         patch.object(fm,'_direct_reader',side_effect=lambda run:approved(fm.DIRECT_OPERATIONS[run.action])),
                         patch.object(fm,'_check_direct_contract'),patch.object(fm,'_enqueue'),
                         patch.object(fm,'on_run_finished'),
                         patch('hero_integration.services.file_transfer._client_and_downloader',return_value=(Mock(),None,source)),
                         patch.object(direct_runtime,'approved_read',side_effect=approved),
                         patch.object(direct_runtime,'fetch_page',side_effect=fetch),
                         patch('hero_integration.services.product_migration.sync_products',side_effect=emit),
                         patch('hero_integration.services.service_migration.sync_services',side_effect=emit)):
                stack.enter_context(mock)
            offset = 0
            for limit in (50,50,10,10):
                root = run_doc(fm.FULL_ACTION,limit)
                fm._save_full(root.name,{'steps':list(fm.NEW_FULL_STEPS),'step':0,'child':'','notes':[]})
                frappe.db.commit()
                budget = execution_budget.for_run(root)
                cleanup_budgets.append(budget.budget_id)
                with patch.object(fm,'_start_step',return_value='first-child') as dispatch:
                    for _ in range(8):
                        fm.advance_full(root.name)
                        if dispatch.called:
                            break
                    dispatch.assert_called_once_with(fm.EMPLOYEE_ACTION,root.name)
                cohort = fm._load_related_test(root.name,expected_digest=fm._full(root.name)['related_test_digest'])
                projects = [str(row['id']) for row in inventory['HeroProjectMatches'][offset:offset+limit]]
                products = [row['product_id'] for row in inventory['HeroProducts'][offset:offset+limit]]
                services = [str(row['id']) for row in inventory['HeroServices'][offset:offset+limit]]
                self.assertEqual(set(cohort['project_ids']),set(projects))
                self.assertEqual(cohort['additional_cohorts'],{'supply_product':products,'supply_service':services})
                frozen = budget.selection()['digest']
                # Re-enter parent preflight before the first child's writes.
                self.assertEqual(fm.prepare_related_test(root)['additional_cohorts'],cohort['additional_cohorts'])
                self.assertEqual(frozen,budget.selection()['digest'])
                for key in projects:
                    budget.reserve_record('project',key)
                    budget.outcome('project',key,'processed')
                for kind, records in [('customer',cohort['customer_ids']),('partner',cohort['employees'])]:
                    for key in records:
                        budget.reserve_record(kind,key)
                        budget.outcome(kind,key,'processed')
                frappe.db.commit()
                children = []
                for action in (fm.PRODUCT_ACTION,fm.SERVICE_ACTION):
                    child = run_doc(action,limit,root.name)
                    children.append(child.name)
                    frappe.db.commit()
                    with execution_budget.run_scope(child):
                        fm._process_file_slice(child,config)
                    self.assertEqual(frappe.db.get_value('HERO Integration Run',child.name,'state'),'Completed')
                state = fm._full(root.name)
                state.update(step=len(fm.NEW_FULL_STEPS),child='',completed_children=children)
                fm._save_full(root.name,state)
                frappe.db.commit()
                with patch('hero_integration.services.deferred_references.reconcile_full_page',return_value=True):
                    fm.advance_full(root.name)
                self.assertEqual(frappe.db.get_value('HERO Integration Run',root.name,'state'),'Completed')
                report = budget.report()
                self.assertEqual((report['selected'],report['processed'],report['failed']),(len(projects),len(projects),0))
                self.assertEqual({k:len(v) for k,v in report['project_dependencies'].items()},
                                 {'customer':len(projects),'partner':len(projects),'User':len(projects)} if projects else {})
                for kind, expected in [('supply_product', products),('supply_service', services)]:
                    group = report['additional_cohorts'][kind]
                    self.assertEqual((group['ids'],group['processed'],group['missing'],group['failed']),
                                     (expected,len(expected),0,0))
                print('INCREMENTAL_FULL_RESULT',json.dumps({key:report[key] for key in
                    ('limit','cohort_digest','project_ids','selected','processed','failed','missing','conflicts',
                     'additional_cohorts','project_dependencies')},sort_keys=True))
                offset += limit


class TestHistoricalCompletion(unittest.TestCase):
    def test_verified_deletion_clears_only_its_specific_directory_issue(self):
        entries = [['User','8','partner','missing or ambiguous employee'],
                   ['partner','3','employee','missing employee']]
        selection = {'ids':['p'], 'evidence':{'members':{'User':['8']}, 'missing_relationships':entries,
                     'edges':[['project','p','User','8'], ['project','p','partner','3']]}}
        outcomes = {('project','p'):'processed'}
        proof = {('User','8'), ('partner','3')}
        self.assertEqual(testing.project_completion_issues(selection,outcomes,[],historical=proof), {})
        self.assertIn(('project','p'), testing.project_completion_issues(selection,outcomes,[],historical={('User','8')}))
        entries.append(['User','8','partner','unproven relation'])
        self.assertIn(('project','p'), testing.project_completion_issues(selection,outcomes,[],historical=proof))

    def test_historical_proof_is_bound_to_cohort_and_source_revision(self):
        proof = {'digest':'sample','source':'source-a','revision':3,'users':['8'],'partners':['3']}
        state = {'related_test_digest':'sample','historical_employees':proof}
        self.assertEqual(fm.historical_employee_ids(state,'source-a',3), (['8'],['3']))
        with self.assertRaises(fm.SourceBindingError):
            fm.historical_employee_ids(state,'source-a',4)
