"""Prepare mapping-owned schema on the explicitly selected disposable CI site."""
import json
import os
from pathlib import Path

import frappe


def prepare():
    if (not frappe.conf.get('allow_tests') or not os.environ.get('HERO_CI_TEST_SITE')
            or os.environ['HERO_CI_TEST_SITE'] != frappe.local.site):
        raise RuntimeError('Schema fixtures require an explicitly selected test-enabled CI site.')
    from hero_integration.services import schema_provisioning

    manifest = Path(__file__).resolve().parents[1] / 'services' / 'task_external_deadline_schema.json'
    return schema_provisioning.provision_schema(json.loads(manifest.read_text(encoding='utf-8')))


def prepare_deferred_cases():
    """Provision only the declared relationship fixture on the exact CI site."""
    if (not frappe.conf.get('allow_tests') or not os.environ.get('HERO_CI_TEST_SITE')
            or os.environ['HERO_CI_TEST_SITE'] != frappe.local.site):
        raise RuntimeError('Deferred fixtures require an explicitly selected CI site.')
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    field = frappe.get_meta('Event').get_field('custom_hero_project')
    if field:
        if field.fieldtype != 'Link' or field.options != 'Project':
            raise RuntimeError('Incompatible CI Event relationship field')
    else:
        create_custom_fields({'Event': [{'fieldname': 'custom_hero_project',
            'label': 'HERO Project', 'fieldtype': 'Link', 'options': 'Project'}]}, update=False)
    if not frappe.db.exists('Company', 'HERO CI Company'):
        raise RuntimeError('The standard CI Company fixture is missing')
