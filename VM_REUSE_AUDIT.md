# VM reuse audit — v1 release candidate

The VM source was inspected read-only through `ssh fd` on 2026-10-21. No VM
site configuration, credentials, keys, data, or runtime imports are used by
this standalone application.

| Capability | VM reference (SHA-256) | Standalone target | Outcome |
| --- | --- | --- | --- |
| HTTPS allowlist/no-redirect GraphQL transport | `hero_connector/client/endpoint_security.py` (`d070964…`), `client/graphql_readonly.py` (`dc764c…`) | `hero_integration/client/graphql.py` | Existing implementation retained; query and introspection share its hardened transport. |
| Complete introspection parsing and deterministic snapshot identity | `hero_connector/tools/graphql_introspection.py` (`6db8e8…`), `tools/graphql_schema_snapshot.py` (`ffd77f…`) | `services/introspection.py`, `services/graphql_graph.py`, schema snapshot DocType | Adapted to Frappe Documents, source-instance provenance and structural breaking-change gates. |
| GraphQL write request fingerprint and exact approval binding | `hero_connector/graphql/graphql_write_safety.py` (`a12d8…`) | `services/graphql_contract.py`, `api.py`, mutation contract/audit DocTypes | Adapted as a standalone AST-like single-operation contract; no import from `hero_connector`. |
| Nested input, scalar, enum and non-null validation | `hero_connector/graphql/graphql_argument_values.py` (`979a…`) | `services/graphql_contract.py` | Adapted for JSON request variables and saved introspection schemas. |
| Read-only monitoring/cadence patterns | `auto_domain` integration job/cadence sources, read-only inventory | `services/polling.py`, polling event/setting fields | New standalone implementation because VM code is domain-coupled; OFF/MONITOR/AUTO_SYNC semantics retained. |

No duplicate implementation was copied verbatim. VM components were used as
behavioral references only where the standalone release contract needs them.
The standalone tree has no runtime `auto_domain` or `hero_connector` import.
