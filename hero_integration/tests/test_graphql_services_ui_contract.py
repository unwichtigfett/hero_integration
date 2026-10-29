"""Source contracts for the GraphQL Services editor and API Analyzer wiring."""
from __future__ import annotations

from pathlib import Path
import unittest

JS = Path(__file__).resolve().parents[1] / "public" / "js"
TOOLS = (JS / "hero_tools.js").read_text()
WORKSPACE = (JS / "hero_workspace.js").read_text()


class TestGraphQLServicesUiContract(unittest.TestCase):
    def test_query_execution_is_wired_only_through_the_read_only_endpoint(self):
        self.assertIn('request("workspace_service_execute"', WORKSPACE)
        self.assertIn('if (args.operation_kind !== "query") throw new Error("HERO_MUTATION_EXECUTION_DISABLED")', WORKSPACE)
        self.assertNotIn("execute_graphql_service", WORKSPACE + TOOLS)
        self.assertNotIn("workspace_mutation_execution_set", TOOLS)

    def test_execute_requires_saved_unchanged_validated_query_and_explicit_click(self):
        self.assertIn("canExecuteQuery(){return this.serviceKind==='query'&&!!this.query&&!this.dirty", TOOLS)
        self.assertIn("this.executeButton.disabled=!allowed||!this.canExecuteQuery()", TOOLS)
        self.assertIn("if(this.serviceKind!=='query')throw new Error", TOOLS)

    def test_results_and_errors_are_rendered_as_text_and_distinguished(self):
        render = TOOLS.split("renderExecution(result){", 1)[1].split("\n        }", 1)[0]
        self.assertIn("textContent", render)
        self.assertNotIn("innerHTML", render)
        for kind in ("GRAPHQL", "TRANSPORT", "RESPONSE", "TOO_LARGE"):
            self.assertIn(kind, render)

    def test_mutation_editor_shows_backend_gate_and_never_executes(self):
        self.assertIn("this.mutationExecution=data.mutation_execution", TOOLS)
        self.assertIn("Mutation drafts are local-only", TOOLS)
        self.assertIn("HERO_MUTATION_EXECUTION_DISABLED", TOOLS)

    def test_analyzer_service_drafts_are_enabled_and_mapped(self):
        self.assertNotIn("require legacy transport state", TOOLS)
        self.assertIn('request("workspace_analyzer_service_draft"', WORKSPACE)
        self.assertIn('request("workspace_analyzer_service_save"', WORKSPACE)
        unsupported = WORKSPACE.split("const unsupported = new Set([", 1)[1].split("]);", 1)[0]
        for method in ("execute_service", "get_analyzer_service_draft", "save_analyzer_service"):
            self.assertNotIn(method, unsupported)


if __name__ == "__main__":
    unittest.main()
