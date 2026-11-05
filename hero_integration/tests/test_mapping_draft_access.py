"""Offline checks for personal mapping and Version permission hooks."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration import hooks
from hero_integration import api
from hero_integration.services import mapping_draft_access as access


class TestPersonalMappingAccess(unittest.TestCase):
    def setUp(self):
        self.frappe = SimpleNamespace(
            _=lambda message: message,
            session=SimpleNamespace(user="owner@example.invalid"),
            get_roles=Mock(return_value=["System Manager"]),
            db=SimpleNamespace(get_value=Mock(return_value="owner@example.invalid"),
                               escape=Mock(return_value="<escaped-user>")),
        )
        patcher = patch.object(access, "frappe", self.frappe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_personal_list_filters_require_manager_and_escape_owner(self):
        self.frappe.get_roles.return_value = ["Employee"]
        self.assertEqual(access.personal_mapping_query(doctype="HERO Mapping Draft"), "1=0")
        self.frappe.db.escape.assert_not_called()

    def test_confirmed_publication_deletes_only_after_success(self):
        fake = SimpleNamespace(session=SimpleNamespace(user='manager'), local=SimpleNamespace(site='site'),
            get_doc=Mock(return_value=SimpleNamespace(state='OPEN', generation=2, content_revision=3)))
        storage = Mock()
        storage.publish.return_value = {'name': 'active'}
        with patch.object(api, 'frappe', fake), patch.object(api, '_mapping_request_fields'), \
             patch.object(api, '_personal_draft_identity', return_value=('definition', 'key')), \
             patch('hero_integration.services.frappe_mapping_draft_storage.FrappeMappingDraftStorage', return_value=storage), \
             patch('hero_integration.services.mapping_draft_exchange._delete_replaced_draft') as delete:
            result = api.mapping_draft_publish('definition', 2, 3, 4, 0, confirmed=True, delete_after=True)
            self.assertEqual(result['name'], 'active')
            storage.service.return_value.discard.assert_called_once_with('definition', 'manager', 2, 3, confirmed=True)
            delete.assert_called_once()
            delete.reset_mock()
            storage.publish.side_effect = RuntimeError('failed')
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                api.mapping_draft_publish('definition', 2, 3, 4, 0, confirmed=True, delete_after=True)
            delete.assert_not_called()
        self.frappe.get_roles.return_value = ["System Manager"]
        for doctype, column in (("HERO Mapping Draft", "owner_user"), ("HERO Mapping Revision", "actor")):
            with self.subTest(doctype=doctype):
                query = access.personal_mapping_query(user="owner'example", doctype=doctype)
                self.assertIn(f"`tab{doctype}`.`{column}` = <escaped-user>", query)
                self.frappe.db.escape.assert_called_with("owner'example")

    def test_registered_hooks_work_with_only_the_user_argument(self):
        for doctype, field, function in (
            ("HERO Mapping Draft", "owner_user", access.personal_mapping_draft_query),
            ("HERO Mapping Revision", "actor", access.personal_mapping_revision_query),
        ):
            with self.subTest(doctype=doctype):
                self.assertEqual(hooks.permission_query_conditions[doctype],
                                 f"{access.__name__}.{function.__name__}")
                self.assertEqual(function("owner'example"),
                                 f"`tab{doctype}`.`{field}` = <escaped-user>")
                self.frappe.db.escape.assert_called_with("owner'example")
                self.frappe.get_roles.return_value = ["Employee"]
                self.assertEqual(function("employee@example.invalid"), "1=0")
                self.frappe.get_roles.return_value = ["System Manager"]
        self.assertNotIn("*", hooks.permission_query_conditions)

    def test_unknown_doctype_cannot_be_interpolated_in_a_query(self):
        self.assertEqual(access.personal_mapping_query("owner@example.invalid"), "1=0")
        self.assertEqual(access.personal_mapping_query(doctype="Invalid`Table"), "1=0")
        self.frappe.db.escape.assert_not_called()

    def test_configuration_import_exception_is_limited_to_one_document_and_scope(self):
        class Document:
            name = 'mapping'
            def __init__(self):
                self.previous = SimpleNamespace(status='Active', modified_by='owner', name='mapping',
                    get=lambda field: {'definition_id': 'definition', 'mapping_json': 'old'}.get(field))
            def is_new(self): return False
            def get_doc_before_save(self): return self.previous
            def get(self, field): return {'mapping_json': 'new'}.get(field)

        self.frappe.db.exists = Mock(return_value=True)
        self.frappe.PermissionError = PermissionError
        self.frappe.throw = Mock(side_effect=lambda message, *args: (_ for _ in ()).throw(PermissionError(message)))
        first, second = Document(), Document()
        with patch('hero_integration.services.frappe_mapping_draft_storage._fresh_definition_candidates', return_value=[object()]), \
             patch('hero_integration.services.frappe_mapping_draft_storage.is_personal_publication', return_value=False):
            with self.assertRaises(PermissionError):
                access.protect_managed_mapping(first)
            with access.configuration_import_mapping(first):
                access.protect_managed_mapping(first)
                with self.assertRaises(PermissionError):
                    access.protect_managed_mapping(second)
            with self.assertRaises(PermissionError):
                access.protect_managed_mapping(first)

    def test_personal_documents_require_owner_and_manager_role(self):
        for doctype in ("HERO Mapping Draft", "HERO Mapping Revision"):
            with self.subTest(doctype=doctype):
                doc = SimpleNamespace(doctype=doctype, owner_user="owner@example.invalid",
                                      actor="owner@example.invalid")
                self.assertTrue(access.personal_mapping_permission(doc))
                self.assertFalse(access.personal_mapping_permission(doc, "other@example.invalid"))
                self.frappe.get_roles.return_value = ["Employee"]
                self.assertFalse(access.personal_mapping_permission(doc))
                self.frappe.get_roles.return_value = ["System Manager"]

    def test_personal_version_list_filter_escapes_owner_and_preserves_other_versions(self):
        query = access.personal_version_query(user="owner'example")
        self.frappe.db.escape.assert_called_once_with("owner'example")
        self.assertIn("`tabVersion`.`ref_doctype` not in", query)
        self.assertIn("d.owner_user=<escaped-user>", query)
        self.assertIn("r.actor=<escaped-user>", query)
        self.assertNotIn("owner'example", query)

    def test_unrelated_versions_leave_native_permission_decision_unchanged(self):
        doc = SimpleNamespace(ref_doctype="Customer", docname="CUST-1")
        self.assertIsNone(access.personal_version_permission(doc))
        self.frappe.db.get_value.assert_not_called()
        self.frappe.get_roles.assert_not_called()

    def test_personal_versions_require_both_owner_and_system_manager(self):
        for doctype, column in (("HERO Mapping Draft", "owner_user"), ("HERO Mapping Revision", "actor")):
            doc = SimpleNamespace(ref_doctype=doctype, docname="synthetic")
            with self.subTest(doctype=doctype):
                self.assertTrue(access.personal_version_permission(doc))
                self.frappe.db.get_value.assert_called_with(doctype, "synthetic", column)
                self.assertFalse(access.personal_version_permission(doc, user="other@example.invalid"))
                self.frappe.get_roles.return_value = ["Employee"]
                self.assertFalse(access.personal_version_permission(doc))
                self.frappe.get_roles.return_value = ["System Manager"]

    def test_version_extension_denies_foreign_document_before_native_permission(self):
        native_check = Mock(return_value='native-result')

        class NativeVersion:
            def check_permission(self, *args, **kwargs):
                return native_check(*args, **kwargs)

        class Version(access.PersonalMappingVersionAccess, NativeVersion):
            ref_doctype = 'HERO Mapping Draft'
            docname = 'synthetic'

        self.frappe.PermissionError = PermissionError
        self.frappe.throw = lambda message, error: (_ for _ in ()).throw(error(message))
        self.frappe.db.get_value.return_value = 'other@example.invalid'
        with self.assertRaisesRegex(PermissionError, 'version access denied'):
            Version().check_permission('read')
        native_check.assert_not_called()

        self.frappe.db.get_value.return_value = 'owner@example.invalid'
        self.assertEqual(Version().check_permission('read'), 'native-result')
        native_check.assert_called_once_with('read')
