"""Validated GraphQL introspection and snapshot provenance for HERO.

The module deliberately has no Frappe or transport dependency.  It accepts a
complete GraphQL introspection response, rejects malformed shapes, and returns
the canonical schema consumed by the existing graph and mapping services.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone

from hero_integration.services.graphql_graph import GraphSnapshotError, analyze_snapshot, normalize_schema


class IntrospectionError(ValueError):
    pass


# This is the standard complete schema query.  It contains roots, fields,
# arguments, input objects, enums, lists/non-null wrappers and directives.
INTROSPECTION_QUERY = """
query HeroIntegrationIntrospection {
  __schema {
    queryType { name }
    mutationType { name }
    subscriptionType { name }
    types {
      kind name description
      fields(includeDeprecated: true) {
        name description isDeprecated deprecationReason
        args { name description defaultValue type { ...TypeRef } }
        type { ...TypeRef }
      }
      inputFields { name description defaultValue type { ...TypeRef } }
      interfaces { ...TypeRef }
      enumValues(includeDeprecated: true) { name description isDeprecated deprecationReason }
      possibleTypes { ...TypeRef }
    }
    directives {
      name description locations isRepeatable
      args { name description defaultValue type { ...TypeRef } }
    }
  }
}
fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType {
          kind
          name
          ofType {
            kind
            name
            ofType {
              kind
              name
              ofType {
                kind
                name
              }
            }
          }
        }
      }
    }
  }
}
""".strip()

# Older GraphQL servers expose the same schema but predate the optional
# __Directive.isRepeatable field.  Keep the primary query complete and use
# this exact read-only compatibility form only after a GraphQL rejection.
COMPATIBILITY_INTROSPECTION_QUERY = INTROSPECTION_QUERY.replace(" locations isRepeatable", " locations")


def canonical_schema(payload: object) -> dict[str, object]:
    """Validate and normalize a complete introspection envelope.

    Only the standard ``data.__schema`` envelope is accepted.  This prevents a
    response with GraphQL errors or a partial arbitrary payload from becoming a
    trusted snapshot.
    """
    if not isinstance(payload, Mapping):
        raise IntrospectionError("GraphQL introspection response must be an object.")
    data = payload.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("__schema"), Mapping):
        raise IntrospectionError("GraphQL introspection response has no data.__schema object.")
    if payload.get("errors"):
        raise IntrospectionError("GraphQL introspection response contains errors.")
    try:
        schema = normalize_schema({"__schema": dict(data["__schema"])})
        _validate_complete_schema(schema)
        analyze_snapshot({"__schema": schema})
    except GraphSnapshotError as error:
        raise IntrospectionError(str(error)) from error
    return schema


def endpoint_fingerprint(endpoint_url: str, source_instance: str) -> str:
    canonical = json.dumps(
        {"endpoint": endpoint_url, "source_instance": source_instance},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def structural_diff(previous: object | None, current: dict[str, object]) -> dict[str, object]:
    """Return a deterministic schema diff suitable for execution gates."""
    if previous is None:
        return {"status": "INITIAL", "breaking": False, "added_types": [], "removed_types": [], "changed_roots": []}
    try:
        before = normalize_schema(previous)
        _validate_complete_schema(before)
    except (GraphSnapshotError, IntrospectionError, TypeError, ValueError):
        return {"status": "PREVIOUS_INVALID", "breaking": True, "added_types": [], "removed_types": [], "changed_roots": []}
    before_types = {item["name"]: item for item in before["types"] if isinstance(item, dict) and isinstance(item.get("name"), str)}
    after_types = {item["name"]: item for item in current["types"] if isinstance(item, dict) and isinstance(item.get("name"), str)}
    removed = sorted(set(before_types) - set(after_types))
    added = sorted(set(after_types) - set(before_types))
    changed_roots = [
        name for name in ("queryType", "mutationType", "subscriptionType")
        if _root_name(before.get(name)) != _root_name(current.get(name))
    ]
    removed_fields: list[str] = []
    for name in sorted(set(before_types) & set(after_types)):
        old_fields = {field.get("name") for field in before_types[name].get("fields") or [] if isinstance(field, dict)}
        new_fields = {field.get("name") for field in after_types[name].get("fields") or [] if isinstance(field, dict)}
        removed_fields.extend(f"{name}.{field}" for field in sorted(old_fields - new_fields) if field)
    breaking = bool(removed or changed_roots or removed_fields)
    return {
        "status": "BREAKING" if breaking else "COMPATIBLE",
        "breaking": breaking,
        "added_types": added,
        "removed_types": removed,
        "removed_fields": removed_fields,
        "changed_roots": changed_roots,
    }


def captured_at_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _validate_complete_schema(schema: dict[str, object]) -> None:
    types = schema.get("types")
    if not isinstance(types, list) or not types:
        raise IntrospectionError("GraphQL introspection schema requires a non-empty types list.")
    query = _root_name(schema.get("queryType"))
    names = {item.get("name") for item in types if isinstance(item, Mapping) and isinstance(item.get("name"), str)}
    if not query or query not in names:
        raise IntrospectionError("GraphQL introspection schema has no resolvable query root.")
    for item in types:
        if not isinstance(item, Mapping) or not isinstance(item.get("kind"), str) or not isinstance(item.get("name"), str):
            raise IntrospectionError("GraphQL introspection schema contains an invalid type.")
        for member in ("fields", "inputFields", "enumValues", "interfaces", "possibleTypes"):
            if member in item and item[member] is not None and not isinstance(item[member], list):
                raise IntrospectionError(f"GraphQL introspection type {item['name']!r} has invalid {member}.")


def _root_name(value: object) -> str:
    return str(value.get("name") or "") if isinstance(value, Mapping) else ""
