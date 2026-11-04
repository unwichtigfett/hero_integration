"""Source contracts for Remember for Mapping in API Analyzer, Graph Viewer and Mapping Editor."""
from __future__ import annotations

from pathlib import Path
import unittest

JS = Path(__file__).resolve().parents[1] / "public" / "js"
TOOLS = (JS / "hero_tools.js").read_text()
WORKSPACE = (JS / "hero_workspace.js").read_text()



class TestNodeBookmarkUiContract(unittest.TestCase):
    def test_one_canonical_store_and_dialog_for_all_tools(self):
        self.assertIn("window.hero_integration_bookmarks = Object.freeze(bookmarks)", WORKSPACE)
        self.assertIn("const identity = Object.freeze({...selection})", WORKSPACE)
        for method in ("workspace_node_bookmark_list", "workspace_node_bookmark_save", "workspace_node_bookmark_rename",
                       "workspace_node_bookmark_delete", "workspace_node_bookmark_resolve"):
            self.assertIn(method, WORKSPACE)
        self.assertNotIn("workspace_node_bookmark", TOOLS, "tools must use the shared store only")

    def test_analyzer_graph_and_focus_share_one_detail_renderer_with_the_button(self):
        self.assertIn("'Remember for Mapping'", TOOLS)
        self.assertIn("const remember=this.rememberButton(data.node,scope)", TOOLS)
        # Analyzer, graph inspector and node focus all render through detailContents.
        for call in ("this.detailContents(this.typeDetail", "this.detailContents(this.nodeDetail", "this.detailContents(this.focusDetail"):
            self.assertIn(call, TOOLS)
        self.assertIn("if(this.scopeKey(scope)!==this.scopeKey())", TOOLS.split("rememberButton(node,scope){", 1)[1].split("detailContents(", 1)[0])
        overview = TOOLS.split("renderGraphOverview(", 1)[1].split("renderGraphScene(", 1)[0]
        self.assertNotIn("Remember for Mapping", overview, "class cards are aggregates, not nodes")



if __name__ == "__main__":
    unittest.main()
