# Mapping Matrix

The HERO integration supports four GraphQL read operations with **76 active field mapping definitions**.

| Operation | Mappings | Frappe Targets | Status |
|---|---:|---|---|
| `HeroCustomers` | 15 (V2) | Customer, Contact, Address | Approved |
| `HeroProjectMatches` | 21 | Project | Approved |
| `HeroCalendarEvents` | 25 | Event, HERO Event Resource, HERO Event Partner | Pending Approval |
| `HeroTasks` | 15 | HERO External Task | Approved |

Customer V1 remains approved with 14 selected mappings. Customer V2 adds M-099 (`address.line_2 → Address.address_line2`). Both revisions require explicit service selection.

## Schema and Bindings

The integration reuses five standard DocTypes, three HERO DocTypes, and 35 provisioned Custom Fields.

Protected binding contracts cover:

- **Customer:** Source Identity, Record Name, Customer Type, Lifecycle, native Contact/Address relationships.
- **Project:** Source Identity, Project Title, Customer Relation, Confirmed Company.
- **Calendar:** Event identity, time values, project relations, resources, partners, and validated timezone semantics.
- **Tasks:** External identity, project association, and external user references. Workflow status and assignments remain protected.

Mappings use the existing revision-controlled mapping engine and native adapters. Unsupported transformations, ambiguous identities, and unresolved required relationships are rejected.

## Import and Execution

Package uploads are preview-first. Compatible schema changes require explicit confirmation before provisioning. Mapping activation is revision-bound.

The offline fixture `hero_integration/tests/fixtures/hero_mapping_fixture_v1.json` supports deterministic tests and is not automatically imported.

Customer V2, including M-099, has passed isolated fixture and rollback tests. Calendar remains pending approval.

**No live HERO queries, automatic synchronization, or production data migration have been authorized or executed.**
