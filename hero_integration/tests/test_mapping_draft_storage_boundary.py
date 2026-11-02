"""Offline boundaries for SQL identifier selection in draft publication."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import MappingDraftError


class TestPublicationIdentifierBoundary(unittest.TestCase):
    def test_unrecognized_doctype_is_rejected_before_sql(self):
        frappe = SimpleNamespace(db=SimpleNamespace(sql=Mock()))
        storage = FrappeMappingDraftStorage(frappe)
        for doctype in ("Customer", "HERO Frappe Mapping` where 1=1 -- ", ""):
            with self.subTest(doctype=doctype), self.assertRaises(MappingDraftError):
                storage._publication_object(doctype, "synthetic")
        frappe.db.sql.assert_not_called()
