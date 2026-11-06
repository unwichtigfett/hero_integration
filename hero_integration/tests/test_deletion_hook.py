from __future__ import annotations

import json

import frappe
from frappe.tests import IntegrationTestCase

import hero_integration.hooks


_HANDLER = "hero_integration.services.guards.assert_app_document_deletion_allowed"


class TestDeletionHook(IntegrationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._original_user = frappe.session.user
        frappe.set_user("Administrator")
        self.addCleanup(frappe.set_user, self._original_user)

        self._original_allow_deletion = frappe.db.get_single_value(
            "HERO Integration Settings", "allow_deletion", cache=False
        )
        frappe.db.set_single_value("HERO Integration Settings", "allow_deletion", 0)
        self.addCleanup(
            frappe.db.set_single_value,
            "HERO Integration Settings",
            "allow_deletion",
            self._original_allow_deletion,
        )

    def test_listed_hero_doctypes_use_deletion_guard_without_replacing_native_hooks(self) -> None:
        self.assertTrue(hero_integration.hooks._HERO_OWNED_DOCTYPES)
        self.assertTrue(
            all(
                hero_integration.hooks.doc_events[doctype].get("on_trash") == _HANDLER
                for doctype in hero_integration.hooks._HERO_OWNED_DOCTYPES
            )
        )
        self.assertEqual(
            set(hero_integration.hooks.doc_events),
            # Project/Version: native activity handling; Helpdesk/CRM: ERPNext reference fields.
            {*hero_integration.hooks._HERO_OWNED_DOCTYPES, "*", "Task", "ToDo", "Project", "Version", "User", "Employee",
             "HD Ticket", "ERPNext HD Settings", "CRM Lead", "CRM Deal", "ERPNext CRM Settings"},
        )
        self.assertEqual(
            hero_integration.hooks.doc_events["*"]["on_update"],
            "hero_integration.services.change_detection.observe_frappe_target_change",
        )
        self.assertEqual(
            hero_integration.hooks.doc_events["Task"]["validate"],
            "hero_integration.services.change_detection.protect_external_task_deadline",
        )
        for doctype in ("User", "Employee"):
            self.assertEqual(hero_integration.hooks.doc_events[doctype]["validate"],
                             "hero_integration.services.change_detection.protect_employee_source_observations")
        self.assertEqual(
            hero_integration.hooks.doc_events["Version"],
            {"before_insert": "hero_integration.services.project_logbook_writeback.mark_imported_project_version"},
        )
        self.assertEqual(
            hero_integration.hooks.doc_events["ToDo"],
            {"on_update": "hero_integration.services.task_assignment_state.observe_local_todo",
             "on_trash": "hero_integration.services.task_assignment_state.observe_local_todo"},
        )

    def test_delete_doc_dispatches_to_the_disabled_deletion_guard(self) -> None:
        self.assertEqual(
            frappe.db.get_single_value("HERO Integration Settings", "allow_deletion", cache=False), 0
        )
        snapshot = frappe.get_doc(
            {
                "doctype": "HERO GraphQL Schema Snapshot",
                "snapshot_json": json.dumps(
                    {
                        "__schema": {
                            "types": [
                                {
                                    "kind": "OBJECT",
                                    "name": "Query",
                                    "fields": [
                                        {
                                            "name": f"delete_hook_test_{frappe.generate_hash(length=12)}",
                                            "type": {"kind": "SCALAR", "name": "String"},
                                            "args": [],
                                        }
                                    ],
                                },
                                {"kind": "SCALAR", "name": "String"},
                            ],
                            "queryType": {"name": "Query"},
                        }
                    }
                ),
            }
        ).insert()
        self.assertTrue(
            frappe.has_permission(snapshot.doctype, "delete", doc=snapshot, user=frappe.session.user)
        )

        expected_message = f"HERO Integration deletion is disabled; refusing deletion of {snapshot.doctype} {snapshot.name}."
        with self.assertRaises(frappe.PermissionError) as raised:
            frappe.delete_doc(snapshot.doctype, snapshot.name)
        self.assertEqual(str(raised.exception), expected_message)

        self.assertTrue(frappe.db.exists(snapshot.doctype, snapshot.name))
