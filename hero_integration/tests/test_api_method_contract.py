from __future__ import annotations

import ast
from pathlib import Path
import unittest


API = Path(__file__).resolve().parents[1] / "api.py"
WRITE_RPC_NAMES = {
    "start_migration", "cancel_run", "resume_run", "workspace_schema_refresh", "workspace_settings_save",
    "workspace_mutation_execution_set", "graphql_mapping_save", "workspace_service_save", "workspace_service_validate",
    "workspace_service_delete", "adopt_mapping_package", "resume_mapping_adoption",
    "workspace_mutation_contract_save", "execute_graphql_service",
}


class TestApiMethodContract(unittest.TestCase):
    def test_every_mutating_rpc_is_registered_for_post_only(self):
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
