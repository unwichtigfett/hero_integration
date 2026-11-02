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
        self.assertIn("$('label','ht-field',tr('Variables')),variablesBox", TOOLS)
        self.assertIn("this.variables.setAttribute('aria-label',tr('Variables'))", TOOLS)
        self.assertNotIn("Variables (JSON)", TOOLS)
        # No JSON preview below the Variables field; problems are flagged on the field itself.
        for preview in ("variablesJson", "Advanced: JSON variables", "ht-variables-json"):
            self.assertNotIn(preview, TOOLS)
        self.assertIn("this.variables.setAttribute('aria-invalid','true')", TOOLS)
        self.assertIn('id := 5;\\noffset := 5;\\norderBy := \"id\";', TOOLS)
        self.assertIn("JSON.parse(raw)", TOOLS)
        for forbidden in ("eval(", "new Function", "Function("):
            self.assertNotIn(forbidden, TOOLS)
        payload = TOOLS.split("        payload(){", 1)[1].split("\n", 1)[0]
        self.assertNotIn("variables", payload, "variable values are never part of the saved service")
        saved_shape = TOOLS.split("        queryString(q) {", 1)[1].split("\n        }", 1)[0].split("return", 1)[1]
        self.assertNotIn("variables", saved_shape)

    def test_colour_coding_uses_scoped_theme_tokens_in_light_and_dark(self):
        adapter = (JS.parent / "css" / "hero_adapter.css").read_text()
        tools_css = (JS.parent / "css" / "hero_tools.css").read_text()
        light, dark = adapter.split(":is(html[data-theme=\"dark\"]", 1)
        for token in ("--hero-syntax-keyword", "--hero-syntax-string", "--hero-syntax-number", "--hero-syntax-boolean",
                      "--hero-syntax-null", "--hero-syntax-variable", "--hero-syntax-key", "--hero-state-ok",
                      "--hero-state-warning", "--hero-state-error"):
            self.assertIn(token + ":", light)
            self.assertIn(token + ":", dark.split("}", 1)[0])
        self.assertNotIn(":root", adapter)
        for rule in (".ht-tok-string", ".ht-tok-error", ".ht-editor .ace_text-layer .ace_keyword", '.ht-result-status[data-state="PARTIAL"]'):
            self.assertIn(rule, tools_css)
        self.assertIn("this.editor.session.setMode(graphqlAceMode(window.ace))", TOOLS)
        # Colours are spans with textContent; no HTML is injected.
        self.assertIn("$('span', 'ht-tok ht-tok-' + type.replace", TOOLS)

    def test_results_and_errors_are_rendered_as_text_and_distinguished(self):
        render = TOOLS.split("renderExecution(result){", 1)[1].split("\n        async deleteQuery(", 1)[0]
        self.assertIn("textContent", render)
        self.assertNotIn("innerHTML", render)
        self.assertNotIn(".html(", render)
        for contract in ("GRAPHQL", "TRANSPORT", "RESPONSE", "TOO_LARGE", "PARTIAL", "'GraphQL Query Result'",
                         "'Readable'", "'Raw JSON'", "'Copy Result'", "primary_action_label:tr('Close')", "admin-compat-modal"):
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
