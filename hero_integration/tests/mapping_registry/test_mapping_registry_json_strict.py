from __future__ import annotations

import json

from hero_integration.services.mapping_registry_json import (
    MappingRegistryJsonError,
    canonical_mapping_registry_json,
)
from hero_integration.tests.mapping_registry.test_mapping_registry_json import _export


def _rejected(value):
    try:
        canonical_mapping_registry_json(value)
    except MappingRegistryJsonError as error:
        return str(error)
    raise AssertionError("Canonicalizer must reject unknown v3 fields.")


def test_canonicalizer_rejects_unknown_root_and_nested_fields_without_value_leakage():
    root = json.loads(_export())
    root["token"] = "synthetic-sensitive-value"
    assert "synthetic-sensitive-value" not in _rejected(root)

    nested = json.loads(_export())
    nested["definitions"][0]["unexpected"] = True
    _rejected(nested)

    metadata = json.loads(_export())
    metadata["provenance"]["unexpected"] = True
    _rejected(metadata)
