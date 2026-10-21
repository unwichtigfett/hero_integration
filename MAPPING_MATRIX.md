# Mapping matrix

The two fixed read-only HERO operations select 24 source paths. The app ships
no active or Draft field mapping definitions for those paths.

The isolated test fixture is
`hero_integration/tests/fixtures/hero_mapping_fixture_v1.json`. It is not a
Frappe fixture, is not installed, and is never imported automatically. It keeps
the previously reviewed source coverage for deterministic offline tests.

An imported production mapping must use these protected bindings where needed:

| Operation | Required explicit bindings |
| --- | --- |
| HeroCustomers | Source Identity, Record Name, Customer Type, Lifecycle Flag |
| HeroProjectMatches | Source Identity, Record Name, Customer Relation, Confirmed Company; the Record Name is the validated native Project title contract |

Regular site-specific mappings can write only writable Custom Fields. A package
can declare required `custom_hero_` Custom Fields and `HERO ` Custom DocTypes.
The Mapping Editor shows them beside mappings in one checklist. An upload is
read-only. A confirmed selection provisions schema before it activates a
mapping. Native
identity, Company, relationship, status, contact, and lifecycle bindings use
fixed protected purpose contracts. Unknown transformations and unresolved links
are rejected.

## RC mapper boundaries

The three similarly named stores are intentionally not interchangeable.

| UI name / DocType | Direction and purpose | Accepted persisted JSON | Execution boundary |
| --- | --- | --- | --- |
| **Map fields** / `HERO Frappe Mapping` | Classic, approved HERO query record to a Frappe field. This is the Customer/Project import mapping used by the migration runtime. | The export package is exactly `hero-frappe-mapping-package-v2` with `schema_fingerprint`, data-only `schema`, and `mappings`. Every mapping has exactly `definition_id`, `operation`, `source_path`, `source_type`, `target_doctype`, `target_field`, `purpose`, `transformation`, `relation`, `priority`, `schema_dependencies`, and `mapping_revision`. | Import is Draft-only; a reviewed mapping must be activated before a manual migration or AUTO_SYNC can use it. |
| **Query Mapping** / `HERO GraphQL Mapping` | Dynamic operation contract from a selected schema query root to an approved Frappe target. | `mapping_json` is one JSON object, stored canonically with its selected snapshot, fingerprint, source instance, operation and revision. It is not an import/export package and cannot replace the classic mapping package. | Save makes Draft unless the System Manager explicitly activates the reviewed contract. Schema drift returns it to Draft. |
| **Mutations Mapping** / `HERO GraphQL Mapping` plus `HERO Mutation Contract` | Dynamic Frappe/admin input to one selected schema mutation. The mapping direction is `FRAPPE_TO_MUTATION`; the separate contract holds the executable mutation document and input mode. | Both `mapping_json` values are JSON objects. The mutation contract requires `mode` of `ADMIN_INPUT` or `VALIDATED_RESOLVER`; it is bound to the exact service, schema fingerprint and contract revision. | Saving/activating a mapping never sends HERO data. Execution additionally requires the global site gate, active contract, AST/type-valid variables, current preview fingerprint and explicit confirmation. |

Examples in automated tests use synthetic schemas and values only. They are not a
HERO snapshot or a license to use any discovered live operation.
