"""Isolated test-only HERO mapping configuration. It is never installed."""
from __future__ import annotations

import json
from pathlib import Path

from hero_integration.services.direct_mapping import MappingSpec
from hero_integration.services.mapping_runtime import plan_from_definitions


FIXTURE_PATH = Path(__file__).with_name("fixtures") / "hero_mapping_fixture_v1.json"


def fixture_payload(schema_fingerprint: str = "synthetic-schema-v1") -> dict[str, object]:
    payload = json.loads(FIXTURE_PATH.read_text())
    # The archived V1 fixture remains source evidence. Tests exercise the current
    # package contract without turning it into an installed default.
    payload["format_version"] = "hero-frappe-mapping-package-v2"
    payload["schema"] = {"custom_doctypes": [], "custom_fields": [], "relations": []}
    for mapping in payload["mappings"]:
        mapping.setdefault("schema_dependencies", [])
        # The archived V1 package used coarse 10/20/100 bands. Test data is
        # deliberately normalised to the RC16 0..10 contract; the production
        # patch only converts the unambiguous former default 100.
        mapping["priority"] = {10: 0, 20: 1, 100: 5}[mapping["priority"]]
    payload["schema_fingerprint"] = schema_fingerprint
    return payload


def fixture_plan(operation: str):
    definitions = [
        MappingSpec(**{key: item[key] for key in MappingSpec.__dataclass_fields__})
        for item in fixture_payload()["mappings"]
    ]
    return plan_from_definitions(operation, definitions)
