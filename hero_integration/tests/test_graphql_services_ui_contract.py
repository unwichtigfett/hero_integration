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

    def test_execute_never_runs_an_older_revision_silently(self):
        execute = TOOLS.split("async executeQuery(){", 1)[1].split("\n        renderExecution(", 1)[0]
        self.assertIn("if(this.serviceKind!=='query')throw new Error", execute)
        # Unsaved content is saved and validated only after an explicit confirmation; otherwise nothing runs.
        self.assertIn("if(!this.query||this.dirty){", execute)
        self.assertIn("window.confirm(", execute)
        self.assertIn("Nothing was executed", execute)
        self.assertIn("call('execute_service',{...this.serviceScope(true),query_id:queryId,expected_revision:revision,variables}", execute)
        self.assertIn("this.executeButton.disabled=!allowed||!this.canExecuteQuery()", TOOLS)
        self.assertIn("'Execute Query'", TOOLS)

    def test_variables_are_runtime_input_in_name_value_lines(self):
        self.assertIn("field('Variables',this.variables)", TOOLS)
        self.assertNotIn("Variables (JSON)", TOOLS)
        self.assertIn("JSON.parse(raw)", TOOLS)
        for forbidden in ("eval(", "new Function", "Function("):
            self.assertNotIn(forbidden, TOOLS)
        payload = TOOLS.split("        payload(){", 1)[1].split("\n", 1)[0]
        self.assertNotIn("variables", payload, "variable values are never part of the saved service")
        saved_shape = TOOLS.split("        queryString(q) {", 1)[1].split("\n        }", 1)[0].split("return", 1)[1]
        self.assertNotIn("variables", saved_shape)

    def test_results_and_errors_are_rendered_as_text_and_distinguished(self):
        render = TOOLS.split("renderExecution(result){", 1)[1].split("\n        async deleteQuery(", 1)[0]
        self.assertIn("textContent", render)
        self.assertNotIn("innerHTML", render)
        self.assertNotIn(".html(", render)
        for contract in ("GRAPHQL", "TRANSPORT", "RESPONSE", "TOO_LARGE", "PARTIAL", "'GraphQL Query Result'",
                         "'Readable'", "'Raw JSON'", "'Copy Result'", "primary_action_label:'Close'", "admin-compat-modal"):
            self.assertIn(contract, render)
        self.assertNotIn("call(", render, "showing a result never triggers another request")

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
