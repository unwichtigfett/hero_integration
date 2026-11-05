"""Snapshot labels remain readable while bookmark writes retain stable identifiers."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestSnapshotLabelsUi(unittest.TestCase):
    def test_snapshot_labels_use_display_names_and_bookmarks_keep_identifiers(self):
        tools = (ROOT / "public" / "js" / "hero_tools.js").read_text()
        workspace = (ROOT / "public" / "js" / "hero_workspace.js").read_text()
        self.assertIn("snapshot.display_name||snapshot.name", tools)
        self.assertIn("default: identity.snapshot_label || identity.snapshot_name", workspace)
        self.assertIn("snapshot_name: identity.snapshot_name, node_id: identity.node_id", workspace)


if __name__ == "__main__":
    unittest.main()
