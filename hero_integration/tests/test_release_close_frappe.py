"""Bounded release acceptance on an explicitly isolated, synthetic site."""
import os
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe
from hero_integration.services import migration, mapped_relations, file_transfer, file_migration
from hero_integration.services.mapping_runtime import MappedValues


class TestReleaseClose(TestCase):
    def setUp(self):
        if not os.environ.get('HERO_RELEASE_TEST_SITE') or os.environ['HERO_RELEASE_TEST_SITE'] != getattr(frappe.local, 'site', None):
            self.skipTest('Requires explicitly selected isolated release site')
        frappe.set_user('Administrator')
        frappe.local.request = None
        self.source = 'release-fixture-' + uuid4().hex[:10]
        self.run = NS(mode='Execute')

    def tearDown(self):
        frappe.db.rollback()
        frappe.set_user('Administrator')

    def project(self, identity='1', title='Synthetic release project'):
        return migration._upsert('project', identity, {'id': identity, 'title': title},
            MappedValues(project_name=title, company='Synthetic Release Company'), 'Project', 'project_name', 'release-fixture', self.run,
            source_instance=self.source)

    def test_project_without_address_repeat_update_and_conflict(self):
        result = self.project()
        self.assertEqual(result.code, 'CREATED')
        project = frappe.get_doc('Project', result.target_name)
        self.assertFalse(project.custom_project_address)
        project.notes = 'Local unmapped note'
        project.save()
        self.assertEqual(self.project().code, 'SKIPPED')
        self.assertEqual(self.project(title='Synthetic source update').code, 'UPDATED')
        project.reload()
        self.assertEqual(project.notes, 'Local unmapped note')
        project.project_name = 'Local protected title'
        project.save()
        self.assertEqual(self.project(title='Different remote title').code, 'CONFLICT')
        self.assertEqual(frappe.db.get_value('Project', project.name, 'project_name'), 'Local protected title')
        self.assertEqual(frappe.db.count('HERO Integration Link', {'source_instance': self.source, 'source_id': '1'}), 1)

    def test_record_failure_then_independent_success(self):
        def invalid():
            frappe.get_doc({'doctype': 'Project', 'project_name': 'Will roll back'}).insert()
            frappe.throw('Synthetic invalid row')
        self.assertEqual(migration._migrate_isolated(invalid).code, 'BLOCKED_RECORD')
        self.assertFalse(frappe.db.exists('Project', {'project_name': 'Will roll back'}))
        self.assertEqual(migration._migrate_isolated(lambda: self.project()).code, 'CREATED')

    def test_native_project_relation_is_idempotent(self):
        from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
        create_custom_fields({'Project': [{'fieldname': 'custom_hero_project_address', 'label': 'HERO Project Address', 'fieldtype': 'Link', 'options': 'Address'}]})
        project = self.project().target_name
        relation = {'kind': 'project_address', 'identity_path': 'address.id',
            'fields': {'address_line1': 'address.street', 'city': 'address.city'},
            'country_code_path': 'address.country', 'parent_field': 'custom_hero_project_address', 'address_type': 'Other'}
        plan = NS(adapter_options={'native_relations': [relation]}, mapping_version='release-fixture')
        row = {'_hero_source_instance': self.source, 'address': {'id': 2, 'street': 'Synthetic Street 1', 'city': 'Synthetic City', 'country': 'de'}}
        self.assertEqual(mapped_relations.sync(plan, row, 'Project', project)['created'], 1)
        self.assertEqual(mapped_relations.sync(plan, row, 'Project', project)['unchanged'], 1)
        address = frappe.db.get_value('Project', project, 'custom_hero_project_address')
        self.assertTrue(frappe.db.exists('Address', address))

    def test_imported_image_fallback_and_native_update(self):
        import base64, hashlib
        project = self.project().target_name
        data = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
        # Transport is synthetic; the mapping output, File hook, save, identity
        # and baseline persistence below use the real site implementation.
        row = {'id': 55, 'category': 'images', 'filename': 'release.png', 'uuid': 'release', 'modified': '1'}
        read = NS(mapping_version='release-fixture', plan=NS(source_identity=lambda record: str(record['id'])))
        path = frappe.get_site_path('private', 'files', 'release-fixture.png')
        from pathlib import Path
        Path(path).write_bytes(data)
        fields = {'file_name': 'release.png', 'file_type': 'PNG'}
        try:
            with patch.object(file_transfer, 'mapped_values', return_value=fields):
                stored = file_transfer._record(row, len(data), hashlib.md5(data).hexdigest(), '/private/files/release-fixture.png',
                    ('Project', project), 'Home/Attachments', None, self.source, read)
            doc = frappe.get_doc('File', stored.file_name)
            self.assertTrue(doc.custom_project_image_category.startswith('HERO-Import'))
            link = frappe.get_doc('HERO Integration Link', {'source_instance': self.source, 'source_id': '55'})
            file_transfer._save_file(doc.name, {'file_name': 'release-updated.png'}, fields, link.name)
            self.assertEqual(frappe.db.get_value('File', doc.name, 'file_name'), 'release-updated.png')
        finally:
            Path(path).unlink(missing_ok=True)

    def test_post_start_persistence_queue_get_and_permissions(self):
        from hero_integration import api
        from frappe.handler import execute_cmd
        from werkzeug.test import EnvironBuilder
        from werkzeug.wrappers import Request
        config = NS(source_binding_confirmed=True, source_instance=self.source, source_binding_revision=1)
        cmd = 'hero_integration.api.sync_area_start'
        for old in frappe.get_all('HERO Integration Run', filters={'source_instance': ['like', 'release-fixture-%'], 'state': 'Queued'}, pluck='name'):
            frappe.db.set_value('HERO Integration Run', old, 'state', 'Cancelled')
        frappe.db.commit()
        with patch.object(api, '_configured_source_instance', return_value=self.source), \
             patch.object(api, '_assert_manual_prerequisite'), \
             patch.object(file_migration, '_configuration', return_value=config), \
             patch.object(file_migration, 'require_mappings'):
            frappe.local.request = Request(EnvironBuilder(method='GET').get_environ())
            with self.assertRaises(frappe.PermissionError):
                execute_cmd(cmd)
            frappe.local.request = Request(EnvironBuilder(method='POST').get_environ())
            frappe.form_dict.clear()
            frappe.form_dict.update(area='documents')
            frappe.set_user('Guest')
            with self.assertRaises(frappe.PermissionError):
                execute_cmd(cmd)
            frappe.set_user('Administrator')
            frappe.form_dict.update(area='documents')
            result = execute_cmd(cmd)
            run = result['run']
            self.assertTrue(frappe.db.exists('HERO Integration Run', run))
            from frappe.utils.background_jobs import get_queue
            before = len(get_queue('long').get_job_ids())
            from frappe.app import sync_database
            sync_database()
            self.assertEqual(frappe.db.get_value('HERO Integration Run', run, 'state'), 'Queued')
            self.assertEqual(len(get_queue('long').get_job_ids()), before + 1)
        frappe.local.request = None

    def test_existing_required_field_becomes_optional(self):
        from frappe.custom.doctype.property_setter.property_setter import make_property_setter
        from hero_integration.services.project_address import ADDRESS, ensure_fields
        make_property_setter('Project', ADDRESS, 'reqd', '1', 'Check', validate_fields_for_doctype=False)
        self.assertTrue(frappe.get_meta('Project').get_field(ADDRESS).reqd)
        ensure_fields()
        ensure_fields()
        self.assertFalse(frappe.get_meta('Project').get_field(ADDRESS).reqd)
        self.assertEqual(self.project().code, 'CREATED')


def run_acceptance(test=None):
    if frappe.local.site != 'hero-close.localhost' or os.environ.get('HERO_RELEASE_TEST_SITE') != frappe.local.site:
        raise RuntimeError('Requires the dedicated disposable release site')
    import unittest
    if not frappe.db.exists('Warehouse Type', 'Transit'):
        frappe.get_doc({'doctype': 'Warehouse Type', 'name': 'Transit'}).insert()
    if not frappe.db.exists('Company', 'Synthetic Release Company'):
        frappe.get_doc({'doctype': 'Company', 'company_name': 'Synthetic Release Company', 'abbr': 'SRC',
            'default_currency': 'EUR', 'country': 'Germany'}).insert()
        frappe.db.commit()
    if not frappe.db.exists('Address Template', {'is_default': 1}):
        frappe.get_doc({'doctype': 'Address Template', 'country': 'Germany', 'is_default': 1,
            'template': '{{ address_line1 }} {{ city }} {{ country }}'}).insert()
        frappe.db.commit()
    suite = (unittest.TestSuite([TestReleaseClose(test)]) if test else
             unittest.defaultTestLoader.loadTestsFromTestCase(TestReleaseClose))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != (1 if test else 6) or result.skipped:
        raise RuntimeError('Release acceptance failed or was skipped')
    return {'tests': result.testsRun, 'failures': 0, 'site': frappe.local.site}
