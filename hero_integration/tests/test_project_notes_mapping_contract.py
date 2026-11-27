from __future__ import annotations

import unittest
from types import SimpleNamespace

from hero_integration.integrations.erpnext.project_notes import NativeProjectNotesError, require_native_project_notes
from hero_integration.services.direct_mapping import DirectMappingError, MappingSpec, validate_spec


class TestProjectNotesMappingContract(unittest.TestCase):
    def test_native_notes_are_required_and_editable(self):
        require_native_project_notes(SimpleNamespace(get_field=lambda name: SimpleNamespace(read_only=0) if name == "notes" else None))
        with self.assertRaises(NativeProjectNotesError):
            require_native_project_notes(SimpleNamespace(get_field=lambda name: None))
        with self.assertRaises(NativeProjectNotesError):
            require_native_project_notes(SimpleNamespace(get_field=lambda name: SimpleNamespace(read_only=1)))

    def test_partner_notes_target_native_notes_only_as_converted_text(self):
        metadata = _Metadata()
        with self.assertRaises(DirectMappingError):
            validate_spec(MappingSpec("HeroProjectMatches", "partner_notes", "Project", "notes", "string|null"), metadata)
        validate_spec(MappingSpec("HeroProjectMatches", "partner_notes", "Project", "notes", "string|null",
                                  transformation="Plain Text To HTML"), metadata)
        # Older mapping revisions keep their app field.

        validate_spec(
            MappingSpec(
                "HeroProjectMatches", "partner_notes", "Project", "custom_hero_partner_notes", "string|null",
                transformation="Trim",
            ),
            metadata,
        )

    def test_explicit_outbound_inverse_preserves_plain_text_roundtrip(self):
        from hero_integration.services.frappe_writeback import _transform, WritebackContractError
        from hero_integration.services.direct_mapping import plain_text_html

        text = "HERO <tag> & note\nnext line"
        self.assertEqual(_transform(plain_text_html(text), "HTML To Plain Text"), text)
        with self.assertRaises(WritebackContractError):
            _transform("<p><strong>Formatting</strong></p>", "HTML To Plain Text")

    def test_authority_excludes_cross_direction_writes_and_unknown_rules(self):
        from hero_integration.services.mapping_runtime import field_authorities, MappingPlanError
        self.assertEqual(field_authorities({"field_authority": {"rule": "ERPNEXT"}}, ["rule"]), {"rule": "ERPNEXT"})
        for invalid in ({"other": "HERO"}, {"rule": "OWNER"}, None, [1]):
            with self.subTest(value=invalid), self.assertRaises(MappingPlanError):
                field_authorities({"field_authority": invalid}, ["rule"])


class _Metadata:
    def ensure_write(self, doctype):
        if doctype != "Project":
            raise AssertionError(doctype)

    @staticmethod
    def field(doctype, fieldname):
        if (doctype, fieldname) == ("Project", "custom_hero_partner_notes"):
            return SimpleNamespace(fieldtype="Small Text", read_only=0, is_custom_field=1)
        if (doctype, fieldname) == ("Project", "notes"):
            return SimpleNamespace(fieldtype="Text Editor", read_only=0, is_custom_field=0)
        return None


if __name__ == "__main__":
    unittest.main()
