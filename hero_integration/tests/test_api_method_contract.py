from __future__ import annotations

import ast
from pathlib import Path
import unittest


API = Path(__file__).resolve().parents[1] / "api.py"
WRITE_RPC_NAMES = {
    "start_migration", "pending_sync", "sync_hero_execute", "cancel_run", "resume_run", "workspace_schema_refresh", "workspace_settings_save",
    "workspace_testing_settings_save",
    "workspace_mutation_execution_set", "graphql_mapping_save", "runtime_plan_approve", "workspace_service_save", "workspace_service_validate",
    "workspace_service_delete", "adopt_mapping_package", "resume_mapping_adoption",
    "workspace_mutation_contract_save", "execute_graphql_service", "writeback_execute",
    "mapping_draft_create", "mapping_draft_save", "mapping_draft_publish",
    "mapping_draft_exchange_save", "workspace_service_execute", "workspace_analyzer_service_save",
    "workspace_node_bookmark_save", "workspace_node_bookmark_rename", "workspace_node_bookmark_delete",
    "sync_area_start", "sync_service_set",
    "approve_read_service", "task_assignment_policy_create", "task_assignment_policy_approve",
    "task_assignment_policy_revoke", "mapping_draft_enroll_portable_rule",
    "mapping_draft_enroll_portable_identity", "mapping_draft_exchange_preview",
    "mapping_draft_reconcile_preview",
    "mapping_draft_reconcile_apply", "decide_hero_review",
}
READ_RPC_NAMES = {"runtime_plan_status",
                  "mapping_draft_read", "mapping_draft_exchange_export",
                  "workspace_node_bookmark_list", "workspace_node_bookmark_resolve",
                  "sync_page_state", "sync_page_runs", "sync_page_run_detail", "sync_page_conflicts",
                  "sync_page_conflict_report"}


class TestApiMethodContract(unittest.TestCase):
    def test_selected_write_and_preview_rpcs_are_registered_for_post_only(self):
        tree = ast.parse(API.read_text())
        methods = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                if decorator.func.attr != "whitelist":
                    continue
                methods[node.name] = next(
                    (keyword.value for keyword in decorator.keywords if keyword.arg == "methods"), None
                )
        self.assertEqual(set(methods) & WRITE_RPC_NAMES, WRITE_RPC_NAMES)
        for name in WRITE_RPC_NAMES:
            self.assertIsInstance(methods[name], ast.List)
            self.assertEqual([item.value for item in methods[name].elts], ["POST"])

    def test_frontend_read_rpcs_are_registered_for_get_only(self):
        tree = ast.parse(API.read_text())
        methods = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                if decorator.func.attr != "whitelist":
                    continue
                methods[node.name] = next(
                    (keyword.value for keyword in decorator.keywords if keyword.arg == "methods"), None
                )
        self.assertEqual(set(methods) & READ_RPC_NAMES, READ_RPC_NAMES)
        for name in READ_RPC_NAMES:
            self.assertIsInstance(methods[name], ast.List)
            self.assertEqual([item.value for item in methods[name].elts], ["GET"])


    def test_retired_mapping_editor_rpcs_are_absent(self):
        methods = {node.name for node in ast.parse(API.read_text()).body if isinstance(node, ast.FunctionDef)}
        self.assertTrue(methods.isdisjoint({'workspace_snapshot_inherit_mappings', 'mapping_draft_revisions', 'mapping_draft_discard', 'mapping_field_rule_activate', 'mapping_draft_materialize_plan', 'graphql_mapping_list', 'frontend_mapping_bindings', 'mapping_draft_new', 'frontend_mapping_detail', 'frontend_mapping_list', 'mapping_draft_new_seed'}))
