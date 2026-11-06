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
