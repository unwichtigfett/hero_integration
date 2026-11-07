"""Native deferred bindings on an explicitly selected disposable Frappe site."""
from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from hero_integration.services import deferred_references as deferred, migration
from hero_integration.services.direct_mapping import MappingSpec
from hero_integration.services.mapping_runtime import plan_from_definitions


class TestSyntheticDeferredMariaDB(TestCase):
    def setUp(self):
        if (not os.environ.get('HERO_TEST_PRIVATE_SITE') or os.environ['HERO_TEST_PRIVATE_SITE'] != frappe.local.site or
                os.environ.get('HERO_SYNTHETIC_BINDING_TEST') != '1'):
            self.skipTest('Requires explicitly selected synthetic Frappe site')
        frappe.set_user('Administrator')
        self.source = 'synthetic-binding-' + uuid4().hex[:10]
        self.scope = {'confirmed': True, 'source_instance': self.source, 'source_type': 'Task'}
        rules = [
            MappingSpec('HeroTasks', 'id', 'HERO Integration Link', 'source_id', 'int',
                        'Source Identity', 'Positive Int To Text', definition_id='synthetic-id'),
            MappingSpec('HeroTasks', 'title', 'Task', 'subject', 'string|null',
                        'Record Name', 'Trim', definition_id='synthetic-title'),
            MappingSpec('HeroTasks', 'target_project_match_id', 'Task', 'project', 'int|null',
                        'Project Relation', 'HERO Project Link', 'HERO Project Link', definition_id='synthetic-project'),
        ]
        self.plan = plan_from_definitions('HeroTasks', rules, target_operation='HeroTasks', adapter_options={'version': 2})
        self.row = {'id': 10001, 'title': 'Synthetic deferred task ' + uuid4().hex[:10], 'is_deleted': False,
                    'target_project_match_id': 20001, '_hero_source_instance': self.source}
        self.run = frappe.get_doc({'doctype': 'HERO Integration Run', 'action': 'Projection Migration', 'mode': 'Execute',
            'state': 'Completed', 'source_instance': self.source, 'source_binding_revision': 1}).insert()

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_messages()

    def test_area_resume_and_test_limits_ignore_stale_defaults_cache(self):
        import json
        from hero_integration.services import product_migration, file_transfer, migration_testing
        key = 'hero_synthetic_checkpoint:' + self.source
        payload = {'offset': 7, 'counts': {'added': 3}, 'testing_record_limit': 5}
        keys = [key, key + ':documents', 'hero_testing_migration:' + self.run.name + ':HeroProducts']
        for identity in keys:
            frappe.db.set_default(identity, json.dumps(payload))
        get_default = frappe.db.get_default
        def stale_default(identity, *args, **kwargs):
            return '{"offset":0}' if identity in keys else get_default(identity, *args, **kwargs)
        with patch.object(frappe.db, 'get_default', side_effect=stale_default), \
                patch.object(product_migration, 'STATE_KEY', key), \
                patch.object(file_transfer, 'STATE_KEY', key + ':{}'):
            self.assertEqual(product_migration._state(), payload)
            self.assertEqual(file_transfer._state('documents'), payload)
            self.assertEqual(migration_testing.test_state(self.run.name, 'HeroProducts'), payload)

    def test_project_completion_hands_off_to_one_event_child_without_an_extra_job(self):
        from hero_integration.services import file_migration as fm
        parent=frappe.get_doc({'doctype':'HERO Integration Run','action':fm.FULL_ACTION,'mode':'Execute',
            'state':'Running','source_instance':self.source,'source_binding_revision':1}).insert()
        frappe.db.set_default(fm.CHILD_KEY.format(self.run.name),parent.name)
        fm._save_full(parent.name,{'steps':list(fm.NEW_FULL_STEPS),'step':2,'child':self.run.name,
            'completed_children':[],'notes':[]})
        started=[]
        get_default=frappe.db.get_default
        def stale_default(key,*args,**kwargs):
            if key.startswith(fm.CHILD_KEY.split('{}')[0]):
                return None
            if key.startswith(fm.FULL_KEY.split('{}')[0]):
                return '{"step":0,"child":"","notes":[]}'
            return get_default(key,*args,**kwargs)
        def start_step(action,name):
            self.assertEqual((action,name),('Schedule Migration',parent.name))
            child=frappe.get_doc({'doctype':'HERO Integration Run','action':action,'mode':'Execute',
                'state':'Queued','source_instance':self.source,'source_binding_revision':1}).insert()
            frappe.db.set_default(fm.CHILD_KEY.format(child.name),name)
            started.append(child.name)
            return child.name
        try:
            with patch.object(frappe,'enqueue') as enqueue, \
                    patch.object(frappe.db,'get_default',side_effect=stale_default), \
                    patch.object(fm,'bound_configuration',return_value=SimpleNamespace(source_instance=self.source)), \
                    patch.object(fm,'_start_step',side_effect=start_step):
                fm.on_run_finished(self.run.name)
                state=fm._full(parent.name)
                self.assertEqual(state['step'],3)
                self.assertEqual(state['child'],started[0])
                self.assertEqual(state['completed_children'],[self.run.name])
                self.assertEqual(frappe.db.get_value('HERO Integration Run',started[0],'state'),'Queued')
                fm.on_run_finished(self.run.name)
                self.assertEqual(len(started),1)
                frappe.db.set_value('HERO Integration Run',parent.name,'state','Cancelled')
                frappe.db.commit()
                fm.on_run_finished(self.run.name)
                self.assertEqual(len(started),1)
                self.assertEqual(frappe.db.get_value('HERO Integration Run',parent.name,'state'),'Cancelled')
                enqueue.assert_not_called()
                frappe.db.set_value('HERO Integration Run',parent.name,'state','Running')
                with patch.object(fm,'bound_configuration',side_effect=fm.SourceBindingError('Synthetic source changed')):
                    fm.on_run_finished(self.run.name)
                self.assertEqual(frappe.db.get_value('HERO Integration Run',parent.name,'state'),'Blocked')
                self.assertEqual(frappe.db.get_value('HERO Integration Run',self.run.name,'state'),'Completed')
                self.assertEqual(len(started),1)
        finally:
            frappe.db.rollback()
            keys=[fm.FULL_KEY.format(parent.name)]+[fm.CHILD_KEY.format(name) for name in [self.run.name]+started]
            frappe.db.delete('DefaultValue',{'parent':'__default','defkey':['in',keys]})
            frappe.db.delete('HERO Integration Run',{'source_instance':self.source})
            frappe.db.commit()

    def _task_and_pending(self):
        result = migration._migrate_mapped_task(self.row, self.run, self.plan, self.scope)
        self.assertEqual(result.code, 'CREATED')
        self.assertFalse(frappe.get_doc('Task', result.target_name).project)
        with patch.object(migration, '_mapping_plan_for_run', return_value=self.plan):
            deferred.remember(self.run, 'Task', self.row, result)
        pending = deferred.observation_key(self.source, 'HeroTasks', str(self.row['id']), self.run.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Waiting')
        return result.target_name, pending

    def test_same_external_identity_in_another_system_cannot_reuse_its_target(self):
        first = migration._migrate_mapped_task(self.row, self.run, self.plan, self.scope)
        self.assertEqual(first.code, 'CREATED')
        link = migration._link('Task', str(self.row['id']), self.source)
        link.source_system = 'SyntheticOther'
        link.save()
        row = {**self.row, 'title': self.row['title'] + ' HERO record'}
        second = migration._migrate_mapped_task(row, self.run, self.plan, self.scope)
        self.assertEqual(second.code, 'CREATED')
        self.assertNotEqual(second.target_name, first.target_name)
        self.assertEqual(frappe.db.get_value('Task', first.target_name, 'subject'), self.row['title'])
        self.assertEqual(migration._link('Task', str(self.row['id']), self.source).source_system, 'HERO')

    def _project(self, source_id):
        project = frappe.get_doc({'doctype': 'Project', 'project_name': 'Synthetic Project ' + uuid4().hex[:10],
                                  'company': os.environ.get('HERO_SYNTHETIC_COMPANY', 'Synthetic Test Company')}).insert()
        frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': 'project',
            'source_id': str(source_id), 'source_instance': self.source, 'target_doctype': 'Project',
            'target_name': project.name, 'sync_status': 'Synchronized', 'source_payload_hash': 'fixture',
            'target_payload_hash': 'fixture', 'mapping_version': 'fixture'}).insert()
        return project

    @contextmanager
    def _resolution_context(self):
        original = frappe.get_doc
        def get_doc(doctype, name=None, **kwargs):
            if doctype == 'HERO GraphQL Mapping' and not name:
                return SimpleNamespace(mapping_json='{}')
            return original(doctype, name, **kwargs) if name is not None else original(doctype, **kwargs)
        with patch('hero_integration.client.settings.get_configuration',
                   return_value=SimpleNamespace(source_instance=self.source, source_binding_confirmed=True, source_binding_revision=1)), \
             patch.object(migration, 'validate_run_mappings'), patch.object(migration, '_source_for_run',
                   return_value=SimpleNamespace(source_instance=self.source, source_binding_confirmed=True, source_binding_revision=1)), \
             patch.object(migration, '_mapping_plan_for_run', return_value=self.plan), \
             patch.object(migration, '_projection_task_scope', return_value=self.scope), \
             patch.object(frappe, 'get_doc', side_effect=get_doc):
            yield

    def _resolve(self, pending):
        with self._resolution_context():
            deferred.resolve_one(pending)

    def test_success_notification_realizes_only_the_completed_origin_area(self):
        task_name, pending = self._task_and_pending()
        origin = self.run
        frappe.db.set_value('HERO Integration Run',origin.name,'state','Running')
        self.run = frappe.get_doc({'doctype':'HERO Integration Run','action':'Projection Migration','mode':'Execute',
            'state':'Completed','source_instance':self.source,'source_binding_revision':1}).insert()
        self.row = {**self.row,'id':10002,'title':'Synthetic unrelated task '+uuid4().hex[:10]}
        other_task, other_pending = self._task_and_pending()
        project = self._project(20001)
        with patch.object(frappe,'enqueue') as enqueue:
            deferred.notify_completed_run(origin.name)
            enqueue.assert_not_called()
            frappe.db.set_value('HERO Integration Run',origin.name,'state','Completed')
            deferred.notify_completed_run(origin.name)
            self.assertEqual(enqueue.call_args.kwargs['run_name'],origin.name)
            with self._resolution_context():
                deferred.reconcile_completed_run(origin.name)
        self.assertEqual(frappe.get_doc('Task',task_name).project,project.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Resolved')
        self.assertFalse(frappe.get_doc('Task',other_task).project)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,other_pending,'status'),'Waiting')

    def test_bulk_recovery_deduplicates_jobs_and_does_not_authorize_later_writes(self):
        task_name, pending = self._task_and_pending()
        self._project(20001)
        frappe.db.set_value(deferred.MAIN, pending, 'realization_started', 1)
        for n in range(5):
            deferred.save(self.run.name, self.source, 'HeroTasks', str(30000+n), self.plan.mapping_version,
                {'id':30000+n}, [('project','20001')])
            frappe.db.set_value(deferred.MAIN,
                deferred.observation_key(self.source,'HeroTasks',str(30000+n),self.run.name),'realization_started',1)
        original=frappe.get_all
        def scoped(doctype,*args,**kwargs):
            if doctype==deferred.MAIN:
                kwargs['filters']={**kwargs.get('filters',{}),'source_instance':self.source}
            return original(doctype,*args,**kwargs)
        from hero_integration.services.performance import collect
        with patch.object(frappe,'get_all',side_effect=scoped), patch.object(frappe,'enqueue') as enqueue, collect() as stats:
            deferred.recover_pending()
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs['identity'],'20001')
        self.assertIn('binding.availability_bulk',stats)
        self.assertFalse(frappe.get_doc('Task',task_name).project)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Waiting')
        link=frappe.get_all('HERO Integration Link',filters={'source_instance':self.source,
            'source_type':'project','source_id':'20001'},pluck='name')[0]
        frappe.db.set_value('HERO Integration Link',link,'sync_status','Rejected')
        self._resolve(pending)
        self.assertFalse(frappe.get_doc('Task',task_name).project)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Waiting')

    def test_bulk_availability_matches_fresh_checks_and_observes_target_deletion(self):
        project=self._project(20001)
        demands=[('project','20001'),('project','missing')]
        before=deferred.available_many(self.source,demands)
        self.assertEqual(before,{(kind,identity):deferred.available(self.source,kind,identity) for kind,identity in demands})
        frappe.db.delete('Project',{'name':project.name})
        self.assertFalse(deferred.available_many(self.source,demands)['project','20001'])

    def test_missing_frozen_observation_is_rejected_before_target_or_attempt_writes(self):
        task,pending=self._task_and_pending()
        frappe.db.set_value(deferred.MAIN,pending,'record_json','')
        self._project(20001)
        with self.assertRaises(deferred.PendingObservationError):
            self._resolve(pending)
        self.assertFalse(frappe.get_doc('Task',task).project)
        self.assertFalse(frappe.db.get_value(deferred.MAIN,pending,'realization_started'))
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Waiting')

    def test_resolved_empty_observation_stays_terminal_without_replay(self):
        task,pending=self._task_and_pending()
        frappe.db.set_value(deferred.MAIN,pending,{'status':'Resolved','record_json':''})
        self._resolve(pending)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Resolved')
        self.assertFalse(frappe.get_doc('Task',task).project)

    def test_task_is_created_without_project_and_linked_after_project_arrives(self):
        task_name, pending = self._task_and_pending()
        project = self._project(20001)
        self._resolve(pending)
        self.assertEqual(frappe.get_doc('Task', task_name).project, project.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')
        self.assertFalse(frappe.db.get_value(deferred.MAIN, pending, 'record_json'))

    def test_available_project_is_not_realized_until_origin_run_completed(self):
        task_name, pending = self._task_and_pending()
        project = self._project(20001)
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Running')
        self._resolve(pending)
        self.assertFalse(frappe.get_doc('Task', task_name).project)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Waiting')
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Completed')
        self._resolve(pending)
        self.assertEqual(frappe.get_doc('Task', task_name).project, project.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')

    def test_project_bound_before_import_waits_for_successful_origin(self):
        project = self._project(20001)
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Running')
        task_name, pending = self._task_and_pending()
        saved = frappe.get_doc(deferred.MAIN, pending)
        self.assertEqual([(row.source_type, row.source_id) for row in saved.dependencies], [('project', '20001')])
        self._resolve(pending)
        self.assertFalse(frappe.get_doc('Task', task_name).project)
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Completed')
        self._resolve(pending)
        self.assertEqual(frappe.get_doc('Task', task_name).project, project.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')

    def test_stale_task_observation_cannot_replay_after_source_or_target_binding_changes(self):
        task_name, pending = self._task_and_pending()
        self._project(20001)
        other = frappe.get_doc({'doctype':'Task','subject':'Synthetic replacement '+uuid4().hex[:10]}).insert()
        link = migration._link('Task','10001',self.source)
        changes = {'source_payload_hash':'newer-source', 'observed_source_hash':'newer-observation',
                   'mapping_version':'newer-mapping', 'target_name':other.name}
        for field, changed in changes.items():
            original = link.get(field)
            frappe.db.set_value('HERO Integration Link',link.name,field,changed)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,'source observation or binding changed'):
                self._resolve(pending)
            self.assertFalse(frappe.get_doc('Task',task_name).project)
            self.assertFalse(frappe.get_doc('Task',other.name).project)
            frappe.db.set_value('HERO Integration Link',link.name,field,original)
        self._resolve(pending)
        self.assertTrue(frappe.get_doc('Task',task_name).project)

    def test_task_binding_worker_skips_full_intake_and_keeps_scalar_baselines(self):
        from hero_integration.services.change_detection import decode
        task_name,pending=self._task_and_pending()
        project=self._project(20001)
        before=migration._link('Task','10001',self.source)
        baseline=decode(before.target_field_hashes)['subject']
        frappe.db.set_value('Task',task_name,'subject','Local task title')
        with patch.object(migration,'_migrate_mapped_task',side_effect=AssertionError('Full Task intake')):
            self._resolve(pending)
        self.assertEqual(frappe.get_doc('Task',task_name).project,project.name)
        self.assertEqual(frappe.get_doc('Task',task_name).subject,'Local task title')
        after=migration._link('Task','10001',self.source)
        self.assertEqual(decode(after.target_field_hashes)['subject'],baseline)

    def test_project_binding_worker_skips_project_and_logbook_intake(self):
        customer=frappe.get_doc({'doctype':'Customer','customer_name':'Synthetic binding '+uuid4().hex[:10],
            'customer_type':'Individual','customer_group':'Commercial','territory':'Germany'}).insert()
        frappe.get_doc({'doctype':'HERO Integration Link','source_system':'HERO','source_instance':self.source,
            'source_type':'customer','source_id':'30003','target_doctype':'Customer','target_name':customer.name,
            'sync_status':'Synchronized','source_payload_hash':'fixture','target_payload_hash':'fixture',
            'mapping_version':'fixture'}).insert()
        rules=[MappingSpec('HeroProjectMatches','id','HERO Integration Link','source_id','int',
                'Source Identity','Positive Int To Text',definition_id='project-id'),
            MappingSpec('HeroProjectMatches','name','Project','project_name','string|null',
                'Record Name','Trim',definition_id='project-name'),
            MappingSpec('HeroProjectMatches','customer_id','Project','customer','int|null',
                'Customer Relation','HERO Customer Link','HERO Customer Link',definition_id='project-customer'),
            MappingSpec('HeroProjectMatches','project_type','Project','company','string|null',
                'Confirmed Company','Confirmed ERPNext Company',definition_id='project-company')]
        self.plan=plan_from_definitions('HeroProjectMatches',rules,target_operation='HeroProjectMatches',
                                        adapter_options={'version':2})
        row={'id':20002,'name':'Synthetic projected binding '+uuid4().hex[:10],'customer_id':30003,
             '_hero_source_instance':self.source,'project_match_assignments':[]}
        result=migration._upsert('project','20002',row,{'project_name':row['name'],
            'company':os.environ['HERO_SYNTHETIC_COMPANY']},'Project','project_name',self.plan.mapping_version,
            self.run,source_instance=self.source)
        self.assertEqual(result.code,'CREATED')
        with patch.object(migration,'_mapping_plan_for_run',return_value=self.plan):
            deferred.remember(self.run,'project',row,result)
        pending=deferred.observation_key(self.source,'HeroProjectMatches','20002',self.run.name)
        with patch.object(migration,'_migrate_project',side_effect=AssertionError('Full Project intake')), \
             patch('hero_integration.services.project_logbook.sync_mapped_entries',side_effect=AssertionError('Logbook replay')):
            self._resolve(pending)
        self.assertEqual(frappe.get_doc('Project',result.target_name).customer,customer.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Resolved')

    def _service_observation(self, file_upload=None):
        from hero_integration.services import service_migration as services, direct_runtime
        code = 'SYNTHETIC-SERVICE-' + uuid4().hex[:10]
        group = frappe.get_all('Item Group', filters={'is_group':0}, pluck='name', limit=1)[0]
        uom = frappe.get_all('UOM', pluck='name', limit=1)[0]
        suffix = uuid4().hex[:10]
        positions = [{'__typename':'Documents_SupplyProduct', 'id':'material-'+suffix,
            'name':'Synthetic material', 'quantity':2, 'net_price_per_unit':5},
            {'__typename':'WageGroup', 'id':'labor-'+suffix, 'name':'Synthetic labor '+suffix,
             'quantity':1, 'wage_per_hour':60, 'wage_cost_price':40}]
        observed = services.composition(positions, self.source, realize=False)
        item = frappe.get_doc({'doctype':'Item', 'item_code':code, 'item_name':code, 'is_stock_item':0,
            'item_group':group, 'stock_uom':uom, services.CALCULATION:observed}).insert()
        row = {'id':82001, 'modified':'frozen', 'company_id':1}
        detail = {**row, 'positions':positions, 'file_upload':file_upload}
        read = direct_runtime.builtin_read('HeroServices')
        item.reload()  # Same persisted child-row baseline as the actual importer.
        values = services.normalized(item, {'item_name',services.CALCULATION})
        services.write_link(None, self.source, services.SOURCE_TYPE, '82001', item,
            'frozen-service-observation', values)
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Running')
        company = os.environ['HERO_SYNTHETIC_COMPANY']
        services.remember_relationships(self.run.name, self.source, company, read, row, detail, 'added')
        pending = deferred.observation_key(self.source, 'HeroServices', '82001', self.run.name)
        return services, read, item, positions, pending, company

    def _service_material(self, service_item, source_id):
        code = 'SYNTHETIC-MATERIAL-' + uuid4().hex[:10]
        item = frappe.get_doc({'doctype':'Item', 'item_code':code, 'item_name':code, 'is_stock_item':0,
            'item_group':service_item.item_group, 'stock_uom':service_item.stock_uom}).insert()
        frappe.get_doc({'doctype':'HERO Integration Link', 'source_system':'HERO', 'source_type':'supply_product',
            'source_id':source_id, 'source_instance':self.source, 'target_doctype':'Item', 'target_name':item.name,
            'sync_status':'Synchronized', 'source_payload_hash':'fixture', 'target_payload_hash':'fixture',
            'mapping_version':'fixture'}).insert()
        return item

    def _resolve_service(self, services, read, pending, company):
        with patch.object(services.runtime, 'resolve_read', return_value=read), \
             patch.object(services, 'preflight', return_value=(company,'fixture-schema')):
            self._resolve(pending)

    def test_service_material_and_labor_links_wait_for_successful_origin(self):
        services, read, item, positions, pending, company = self._service_observation()
        material = self._service_material(item, positions[0]['id'])
        self._resolve_service(services, read, pending, company)
        item.reload()
        self.assertFalse(item.get(services.CALCULATION)[0].item_code)
        self.assertFalse(frappe.db.exists('Activity Type', positions[1]['name']))
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Completed')
        self._resolve_service(services, read, pending, company)
        item.reload()
        rows = item.get(services.CALCULATION)
        self.assertEqual(rows[0].item_code, material.name)
        self.assertEqual(rows[1].activity_type, positions[1]['name'])
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')
        self.assertEqual({(row.source_type,row.source_id) for row in frappe.get_doc(deferred.MAIN,pending).dependencies},
            {('supply_product',positions[0]['id']), ('WageGroup',positions[1]['id'])})

    def test_service_waits_for_missing_material_and_then_creates_owned_labor_binding(self):
        services, read, item, positions, pending, company = self._service_observation()
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Completed')
        self._resolve_service(services, read, pending, company)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'), 'Waiting')
        self.assertFalse(frappe.db.exists('Activity Type',positions[1]['name']))
        material = self._service_material(item,positions[0]['id'])
        self._resolve_service(services, read, pending, company)
        item.reload()
        self.assertEqual(item.get(services.CALCULATION)[0].item_code,material.name)
        self.assertTrue(item.get(services.CALCULATION)[1].activity_type)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'), 'Resolved')

    def test_local_service_calculation_edit_is_preserved_before_linking(self):
        services, read, item, positions, pending, company = self._service_observation()
        self._service_material(item,positions[0]['id'])
        item.get(services.CALCULATION)[0].quantity = 99
        item.save()
        frappe.db.set_value('HERO Integration Run',self.run.name,'state','Completed')
        with self.assertRaises(services.ServiceBlocked):
            self._resolve_service(services,read,pending,company)
        item.reload()
        self.assertEqual(item.get(services.CALCULATION)[0].quantity,99)
        self.assertFalse(item.get(services.CALCULATION)[0].item_code)
        self.assertFalse(frappe.db.exists('Activity Type',positions[1]['name']))

    def test_newer_service_source_observation_cannot_be_replaced_by_old_pending_calculation(self):
        services, read, item, positions, pending, company = self._service_observation()
        self._service_material(item,positions[0]['id'])
        link = services.scoped_link(self.source,services.SOURCE_TYPE,'82001','Item')
        frappe.db.set_value('HERO Integration Link',link.name,'source_payload_hash','newer-service-observation')
        frappe.db.set_value('HERO Integration Run',self.run.name,'state','Completed')
        with self.assertRaisesRegex(services.ServiceBlocked,'observation changed'):
            self._resolve_service(services,read,pending,company)
        item.reload()
        self.assertFalse(item.get(services.CALCULATION)[0].item_code)
        self.assertFalse(frappe.db.exists('Activity Type',positions[1]['name']))

    def test_service_attachment_refresh_and_native_file_wait_for_successful_origin(self):
        import json
        from pathlib import Path
        from unittest.mock import Mock
        file = {'id':94001, 'filename':'synthetic-service.txt', 'size':13,
                'modified':'frozen', 'url_download':'https://example.invalid/expired-secret'}
        services, read, item, positions, pending, company = self._service_observation(file)
        self._service_material(item, positions[0]['id'])
        saved = json.loads(frappe.get_doc(deferred.MAIN,pending).record_json)
        self.assertNotIn('url_download', saved['detail']['file_upload'])
        fresh = {'id':saved['detail']['id'],'company_id':saved['detail']['company_id'],
                 'file_upload':{**saved['detail']['file_upload'],
                  'url_download':'https://example.invalid/fresh-secret'}}
        client = SimpleNamespace(query=Mock(return_value={'supply_services':[fresh]}))
        def fetch(url, path, size):
            self.assertEqual(url, fresh['file_upload']['url_download'])
            Path(path).write_bytes(b'hello service')
        downloader = SimpleNamespace(fetch=Mock(side_effect=fetch))
        with patch('hero_integration.services.file_transfer._client_and_downloader',
                   return_value=(client,downloader,self.source)), \
             patch.object(services.runtime,'validated_snapshot'), \
             patch.object(services,'_attachment_request', return_value=SimpleNamespace(document='sealed',variables={})):
            self._resolve_service(services,read,pending,company)
            client.query.assert_not_called()
            downloader.fetch.assert_not_called()
            frappe.db.set_value('HERO Integration Run',self.run.name,'state','Completed')
            self._resolve_service(services,read,pending,company)
        client.query.assert_called_once()
        downloader.fetch.assert_called_once()
        files = frappe.get_all('File',filters={'attached_to_doctype':'Item','attached_to_name':item.name},pluck='name')
        self.assertEqual(len(files),1)
        attached = frappe.get_doc('File',files[0])
        self.assertTrue(attached.is_private)
        self.assertEqual(Path(attached.get_full_path()).read_bytes(), b'hello service')
        path = attached.get_full_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Resolved')

    def test_changed_service_attachment_observation_blocks_before_any_relationship_write(self):
        import json
        from unittest.mock import Mock
        services, read, item, positions, pending, company = self._service_observation(
            {'id':94002,'filename':'original.txt','size':1,'modified':'frozen'})
        self._service_material(item,positions[0]['id'])
        fresh = json.loads(frappe.get_doc(deferred.MAIN,pending).record_json)['detail']
        fresh['file_upload']['modified'] = 'newer'
        client = SimpleNamespace(query=Mock(return_value={'supply_services':[fresh]}))
        downloader = SimpleNamespace(fetch=Mock())
        frappe.db.set_value('HERO Integration Run',self.run.name,'state','Completed')
        with patch('hero_integration.services.file_transfer._client_and_downloader',
                   return_value=(client,downloader,self.source)), \
             patch.object(services.runtime,'validated_snapshot'), \
             patch.object(services,'_attachment_request',return_value=SimpleNamespace(document='sealed',variables={})), \
             self.assertRaises(services.ServiceBlocked):
            self._resolve_service(services,read,pending,company)
        downloader.fetch.assert_not_called()
        item.reload()
        self.assertFalse(item.get(services.CALCULATION)[0].item_code)
        self.assertFalse(frappe.db.exists('Activity Type',positions[1]['name']))

    def _employee_observation(self):
        from dataclasses import replace
        from hero_integration.services import employee_migration as employees, direct_runtime
        selected = {'id', 'first_name', 'last_name', 'full_name', 'status', 'phone', 'user.email'}
        template = direct_runtime.builtin_read('HeroEmployees')
        plan = replace(template.plan, definitions=tuple(rule for rule in template.plan.definitions if rule.source_path in selected))
        read = replace(template, plan=plan, mapping='synthetic-employee-mapping')
        if not frappe.db.exists('User Type', 'Website User'):
            frappe.get_doc({'doctype':'User Type', 'name':'Website User', 'is_standard':1}).insert()
        user_name = os.environ.get('HERO_SYNTHETIC_USER', 'synthetic@example.invalid')
        row = {'id':70001, 'user_id':71001, 'first_name':'Synthetic', 'last_name':'Deferred Employee',
            'full_name':'Synthetic Deferred Employee', 'status':'active', 'phone':'+49301234567',
            'user':{'id':71001, 'email':user_name}}
        company = os.environ['HERO_SYNTHETIC_COMPANY']
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Running')
        genders = frappe.get_all('Gender', pluck='name', limit=1)
        if not genders:
            frappe.get_doc({'doctype':'Gender', 'gender':'Synthetic Test Gender'}).insert()
            genders = ['Synthetic Test Gender']
        employee = frappe.get_doc({'doctype':'Employee', 'first_name':row['first_name'], 'last_name':row['last_name'],
            'status':'Active', 'company':company, 'date_of_joining':employees.JOINING_DATE,
            'gender':genders[0], 'date_of_birth':'1990-01-01', 'create_user_permission':0,
            'create_user_automatically':0}).insert()
        baseline = {name:employee.get(name) for name in ('first_name','last_name','status','company','date_of_joining')}
        employees._write_link(None, self.source, 'partner', row['id'], employee, baseline, row, read.mapping_version)
        self.assertEqual(employees.upsert(row, self.source, company, [], read=read), 'updated')
        employees.remember_relationships(self.run.name, self.source, company, read, row)
        pending = deferred.observation_key(self.source, 'HeroEmployees', '70001', self.run.name)
        saved = frappe.get_doc(deferred.MAIN, pending)
        self.assertEqual([(dep.source_type, dep.source_id) for dep in saved.dependencies],
                         [('User','71001'), ('partner','70001')])
        return employees, read, saved, company

    def _resolve_employee(self, employees, read, company, pending_name):
        original = frappe.get_doc
        def get_doc(doctype, name=None, **kwargs):
            if doctype == 'HERO GraphQL Mapping' and name == read.mapping:
                return SimpleNamespace(source_instance=self.source, status='Active',
                    definition_id='synthetic-employee-definition', mapping_revision=1)
            return original(doctype, name, **kwargs) if name is not None else original(doctype, **kwargs)
        with patch.object(frappe, 'get_doc', side_effect=get_doc), \
             patch.object(employees.runtime, 'resolve_read', return_value=read), \
             patch.object(employees, 'preflight', return_value=(company, read.mapping_version, read.document, read.records_path)):
            self._resolve(pending_name)

    def test_employee_user_relation_and_object_binding_wait_for_success(self):
        import json
        employees, read, pending, company = self._employee_observation()
        saved = json.loads(pending.record_json)
        employee_name = saved['employee_name']
        self.assertFalse(frappe.get_doc('Employee', employee_name).user_id)
        filters = {'source_instance':self.source, 'object_type':'User', 'external_object_id':'71001'}
        self.assertFalse(frappe.db.exists('HERO Object Binding', filters))
        self._resolve_employee(employees, read, company, pending.name)
        self.assertFalse(frappe.db.exists('HERO Object Binding', filters))
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Completed')
        with patch.object(employees,'upsert',side_effect=AssertionError('Employee scalar intake replay')):
            self._resolve_employee(employees, read, company, pending.name)
        self.assertEqual(frappe.get_doc('Employee', employee_name).user_id, saved['user_name'])
        self.assertTrue(frappe.db.exists('HERO Object Binding', filters))
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending.name, 'status'), 'Resolved')

    def test_new_employee_source_observation_prevents_stale_binding(self):
        import json
        employees, read, pending, company = self._employee_observation()
        saved = json.loads(pending.record_json)
        link = employees._link(self.source, 'User', 71001, 'User')
        frappe.db.set_value('HERO Integration Link', link.name, 'source_payload_hash', 'newer-source-observation')
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Completed')
        with self.assertRaises(employees.EmployeeMigrationBlocked):
            self._resolve_employee(employees, read, company, pending.name)
        self.assertFalse(frappe.get_doc('Employee', saved['employee_name']).user_id)
        self.assertFalse(frappe.db.exists('HERO Object Binding', {'source_instance':self.source}))

    def test_local_employee_edit_is_preserved_before_binding(self):
        import json
        employees, read, pending, company = self._employee_observation()
        employee_name = json.loads(pending.record_json)['employee_name']
        frappe.db.set_value('Employee', employee_name, 'first_name', 'Local employee edit')
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Completed')
        with self.assertRaises(employees.EmployeeMigrationBlocked):
            self._resolve_employee(employees, read, company, pending.name)
        self.assertEqual(frappe.db.get_value('Employee', employee_name, 'first_name'), 'Local employee edit')
        self.assertFalse(frappe.get_doc('Employee', employee_name).user_id)
        self.assertFalse(frappe.db.exists('HERO Object Binding', {'source_instance':self.source}))

    def test_primary_customer_contact_and_address_wait_for_success(self):
        from hero_integration.tests.mapping_fixture import fixture_plan
        self.plan = fixture_plan('HeroCustomers')
        record = {'id':60001, 'full_name':'Synthetic delayed customer ' + uuid4().hex[:10], 'company_name':None,
            'email':'synthetic-customer@example.invalid', 'is_deleted':False, '_hero_source_instance':self.source,
            'address':{'id':61001, 'street':'Synthetic Road 10', 'city':'Example City', 'zipcode':'12345',
                       'country':{'code':'de'}}}
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Running')
        with patch.object(migration, '_mapping_plan_for_run', return_value=self.plan):
            result = migration._migrate_customer(record, self.run)
            self.assertEqual(result.code, 'CREATED', result.detail)
            deferred.remember(self.run, 'customer', record, result)
        customer = frappe.get_doc('Customer', result.target_name)
        self.assertFalse(customer.customer_primary_contact)
        self.assertFalse(customer.customer_primary_address)
        pending = deferred.observation_key(self.source, 'HeroCustomers', '60001', self.run.name)
        self.assertEqual([(dep.source_type, dep.source_id) for dep in frappe.get_doc(deferred.MAIN, pending).dependencies],
                         [('customer_address','61001')])
        self._resolve(pending)
        self.assertFalse(frappe.get_doc('Customer', customer.name).customer_primary_contact)
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Completed')
        self._resolve(pending)
        customer.reload()
        self.assertTrue(customer.customer_primary_contact)
        self.assertTrue(customer.customer_primary_address)
        self.assertEqual(frappe.get_doc('Contact', customer.customer_primary_contact).email_id,
                         'synthetic-customer@example.invalid')
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')

    def test_address_only_customer_observation_is_realized_without_scalar_change(self):
        from hero_integration.tests.mapping_fixture import fixture_plan
        self.plan = fixture_plan('HeroCustomers')
        record = {'id':60002, 'full_name':'Synthetic address only ' + uuid4().hex[:10], 'company_name':None,
            'is_deleted':False, '_hero_source_instance':self.source,
            'address':{'id':61002, 'street':'Synthetic Road 20', 'city':'Example City', 'zipcode':'12345',
                       'country':{'code':'de'}}}
        with patch.object(migration, '_mapping_plan_for_run', return_value=self.plan):
            result = migration._migrate_customer(record, self.run)
            self.assertEqual(result.code, 'CREATED', result.detail)
            deferred.remember(self.run, 'customer', record, result)
        customer = frappe.get_doc('Customer', result.target_name)
        self.assertFalse(customer.customer_primary_address)
        pending = deferred.observation_key(self.source, 'HeroCustomers', '60002', self.run.name)
        self._resolve(pending)
        customer.reload()
        self.assertTrue(customer.customer_primary_address)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')

    def test_registered_customer_hook_suppresses_automatic_contact_in_owned_import(self):
        from hero_integration.integrations.erpnext.contact import DeferredCustomerRelations
        from hero_integration.services.change_detection import managed_target_update
        customer = frappe.get_doc({'doctype':'Customer', 'customer_name':'Synthetic hook ' + uuid4().hex[:10],
            'customer_type':'Individual', 'customer_group':'Commercial', 'territory':'Germany'}).insert()
        self.assertIsInstance(customer, DeferredCustomerRelations)
        customer.email_id = 'synthetic-hook@example.invalid'
        with managed_target_update('Customer', customer.name, 'fixture'):
            customer.create_primary_contact()
        self.assertFalse(customer.customer_primary_contact)
        customer.create_primary_contact()
        self.assertTrue(customer.customer_primary_contact)

    def test_private_file_attaches_only_after_success_and_keeps_source_ids(self):
        import hashlib
        from hero_integration.services import file_transfer as files, direct_runtime
        project = self._project(20001)
        filename = 'synthetic-deferred-' + uuid4().hex[:10] + '.txt'
        content = b'Synthetic deferred file.'
        document = frappe.get_doc({'doctype':'File', 'file_name':filename, 'content':content, 'is_private':1}).insert()
        path = document.get_full_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        row = {'id':50001, 'uuid':uuid4().hex, 'filename':filename, 'modified':'1', 'size':len(content), 'category':'offer'}
        read = direct_runtime.builtin_read('HeroDocuments')
        fields = files.mapped_values(read, row)
        frappe.get_doc({'doctype':'HERO Integration Link', 'source_system':'HERO', 'source_type':'file_upload',
            'source_id':'50001', 'source_instance':self.source, 'target_doctype':'File', 'target_name':document.name,
            'sync_status':'Synchronized', 'source_payload_hash':files.source_hash(row),
            'target_payload_hash':hashlib.md5(content).hexdigest(), 'mapping_version':read.mapping_version,
            'target_field_hashes':files._target_baseline(document.name, fields)}).insert()
        detail = {'file_upload_matches':[{'project_match_id':20001}], 'file_upload_links':[], 'customer_document':None}
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Running')
        deferred.remember_file(self.run.name, self.source, 'HeroDocuments', read, row, detail)
        pending = deferred.observation_key(self.source, 'HeroDocuments', '50001', self.run.name)
        saved = frappe.get_doc(deferred.MAIN, pending)
        self.assertEqual([(dep.source_type, dep.source_id) for dep in saved.dependencies], [('project','20001')])
        with patch.object(direct_runtime, 'resolve_read', return_value=read):
            self._resolve(pending)
            self.assertFalse(frappe.get_doc('File', document.name).attached_to_name)
            frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Completed')
            self._resolve(pending)
        self.assertEqual(frappe.get_doc('File', document.name).attached_to_name, project.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')

    def test_legacy_native_task_also_defers_an_available_project(self):
        from dataclasses import replace
        self.plan = replace(self.plan, adapter_options={})
        frappe.db.set_value('HERO Integration Run', self.run.name, {'mapping_revision': 1, 'state': 'Running'})
        self.run.reload()
        project = self._project(20001)
        result = migration._migrate_native_task(self.row, self.run, self.plan, self.scope)
        self.assertEqual(result.code, 'CREATED')
        self.assertFalse(frappe.get_doc('Task', result.target_name).project)
        with patch.object(migration, '_mapping_plan_for_run', return_value=self.plan):
            deferred.remember(self.run, 'Task', self.row, result)
        pending = deferred.observation_key(self.source, 'HeroTasks', '10001', self.run.name)
        self._resolve(pending)
        self.assertFalse(frappe.get_doc('Task', result.target_name).project)
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Completed')
        self._resolve(pending)
        self.assertEqual(frappe.get_doc('Task', result.target_name).project, project.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')

    def test_local_project_selected_between_import_and_realization_is_preserved(self):
        self._project(20001)
        task_name, pending = self._task_and_pending()
        local = self._project(20002)
        task = frappe.get_doc('Task', task_name)
        task.project = local.name
        task.save()
        self._resolve(pending)
        self.assertEqual(frappe.get_doc('Task', task_name).project, local.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Conflict')

    def test_new_run_retains_prior_dependency_ids_and_supersedes_its_open_work(self):
        task_name, previous = self._task_and_pending()
        newer = frappe.get_doc({'doctype': 'HERO Integration Run', 'action': 'Projection Migration',
            'mode': 'Execute', 'state': 'Running', 'source_instance': self.source,
            'source_binding_revision': 1}).insert()
        with patch.object(migration,'_mapping_plan_for_run',return_value=self.plan):
            deferred.remember(newer,'Task',self.row,migration.Result('UPDATED','','Task',task_name))
        current = deferred.observation_key(self.source, 'HeroTasks', '10001', newer.name)
        self.assertNotEqual(previous, current)
        old = frappe.get_doc(deferred.MAIN, previous)
        self.assertEqual(old.status, 'Resolved')
        self.assertEqual(old.dependencies[0].source_id, '20001')
        self.assertEqual(old.origin_run, self.run.name)
        self.assertEqual(frappe.get_doc(deferred.MAIN, current).origin_run, newer.name)
        project = self._project(20001)
        self._resolve(previous)
        self._resolve(current)
        self.assertFalse(frappe.get_doc('Task', task_name).project)
        frappe.db.set_value('HERO Integration Run', newer.name, 'state', 'Completed')
        self._resolve(current)
        self.assertEqual(frappe.get_doc('Task', task_name).project, project.name)

    def test_invalid_assignee_does_not_prevent_task_import(self):
        assignment = MappingSpec('HeroTasks', 'target_user_id', 'ToDo', 'allocated_to', 'int|null',
                                 'Native Binding', 'HERO User Link', definition_id='synthetic-assignee')
        plan = plan_from_definitions('HeroTasks', [*self.plan.definitions, assignment],
                                     target_operation='HeroTasks', adapter_options={'version': 2})
        result = migration._migrate_mapped_task({**self.row, 'target_user_id': -1}, self.run, plan, self.scope)
        self.assertEqual(result.code, 'CREATED')
        self.assertTrue(frappe.db.exists('Task', result.target_name))
        self.assertTrue(frappe.db.exists('HERO Integration Record', {'run': self.run.name,
            'target_name': result.target_name, 'technical_action': 'task_assignment_review', 'result': 'PENDING_REVIEW'}))

    def test_zero_assignee_is_unassigned_and_preserves_local_todos(self):
        assignment = MappingSpec('HeroTasks', 'target_user_id', 'ToDo', 'allocated_to', 'int|null',
                                 'Native Binding', 'HERO User Link', definition_id='synthetic-assignee')
        plan = plan_from_definitions('HeroTasks', [*self.plan.definitions, assignment],
                                     target_operation='HeroTasks', adapter_options={'version': 2})
        record = {**self.row, 'target_user_id': 0}
        result = migration._migrate_mapped_task(record, self.run, plan, self.scope)
        self.assertEqual(result.code, 'CREATED')
        todo = frappe.get_doc({'doctype': 'ToDo', 'allocated_to': os.environ.get('HERO_SYNTHETIC_USER', 'Administrator'), 'reference_type': 'Task',
            'reference_name': result.target_name, 'description': 'Synthetic local assignment'}).insert()
        repeated = migration._migrate_mapped_task(record, self.run, plan, self.scope)
        self.assertFalse(repeated.pending_relation)
        self.assertEqual(frappe.db.get_value('ToDo', todo.name, 'status'), 'Open')
        self.assertFalse(frappe.db.exists('HERO Integration Record', {'run': self.run.name,
            'target_name': result.target_name, 'technical_action': 'task_assignment_review'}))

    def test_resolving_project_does_not_hide_unresolved_task_assignee(self):
        assignment = MappingSpec('HeroTasks', 'target_user_id', 'ToDo', 'allocated_to', 'int|null',
                                 'Native Binding', 'HERO User Link', definition_id='synthetic-assignee')
        self.plan = plan_from_definitions('HeroTasks', [*self.plan.definitions, assignment],
                                          target_operation='HeroTasks', adapter_options={'version': 2})
        self.row['target_user_id'] = -1
        task_name, pending = self._task_and_pending()
        project = self._project(20001)
        self._resolve(pending)
        self.assertEqual(frappe.get_doc('Task', task_name).project, project.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Conflict')
        self.assertTrue(frappe.db.get_value(deferred.MAIN, pending, 'record_json'))

    def _event_and_pending(self):
        rules=[
            MappingSpec('HeroCalendarEvents','id','HERO Integration Link','source_id','int','Source Identity','Positive Int To Text',definition_id='eid'),
            MappingSpec('HeroCalendarEvents','title','Event','subject','string|null','Record Name','Trim',definition_id='etitle'),
            MappingSpec('HeroCalendarEvents','start','Event','starts_on','string|null','Native Binding','Native Adapter',definition_id='estart'),
            MappingSpec('HeroCalendarEvents','all_day','Event','all_day','boolean','Native Binding','Native Adapter',definition_id='eall'),
            MappingSpec('HeroCalendarEvents','project_match_id','Event','custom_hero_project','int|null',
                'Project Relation','HERO Project Link','HERO Project Link',definition_id='eproject')]
        self.plan=plan_from_definitions('HeroCalendarEvents',rules,target_operation='HeroCalendarEvents',adapter_options={'version':2})
        self.row={'id':50001,'title':'Synthetic event '+uuid4().hex[:10],'start':'2026-11-05T09:00:00Z',
            'all_day':False,'deleted':False,'is_recurring':False,'project_match_id':20001,'_hero_source_instance':self.source}
        from hero_integration.services.mapping_runtime import MappedValues
        values=MappedValues({'subject':self.row['title'],'starts_on':'2026-11-05 09:00:00','all_day':0},missing={'custom_hero_project'})
        result=migration._upsert('CalendarEvent','50001',self.row,values,'Event','subject',self.plan.mapping_version,
            self.run,source_instance=self.source,creation_values={'event_type':'Private','send_reminder':0,
                'sync_with_google_calendar':0,'add_video_conferencing':0})
        self.assertEqual(result.code,'CREATED')
        with patch.object(migration,'_mapping_plan_for_run',return_value=self.plan):
            deferred.remember(self.run,'CalendarEvent',self.row,result)
        pending=deferred.observation_key(self.source,'HeroCalendarEvents','50001',self.run.name)
        return result.target_name,pending

    def test_event_waits_then_only_binds_project_and_keeps_local_scalar_baseline(self):
        event,pending=self._event_and_pending()
        self._resolve(pending)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Waiting')
        link=migration._link('CalendarEvent','50001',self.source)
        baseline=link.target_field_hashes
        frappe.db.set_value('Event',event,'subject','Local event title')
        project=self._project(20001)
        with patch.object(migration,'_migrate_projection',side_effect=AssertionError('No full Event intake during binding')):
            self._resolve(pending)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Resolved')
        target=frappe.get_doc('Event',event)
        self.assertEqual(target.custom_hero_project,project.name)
        self.assertEqual(target.subject,'Local event title')
        import json
        current=migration._link('CalendarEvent','50001',self.source)
        self.assertEqual(json.loads(current.target_field_hashes)['subject'],json.loads(baseline)['subject'])

    def test_event_binding_keeps_changed_privacy_as_review_conflict(self):
        event,pending=self._event_and_pending()
        self._project(20001)
        frappe.db.set_value('Event',event,'send_reminder',1)
        self._resolve(pending)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Conflict')
        self.assertFalse(frappe.get_doc('Event',event).custom_hero_project)

    def test_full_binding_pass_resolves_event_after_project_stage_without_another_import(self):
        event,pending=self._event_and_pending()
        original=deferred.reconcile_source_page
        stages=[]
        def page(source,*args,operations=None,**kwargs):
            stages.extend(operations or [])
            if operations==('HeroProjectMatches',):
                self._project(20001)
            return original(source,*args,operations=operations,**kwargs)
        state={}
        with self._resolution_context(),patch.object(frappe.db,'commit'), \
                patch.object(deferred,'reconcile_source_page',side_effect=page), \
                patch.object(migration,'_migrate_projection',side_effect=AssertionError('No scalar Event replay')):
            for _ in range(len(deferred.FINAL_RELATION_ORDER)+1):
                done=deferred.reconcile_full_page(self.source,[self.run.name],state)
        self.assertTrue(done)
        self.assertLess(stages.index('HeroProjectMatches'),stages.index('HeroCalendarEvents'))
        self.assertEqual(state['reconciliation_conflicts'],0)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Resolved')
        self.assertTrue(frappe.get_doc('Event',event).custom_hero_project)

    def test_missing_event_project_is_waiting_until_full_final_assessment(self):
        event,pending=self._event_and_pending()
        state={}
        with self._resolution_context(),patch.object(frappe.db,'commit'):
            for _ in deferred.FINAL_RELATION_ORDER:
                self.assertFalse(deferred.reconcile_full_page(self.source,[self.run.name],state))
                self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Waiting')
            self.assertTrue(deferred.reconcile_full_page(self.source,[self.run.name],state))
        self.assertEqual(state['reconciliation_conflicts'],1)
        self.assertEqual(frappe.db.get_value(deferred.MAIN,pending,'status'),'Conflict')
        self.assertTrue(frappe.db.get_value(deferred.MAIN,pending,'record_json'))
        self.assertFalse(frappe.get_doc('Event',event).custom_hero_project)

    def _native_logbook_fixture(self):
        from copy import deepcopy
        from hero_integration.tests.test_project_logbook_mapped_complete import TestCompleteMappedLogbook
        fixture=TestCompleteMappedLogbook();fixture.setUp()
        project=self._project(20001)
        record=deepcopy(fixture.record)
        record.update(id=20001,_hero_source_instance=self.source)
        record['histories'][0]['custom_text']='Project created through migration & ERPNext'
        record['histories'][0]['user']['email']=''
        return project,fixture.plan,record

    def test_native_logbook_baseline_ignores_own_creation_defaults_and_unlinked_system_comments(self):
        from hero_integration.services import project_logbook as lb
        project,plan,record=self._native_logbook_fixture()
        generated=frappe.get_doc({'doctype':'Comment','comment_type':'Created','reference_doctype':'Project',
            'reference_name':project.name,'content':'Created by migration'}).insert(ignore_permissions=True)
        self.assertEqual(lb.sync_mapped_entries(plan,record,project.name),1)
        link=frappe.get_all('HERO Integration Link',filters={'source_instance':self.source,
            'source_type':'project_history'},fields=['name','target_name','source_payload_hash','target_payload_hash'])[0]
        comment=frappe.get_doc('Comment',link.target_name)
        self.assertEqual(comment.comment_email,'Administrator')
        self.assertIn('&amp;',comment.content)
        self.assertNotEqual(link.source_payload_hash,link.target_payload_hash)
        conflicts=[]
        self.assertEqual(lb.sync_mapped_entries(plan,record,project.name,conflicts=conflicts),0)
        self.assertEqual(conflicts,[])
        self.assertTrue(frappe.db.exists('Comment',generated.name))
        self.assertEqual(frappe.db.count('HERO Integration Link',{'source_instance':self.source,'source_type':'project_history'}),1)
        record['histories'][0]['custom_text']='Changed in HERO & ERPNext'
        lb.sync_mapped_entries(plan,record,project.name,conflicts=conflicts)
        self.assertEqual(conflicts,[])
        self.assertIn('Changed in HERO &amp; ERPNext',frappe.get_doc('Comment',comment.name).content)
        lb.sync_mapped_entries(plan,record,project.name,conflicts=conflicts)
        self.assertEqual(conflicts,[])

    def test_old_pre_save_logbook_baseline_can_be_repaired_only_for_proven_native_defaults(self):
        from hero_integration.services import project_logbook as lb
        project,plan,record=self._native_logbook_fixture()
        lb.sync_mapped_entries(plan,record,project.name)
        link=frappe.get_all('HERO Integration Link',filters={'source_instance':self.source,
            'source_type':'project_history'},fields=['name','target_name','source_payload_hash'])[0]
        comment=frappe.get_doc('Comment',link.target_name)
        before=(comment.content,comment.creation,comment.modified,comment.comment_email)
        frappe.db.set_value('HERO Integration Link',link.name,{'target_payload_hash':link.source_payload_hash,'sync_status':'Conflict'})
        original_text=record['histories'][0]['custom_text']
        record['histories'][0]['custom_text']='Concurrent source change'
        blocked=[]
        lb.sync_mapped_entries(plan,record,project.name,conflicts=blocked)
        self.assertEqual(blocked,[link.name])
        self.assertEqual(frappe.get_doc('Comment',comment.name).content,before[0])
        record['histories'][0]['custom_text']=original_text
        conflicts=[]
        original=frappe.db.set_value
        with patch.object(frappe.db,'set_value',wraps=original) as writes:
            lb.sync_mapped_entries(plan,record,project.name,conflicts=conflicts)
        self.assertEqual(conflicts,[])
        self.assertTrue(all(call.args[0]!='Comment' for call in writes.call_args_list))
        comment.reload()
        self.assertEqual((comment.content,comment.creation,comment.modified,comment.comment_email),before)
        self.assertEqual(frappe.db.get_value('HERO Integration Link',link.name,'sync_status'),'Synchronized')
        frappe.db.set_value('Comment',comment.name,'content','Real local edit')
        lb.sync_mapped_entries(plan,record,project.name,conflicts=conflicts)
        self.assertEqual(conflicts,[link.name])
        self.assertEqual(frappe.get_doc('Comment',comment.name).content,'Real local edit')

    def test_event_mapping_change_reuses_only_unchanged_field_baselines(self):
        from hero_integration.services.change_detection import encode, field_hashes

        values = {'subject': 'Synthetic event ' + uuid4().hex[:10]}
        event = frappe.get_doc({'doctype': 'Event', **values, 'event_type': 'Private',
                                'starts_on': '2026-11-05 09:00:00'}).insert()
        link = frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO',
            'source_type': 'CalendarEvent', 'source_id': '50001', 'source_instance': self.source,
            'target_doctype': 'Event', 'target_name': event.name, 'sync_status': 'Synchronized',
            'source_payload_hash': 'fixture', 'target_payload_hash': 'fixture', 'mapping_version': 'old',
            'source_field_hashes': encode(field_hashes(values)), 'target_field_hashes': encode(field_hashes(values))}).insert()
        result = migration._upsert('CalendarEvent', '50001', {'id': 50001}, values, 'Event', 'subject',
                                   'new', self.run, source_instance=self.source, preserve_omitted=True)
        self.assertEqual(result.code, 'SKIPPED')
        link.reload()
        self.assertEqual(link.mapping_version, 'new')
        event.subject = 'Local event change'; event.save()
        result = migration._upsert('CalendarEvent', '50001', {'id': 50001}, values, 'Event', 'subject',
                                   'newer', self.run, source_instance=self.source, preserve_omitted=True)
        self.assertEqual(result.code, 'CONFLICT')
        self.assertEqual(frappe.get_doc('Event', event.name).subject, 'Local event change')
        self.assertEqual(frappe.get_doc('HERO Integration Link', link.name).mapping_version, 'new')

    def test_new_secondary_address_is_created_only_by_post_success_service(self):
        from hero_integration.tests.test_mapped_collection_relations import ADDRESSES
        customer = frappe.get_doc({'doctype': 'Customer', 'customer_name': 'Synthetic deferred address ' + uuid4().hex[:10],
            'customer_type': 'Individual', 'customer_group': 'Commercial', 'territory': 'Germany'}).insert()
        record = {'id': 888, 'address': {'id': 1}, '_hero_source_instance': self.source,
            'customer_addresses': [{'is_deleted': False, 'address': {'id': 2, 'street': 'Synthetic 2',
                'city': 'Example City', 'zipcode': '12345', 'country': {'code': 'DE'}}}]}
        self.plan = SimpleNamespace(definitions=[], adapter_options={'native_relations': [ADDRESSES]},
            target_doctype='Customer', mapping_version='fixture', source_identity=lambda _: '888')
        result = migration.Result('UPDATED', 'Imported', 'Customer', customer.name)
        frappe.get_doc({'doctype':'HERO Integration Link','source_system':'HERO','source_type':'customer',
            'source_id':'888','source_instance':self.source,'target_doctype':'Customer','target_name':customer.name,
            'sync_status':'Synchronized','source_payload_hash':migration._digest(record),
            'target_payload_hash':'fixture','mapping_version':'fixture'}).insert()
        frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Running')
        self.assertIs(migration._sync_mapped_relations(self.plan, record, result, self.run), result)
        with patch.object(migration, '_mapping_plan_for_run', return_value=self.plan):
            deferred.remember(self.run, 'customer', record, result)
        pending = deferred.observation_key(self.source, 'HeroCustomers', '888', self.run.name)
        filters = {'source_instance': self.source, 'source_type': 'customer_address', 'source_id': '2'}
        self.assertFalse(frappe.db.exists('HERO Integration Link', filters))
        with patch.object(migration, '_migrate_customer', side_effect=lambda row, run:
                migration._sync_mapped_relations(self.plan, row, result, run)):
            self._resolve(pending)
            self.assertFalse(frappe.db.exists('HERO Integration Link', filters))
            frappe.db.set_value('HERO Integration Run', self.run.name, 'state', 'Completed')
            self._resolve(pending)
        self.assertTrue(frappe.db.exists('HERO Integration Link', filters))
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')

    def test_existing_unbound_customer_address_waits_until_exact_binding_exists(self):
        from hero_integration.services import mapped_collection_relations as collections
        from hero_integration.tests.test_mapped_collection_relations import ADDRESSES

        customer = frappe.get_doc({'doctype': 'Customer', 'customer_name': 'Synthetic customer ' + uuid4().hex[:10],
            'customer_type': 'Individual', 'customer_group': 'Commercial', 'territory': 'Germany'}).insert()
        address = frappe.get_doc({'doctype': 'Address', 'address_title': 'Synthetic secondary address',
            'address_type': 'Other', 'address_line1': 'Example 2', 'city': 'Example City',
            'pincode': '12345', 'country': 'Germany',
            'links': [{'link_doctype': 'Customer', 'link_name': customer.name}]}).insert()
        record = {'id': 777, 'address': {'id': 1}, '_hero_source_instance': self.source,
                  'customer_addresses': [{'is_deleted': False, 'address': {'id': 2, 'street': 'Example 2',
                      'city': 'Example City', 'zipcode': '12345', 'country': {'code': 'DE'}}}]}
        plan = SimpleNamespace(definitions=[], adapter_options={'native_relations': [ADDRESSES]},
                               target_doctype='Customer', mapping_version='fixture', source_identity=lambda _: '777')
        result = migration.Result('SKIPPED', 'Customer synchronized.', 'Customer', customer.name)
        actual = migration._sync_mapped_relations(plan, record, result, self.run)
        self.assertEqual(actual.code, 'SKIPPED')
        self.assertFalse(frappe.db.exists('HERO Integration Record', {'run': self.run.name,
            'technical_action': 'customer_address_review', 'target_name': customer.name, 'result': 'PENDING_REVIEW'}))
        frappe.get_doc({'doctype':'HERO Integration Link','source_system':'HERO','source_type':'customer',
            'source_id':'777','source_instance':self.source,'target_doctype':'Customer','target_name':customer.name,
            'sync_status':'Synchronized','source_payload_hash':migration._digest(record),
            'target_payload_hash':'fixture','mapping_version':'fixture'}).insert()
        with patch.object(migration,'_mapping_plan_for_run',return_value=plan):
            deferred.remember(self.run,'customer',record,actual)
        pending = deferred.observation_key(self.source, 'HeroCustomers', '777', self.run.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Waiting')

        item = collections.prepare(ADDRESSES, record, 'Customer', customer.name)[0]
        values = {'address_line1': 'Example 2', 'city': 'Example City', 'pincode': '12345',
                  'country': 'Germany', 'address_type': 'Other'}
        observed = collections._observed({'collection_kind': 'customer_addresses', 'document': address, 'values': values})
        with patch.object(deferred, '_enqueue_dependency') as enqueue:
            frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': 'customer_address',
                'source_id': '2', 'source_instance': self.source, 'target_doctype': 'Address', 'target_name': address.name,
                'sync_status': 'Synchronized', 'source_payload_hash': collections._hash(item['values']),
                'target_payload_hash': collections._hash(observed), 'mapping_version': 'fixture'}).insert()
            enqueue.assert_called_once_with(self.source, 'customer_address', '2', enqueue_after_commit=True)
        with patch('hero_integration.client.settings.get_configuration', return_value=SimpleNamespace(source_instance=self.source, source_binding_confirmed=True, source_binding_revision=1)), \
             patch.object(migration, 'validate_run_mappings'), \
             patch.object(migration, '_source_for_run', return_value=SimpleNamespace(source_instance=self.source, source_binding_confirmed=True, source_binding_revision=1)), \
             patch.object(migration, '_mapping_plan_for_run', return_value=plan), \
             patch.object(migration, '_migrate_customer', side_effect=lambda row, run: migration._sync_mapped_relations(plan, row, result, run)):
            deferred.resolve_one(pending, dependency=('customer_address', '2'))
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')
        self.assertEqual(frappe.db.count('Address', {'address_title': 'Synthetic secondary address'}), 1)

    def test_equal_secondary_source_address_reuses_bound_primary_address(self):
        realizer = patch.object(deferred, 'realizing', return_value=True)
        realizer.start(); self.addCleanup(realizer.stop)
        from hero_integration.tests.test_mapped_collection_relations import ADDRESSES
        customer = frappe.get_doc({'doctype': 'Customer', 'customer_name': 'Synthetic alias ' + uuid4().hex[:10],
            'customer_type': 'Individual', 'customer_group': 'Commercial', 'territory': 'Germany'}).insert()
        address = frappe.get_doc({'doctype': 'Address', 'address_title': 'Synthetic alias ' + uuid4().hex[:10],
            'address_type': 'Billing', 'address_line1': 'Example 2', 'city': 'Example City',
            'pincode': '12345', 'country': 'Germany', 'is_primary_address': 1,
            'links': [{'link_doctype': 'Customer', 'link_name': customer.name}]}).insert()
        frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': 'customer_address',
            'source_id': '1', 'source_instance': self.source, 'target_doctype': 'Address', 'target_name': address.name,
            'sync_status': 'Synchronized', 'source_payload_hash': 'source', 'target_payload_hash': 'target',
            'mapping_version': 'fixture'}).insert()
        record = {'id': 777, 'address': {'id': 1}, '_hero_source_instance': self.source,
                  'customer_addresses': [{'is_deleted': False, 'address': {'id': 2, 'street': 'Example 2',
                      'city': 'Example City', 'zipcode': '12345', 'country': None}}]}
        plan = SimpleNamespace(definitions=[], adapter_options={'native_relations': [ADDRESSES]},
                               target_doctype='Customer', mapping_version='fixture', source_identity=lambda _: '777')
        result = migration.Result('SKIPPED', 'Customer synchronized.', 'Customer', customer.name)
        actual = migration._sync_mapped_relations(plan, record, result, self.run)
        self.assertFalse(actual.pending_relation)
        self.assertEqual(frappe.db.get_value('HERO Integration Link', {'source_type': 'customer_address',
            'source_instance': self.source, 'source_id': '2'}, 'target_name'), address.name)
        self.assertEqual(frappe.db.get_value('Address', address.name, 'address_type'), 'Billing')
        self.assertEqual(frappe.db.get_value('Address', address.name, 'is_primary_address'), 1)
        record['customer_addresses'][0]['address']['id'] = 4
        self.assertNotEqual(migration._sync_mapped_relations(plan, record, result, self.run).code, 'CONFLICT')
        stale = frappe.db.get_value('HERO Integration Link', {'source_type': 'customer_address',
            'source_instance': self.source, 'source_id': '2'}, 'name')
        frappe.db.set_value('HERO Integration Link', stale, 'target_payload_hash', 'stale')
        record['customer_addresses'][0]['address']['id'] = 2
        self.assertNotEqual(migration._sync_mapped_relations(plan, record, result, self.run).code, 'CONFLICT')
        self.assertEqual(frappe.db.get_value('HERO Integration Link', stale, 'sync_status'), 'Synchronized')
        record['customer_addresses'][0]['address'].update(id=3, zipcode='54321')
        actual = migration._sync_mapped_relations(plan, record, result, self.run)
        self.assertFalse(actual.pending_relation)
        other = frappe.db.get_value('HERO Integration Link', {'source_type': 'customer_address',
            'source_instance': self.source, 'source_id': '3'}, 'target_name')
        self.assertTrue(other)
        self.assertNotEqual(other, address.name)
        self.assertEqual(frappe.db.get_value('Address', other, 'pincode'), '54321')

    def test_local_project_edit_is_preserved_as_conflict(self):
        task_name, pending = self._task_and_pending()
        local = self._project(20002)
        task = frappe.get_doc('Task', task_name); task.project = local.name; task.save()
        self._project(20001)
        self._resolve(pending)
        self.assertEqual(frappe.get_doc('Task', task_name).project, local.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Conflict')

    def test_matching_unbound_contact_keeps_its_local_last_name(self):
        from hero_integration.services import mapped_relations
        from hero_integration.tests.test_mapped_collection_relations import CONTACTS
        customer = frappe.get_doc({'doctype': 'Customer', 'customer_name': 'Synthetic person ' + uuid4().hex[:10],
            'customer_type': 'Individual', 'customer_group': 'Commercial', 'territory': 'Germany'}).insert()
        contact = frappe.get_doc({'doctype': 'Contact', 'first_name': 'Example', 'last_name': 'Local Name',
            'designation': 'Office', 'email_ids': [{'email_id': 'synthetic@example.invalid', 'is_primary': 1}],
            'phone_nos': [{'phone': '+491234', 'is_primary_mobile_no': 1},
                          {'phone': '+495678', 'is_primary_phone': 1}],
            'links': [{'link_doctype': 'Customer', 'link_name': customer.name}]}).insert()
        record = {'id': 100, '_hero_source_instance': self.source, 'contacts': [
            {'id': 101, 'parent_customer_id': 100, 'is_contact_person': True, 'is_deleted': False,
             'first_name': 'Example', 'last_name': 'Source Name', 'email': 'synthetic@example.invalid',
             'phone_mobile': '+491234', 'phone_home': '+495678', 'title': None, 'position': 'Office'}]}
        plan = SimpleNamespace(adapter_options={'native_relations': [CONTACTS]}, mapping_version='fixture')
        self.assertEqual(mapped_relations.sync(plan, record, 'Customer', customer.name)['created'], 1)
        self.assertEqual(mapped_relations.sync(plan, record, 'Customer', customer.name)['unchanged'], 1)
        self.assertEqual(frappe.db.get_value('Contact', contact.name, 'last_name'), 'Local Name')
        record['contacts'][0]['last_name'] = 'Changed Source Name'
        self.assertEqual(mapped_relations.sync(plan, record, 'Customer', customer.name)['conflicts'], 1)
        self.assertEqual(frappe.db.get_value('Contact', contact.name, 'last_name'), 'Local Name')

    def test_large_test_selection_uses_longtext_and_rejects_changed_content(self):
        from hero_integration.services import file_migration
        selection = {'version': 1, 'project_ids': ['20001'], 'customer_ids': [], 'user_ids': [],
                     'task_relation_hashes': {str(i): 'a' * 64 for i in range(5000)}}
        digest = file_migration._save_related_test(self.run.name, selection)
        file_migration._save_full(self.run.name, {'related_test_enabled': True, 'related_test_digest': digest})
        raw = frappe.db.get_value('HERO Integration Run', self.run.name, 'testing_cohort_json')
        self.assertGreater(len(raw.encode()), 65535)
        self.assertEqual(len(file_migration._load_related_test(self.run.name, expected_digest=digest)['task_relation_hashes']), 5000)
        frappe.db.set_value('HERO Integration Run', self.run.name, 'testing_cohort_json', raw + ' ')
        with self.assertRaisesRegex(file_migration.SourceBindingError, 'selection changed'):
            file_migration._load_related_test(self.run.name, expected_digest=digest)
