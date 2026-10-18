# DocType extensions

HERO Integration requires a site where Frappe v16 and ERPNext v16 are already
installed. `before_install` checks ERPNext before the app's schema is synced.
It does not declare `required_apps`, so a failed install cannot trigger an
ERPNext installation.

## Native ERPNext storage

Customer names remain native Customer values. Customer first and last name,
email, and mobile number use the ERPNext primary Contact lifecycle. `email`
creates a Contact Email and `phone_mobile` creates a mobile Contact Phone.
`phone_home` is appended once as an additional Contact Phone only when the
primary Contact is linked exclusively to that Customer. A shared Contact is
reported as a conflict and is not changed.

Project title, customer, and company use native Project fields. The Customer
relation is resolved only through a canonical HERO Integration Link. Company
comes only from the confirmed HERO Integration Settings value. No selected
HERO field supplies an Address, so this app creates no Address data.

## App-owned Custom Fields

The only authoritative definition path is `hero_integration.install` through
`after_install` and `after_migrate`. It uses `create_custom_fields(...,
update=False)`. An existing compatible field is retained unchanged; a field
with the same target and fieldname but a different definition stops migration.
No fixture writes the same fields.

| DocType | Field | Source path | Reason |
| --- | --- | --- | --- |
| Customer | `custom_hero_source_id` | `contacts[].id` | Stable external identity. |
| Customer | `custom_hero_customer_number` | `contacts[].nr` | External display number; no native equivalent. |
| Customer | `custom_hero_customer_category` | `contacts[].category` | Raw source attribute; never maps to Customer Group. |
| Customer | `custom_hero_customer_type` | `contacts[].type` | Raw source attribute; never maps to Customer Type. |
| Project | `custom_hero_source_id` | `project_matches[].id` | Stable external identity. |
| Project | `custom_hero_related_project_id` | `project_matches[].project_id` | Related HERO ID; no Project Link is inferred. |
| Project | `custom_hero_display_id` | `project_matches[].display_id` | Source display ID; no generated Project name is replaced. |
| Project | `custom_hero_source_created` | `project_matches[].created` | Raw source timestamp; never writes Frappe `creation`. |
| Project | `custom_hero_source_modified` | `project_matches[].modified` | Raw source timestamp; never writes Frappe `modified`. |

## Canonical identity and provenance

**HERO Integration Link** is the only identity table. It records source system,
optional source instance, HERO entity type, external ID, unique source key,
ERPNext target DocType and ID, source and target payload hashes, mapping
version, sync status, conflict detail, and last sync time. The Custom Fields
make identity visible on native records; they do not replace that link.

The test-only mapping fixture at
[`hero_mapping_fixture_v1.json`](hero_integration/tests/fixtures/hero_mapping_fixture_v1.json)
accounts for all 19 selected paths. It is not installed or activated by the
app.

## Direct adapter configuration

**HERO GraphQL Schema Snapshot** stores a reviewed introspection JSON document,
its content fingerprint, and graph-analysis result. **HERO GraphQL Service**
stores a read-only GraphQL document, its revision, and the fingerprint it was
validated against. **HERO Frappe Mapping** maps one selected source path to one
writable Custom Field or a protected, explicit native binding. Activation
checks source type, snapshot, Frappe metadata, target relation, and current
user write permission. No default mapping is installed.

The **HERO Adapter Tools** page is the normal mapping-entry path. Its source
picker uses only saved schema snapshots and the two versioned read operations;
it shows query-root paths, GraphQL type, nullability, and object relation. A
schema field that is not in an operation selection set remains visible only as
disabled context. Its target picker calls `frappe.get_meta()` for the approved
Customer or Project target, including Custom Fields. It excludes technical,
read-only, protected, standard-mapping, standard-field, and unresolved
child-DocType fields. Site-specific mappings can only target writable Custom
Fields; a new native mapping remains an app-review decision.
Suggestions are ranked by compatible type plus name/label overlap and remain
Draft until an administrator explicitly activates the mapping.

## Imported HERO-owned schema

A reviewed mapping package can declare a missing target structure. The package
format permits only data-only definitions for `HERO ` Custom DocTypes,
`custom_hero_` Custom Fields, and checked Link or Table relations. It cannot
contain scripts, permissions, fixtures, SQL, or executable hooks.

The Mapping Editor makes no schema change when a user selects a field or
uploads a package. It presents one checklist. The System Manager confirms one
selection. The app creates selected Custom DocTypes first, then Custom Fields,
checks selected relations, and only then activates selected mappings. It never
modifies or deletes a native or foreign structure. Frappe DDL may not support a
full rollback, so an adoption result reports created schema explicitly. A
mapping failure does not leave an active mapping plan behind. **HERO Mapping
Adoption** stores the package, selection, outcome, and error for a safe retry.
