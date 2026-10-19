from __future__ import annotations

import unittest

from hero_integration.services.project_naming import ProjectTitleValidationError, build_project_title


def project_match(**overrides):
    value = {
        "id": 1,
        "project_nr": "1111",
        "project_type": {"code": "PV"},
        "customer": {"title": "Herr", "full_name": "Max Mustermann"},
        "address": {
            "formatted_address": "", "street": "Musterstraße", "house_number": "10",
            "postal_code": "77777", "city": "Musterstadt",
        },
    }
    value.update(overrides)
    return value


class TestProjectNaming(unittest.TestCase):
    def test_exact_contract_example_is_byte_exact(self):
        self.assertEqual(
            build_project_title(project_match()),
            "PV-1111 | Photovoltaik, Herr Max Mustermann, Musterstraße 10, 77777 Musterstadt",
        )

    def test_preserves_leading_zeroes_and_does_not_repeat_existing_prefix(self):
        self.assertTrue(build_project_title(project_match(project_nr="0007")).startswith("PV-0007 |"))
        self.assertTrue(build_project_title(project_match(project_nr="PV-0007")).startswith("PV-0007 |"))
        self.assertTrue(build_project_title(project_match(project_nr="PV-PV-0007")).startswith("PV-0007 |"))

    def test_formatted_project_address_is_used_only_when_complete(self):
        title = build_project_title(project_match(address={"formatted_address": "Musterstraße 10, 77777 Musterstadt"}))
        self.assertTrue(title.endswith("Musterstraße 10, 77777 Musterstadt"))
        with self.assertRaisesRegex(ProjectTitleValidationError, "complete formatted"):
            build_project_title(project_match(address={"formatted_address": "Musterstraße 10"}))

    def test_missing_personal_or_trade_data_blocks_instead_of_guessing(self):
        for key, value in (
            ("project_type", {"code": "Unknown"}),
            ("customer", {"full_name": "Max Mustermann"}),
            ("customer", {"title": "Herr"}),
            ("address", {"street": "Musterstraße", "house_number": "10", "postal_code": "77777"}),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ProjectTitleValidationError):
                build_project_title(project_match(**{key: value}))

    def test_title_is_not_silently_truncated(self):
        with self.assertRaisesRegex(ProjectTitleValidationError, "field limit"):
            build_project_title(project_match(), max_length=20)
