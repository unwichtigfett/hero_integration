# Mapping matrix

The two fixed read-only HERO operations select 19 source paths. The app ships
no active or Draft field mapping definitions for those paths.

The isolated test fixture is
`hero_integration/tests/fixtures/hero_mapping_fixture_v1.json`. It is not a
Frappe fixture, is not installed, and is never imported automatically. It keeps
the previously reviewed source coverage for deterministic offline tests.

An imported production mapping must use these protected bindings where needed:

| Operation | Required explicit bindings |
| --- | --- |
| HeroCustomers | Source Identity, Record Name, Customer Type, Lifecycle Flag |
| HeroProjectMatches | Source Identity, Record Name, Customer Relation, Confirmed Company |

Regular site-specific mappings can write only writable Custom Fields. A package
can declare required `custom_hero_` Custom Fields and `HERO ` Custom DocTypes.
The Mapping Editor shows them beside mappings in one checklist. An upload is
read-only. A confirmed selection provisions schema before it activates a
mapping. Native
identity, Company, relationship, status, contact, and lifecycle bindings use
fixed protected purpose contracts. Unknown transformations and unresolved links
are rejected.
