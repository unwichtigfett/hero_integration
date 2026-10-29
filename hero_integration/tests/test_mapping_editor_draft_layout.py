"""Mapping Editor: personal draft actions live in Map Fields; no separate lower draft area."""
from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
EDITOR = (ROOT / "public" / "js" / "hero_mapping_editor.js").read_text()
TOOLS = (ROOT / "public" / "js" / "hero_tools.js").read_text()
WORKSPACE = (ROOT / "public" / "js" / "hero_workspace.js").read_text()


def function(name: str) -> str:
    return EDITOR.split(f"function {name}(", 1)[1].split("\n        function ", 1)[0]


class TestMappingEditorDraftLayout(unittest.TestCase):
    def test_lower_personal_draft_area_is_gone(self):
        for removed in ("Selected definition and draft tools", "Personal draft field mappings", "Selected definition field mappings",
                        "hero-mapping-selected-tools", "Delete selected mapping", "Filter selected field mappings", "'New mapping'"):
            self.assertNotIn(removed, EDITOR)

    def test_draft_actions_are_in_map_fields_and_use_the_existing_lifecycle(self):
        detail = function("renderDetail")
        self.assertIn('const contextSection = body.find(".hero-mapping-context").last();', detail)
        actions = detail.split("hero-mapping-draft-actions", 1)[1].split("contextSection.append(actions);", 1)[0]
        for label in ('"Create Mapping"', '"Delete Mapping"', '"Activate Mapping"', '"Cancel new mapping"', "model.createAdditional()",
                      "activateDraft", "model.restore()", "Undo removal"):
            self.assertIn(label, actions)
        self.assertLess(detail.index("contextSection.append(actions);"), detail.index("renderMappingsTable(body);"))
        # Save, discard and activation keep their existing guarded functions.
        for contract in ('button("Save draft", save, true)', 'button("Discard saved draft", discard)', "function activateDraft()"):
            self.assertIn(contract, EDITOR)

    def test_rule_details_keep_transformation_and_definition_settings(self):
        detail = function("renderDetail")
        rules = detail.split("hero-mapping-rule-details", 1)[1].split("contextSection.append(parameters);", 1)[0]
        for contract in ("hero-mapping-row-detail", "records_path", "identity_path", "Configured structure and additional rules", "data-editable"):
            self.assertIn(contract, rules)
        self.assertIn("renderRow(list[selected], detail)", detail)

    def test_active_mappings_table_shows_the_open_draft_rows(self):
        table = function("renderMappingsTable")
        self.assertIn("const definition = isDraft ? model.content : item.definition || item;", table)
        self.assertIn('isDraft ? text("Personal draft")', table)
        self.assertIn('toggleClass("is-draft", isDraft)', table)
        self.assertIn("entries.unshift({item: active, kind: \"graphql\"})", table, "a new draft without an active mapping is listed too")
        self.assertIn('root.off("hero-mapping-content-change").on("hero-mapping-content-change", rowChangeHandler);', table)

    def test_newest_snapshot_is_the_default_and_labels_hide_the_uid(self):
        self.assertIn("if (latest && !snapshotFilter) snapshotFilter = latest.name;", function("loadList"))
        self.assertIn("snapshotLabels.set(item.name", EDITOR)
        for display in ("snapshotLabel(active.schema_snapshot)", "snapshotLabel(item.schema_snapshot)", "snapshotLabel(row.name)",
                        "snapshotLabel(item.snapshot_name)", "snapshotLabel(snapshotFilter)"):
            self.assertIn(display, EDITOR)
        self.assertIn("snapshot.display_name||snapshot.name", TOOLS)
        self.assertIn("default: identity.snapshot_label || identity.snapshot_name", WORKSPACE)
        self.assertIn("snapshot_name: identity.snapshot_name, node_id: identity.node_id", WORKSPACE, "the UID is what gets saved")


class TestSnapshotInheritanceUi(unittest.TestCase):
    def test_inheritance_is_an_explicit_confirmed_draft_only_action(self):
        inherit = function("inheritFromParent")
        self.assertIn('rpc("workspace_snapshot_inherit_mappings", {snapshot_name: snapshotName}, true)', inherit)
        self.assertIn("modal(", inherit)
        self.assertIn("Nothing is activated", inherit)
        for forbidden in ("mapping_draft_publish", "activateDraft", "mapping_field_rule_activate"):
            self.assertNotIn(forbidden, inherit)
        self.assertIn("snapshotParents.set(item.name, item.parent_snapshot)", EDITOR)
        self.assertIn("row.configured_source === true", EDITOR, "the configured HERO source wins the default snapshot")


if __name__ == "__main__":
    unittest.main()
