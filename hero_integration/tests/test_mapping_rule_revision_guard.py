"""Rule revision counters are advanced only by the Document controller."""
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping
from hero_integration.tests import test_portable_draft_exchange as fixture


class TestMappingRuleRevisionGuard(unittest.TestCase):
    def test_direct_rule_revision_edits_are_rejected(self):
        target = fixture.PublicationFake('target.test', 'source-B', 'snapshot-B')
        identity = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        for status in ('Draft', 'Active'):
            before = {'doctype':'HERO Frappe Mapping', 'name':'RULE', 'definition_id':identity,
                'owner':target.session.user, 'status':status, 'operation':'HeroCustomers', 'schema_snapshot':'snapshot-B',
                'source_path':'email', 'source_type':'string|null', 'target_doctype':'Customer',
                'target_field':'custom_import_email', 'purpose':'Field', 'transformation':'Trim', 'relation':'',
                'priority':5, 'mapping_revision':1}
            doc = fixture.PublicationDocument(target, {**before, 'mapping_revision':2}, False)
            object.__setattr__(doc, 'before', fixture.PublicationDocument(target, before, False))
            with fixture.TestPortableDraftExchange().context(target), patch.dict(sys.modules, {'frappe':target}), \
                 patch.object(hero_frappe_mapping, 'frappe', target), \
                 patch.object(hero_frappe_mapping, 'validate_draft_spec', return_value=SimpleNamespace(activation_allowed=True)), \
                 patch.object(hero_frappe_mapping, 'validate_spec'):
                with self.assertRaisesRegex(ValueError, 'revision is managed'):
                    hero_frappe_mapping.HEROFrappeMapping.validate(doc)
        self.assertEqual(target.rules, [])
