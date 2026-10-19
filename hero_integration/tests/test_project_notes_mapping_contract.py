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

    def test_partner_notes_never_target_native_project_notes(self):
        metadata = _Metadata()
        with self.assertRaises(DirectMappingError):
            validate_spec(MappingSpec("HeroProjectMatches", "partner_notes", "Project", "notes", "string|null"), metadata)

        validate_spec(
            MappingSpec(
                "HeroProjectMatches", "partner_notes", "Project", "custom_hero_partner_notes", "string|null",
                transformation="Trim",
            ),
            metadata,
        )


class _Metadata:
    def ensure_write(self, doctype):
        if doctype != "Project":
            raise AssertionError(doctype)

    @staticmethod
    def field(doctype, fieldname):
        if (doctype, fieldname) == ("Project", "custom_hero_partner_notes"):
            return SimpleNamespace(fieldtype="Small Text", read_only=0, is_custom_field=1)
        return None


if __name__ == "__main__":
    unittest.main()
