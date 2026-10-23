# DocType Extensions

**HERO Integration — four-domain Frappe/ERPNext v16 schema contract**  
**Updated:** 23 October 2026  
**Applies to:** Customer, Project, Calendar Events and external Tasks

The consolidated mapping batch `HERO_4_DOMAIN_REMAINING_MAPPINGS_BATCH_12` is accepted as completed (PASS). This document distinguishes source-controlled app DocTypes, the **nine** installer-managed Custom Fields, and the separately reviewed **35** additional Custom Field definitions in the four-domain mapping package. Completing a batch review does not override an explicit schema-adoption, service-approval or business-data execution gate.

## 1. Installation and schema ownership

HERO Integration requires Frappe v16 and ERPNext v16 on the selected site. `before_install` checks for an installed/importable ERPNext v16, the native `Customer`, `Contact`, `Contact Email`, `Contact Phone`, `Address` and `Project` DocTypes, and the editable native `Project.notes` contract. The application intentionally does not set `required_apps` to auto-install ERPNext.

`after_install` and `after_migrate` run `hero_integration.install.ensure_hero_source_fields()`. This uses `create_custom_fields(..., update=False)` for **nine installer-owned definitions**; compatible existing definitions are retained, and incompatible definitions fail explicitly. These hooks do **not** install the separate four-domain mapping package, its three additional DocTypes, or its 35 additional Custom Fields.

The following source-controlled HERO Integration DocTypes belong to the app's Frappe module and are not imported as Mapping Editor Custom DocTypes: `HERO Frappe Mapping`, `HERO GraphQL Schema Snapshot`, `HERO GraphQL Service`, `HERO Integration Link`, `HERO Integration Record`, `HERO Integration Run`, `HERO Integration Settings`, `HERO Integration Sync State`, and `HERO Mapping Adoption`.

### Installer-owned Custom Fields (9)

| Native DocType | Field | Type | Source/meaning |
| --- | --- | --- | --- |
| Customer | `custom_hero_source_id` | Data, unique, read-only | External Customer identity |
| Customer | `custom_hero_customer_number` | Data, read-only | `contacts[].nr` |
| Customer | `custom_hero_customer_category` | Data, read-only | `contacts[].category` (observation only) |
| Customer | `custom_hero_customer_type` | Data, read-only | `contacts[].type` (observation only) |
| Project | `custom_hero_source_id` | Data, unique, read-only | External Project Match identity |
| Project | `custom_hero_related_project_id` | Data, read-only | `project_matches[].project_id` (external ID) |
| Project | `custom_hero_display_id` | Data, read-only | `project_matches[].display_id` |
| Project | `custom_hero_source_created` | Data, read-only | External creation timestamp; not Frappe `creation` |
| Project | `custom_hero_source_modified` | Data, read-only | External modification timestamp; not Frappe `modified` |

These nine definitions are installed by the application's lifecycle hooks, without a manual Customize Form step.

## 2. Native record projections

### Customer and associated records

- `Customer.customer_name`, customer type and source identity are governed by protected Customer mapping purposes; external HERO company names do not create or select an ERPNext `Company`.
- Customer's approved primary contact is stored as a native `Contact` with its own `Contact Email`/`Contact Phone` child records and Customer Dynamic Link. First/last name, email and mobile source attributes flow through the existing primary-Contact adapter rather than arbitrary edits to an unrelated Contact.
- `phone_home` is added at most once to the primary Contact only if that Contact belongs exclusively to this Customer. Shared/ambiguous Contacts must not be silently modified or merged.
- Primary address is a native `Address` linked to the Customer by an approved relation. `address.street` populates `Address.address_line1` without splitting out a house number. The approved Customer V2 addition M-099 maps a nonblank `address.line_2` into `Address.address_line2`; `null`, empty or whitespace values retain an existing target value. Shared-address mutation is blocked.
- Country references must resolve to actual local Frappe/ERPNext values. Additional `contacts[]` and `customer_addresses[]` are not automatically projected without identity and lifecycle contracts.
- M-076 salutation is restricted to an approved explicit HERO source-value → existing `Salutation.name` table. No eligible source values have yet been documented and `title_custom` is not an automatic fallback.

### Project

- `Project.project_name` uses the validated native Project title contract. `Project.customer` is resolved through `HERO Integration Link`.
- `Project.company` is set only from the confirmed local ERPNext Company setting, never directly from a HERO Customer ID. `Project.notes` remains the native ERPNext field; optional external partner notes are held separately.
- Seven external project-match status attributes are stored under `custom_hero_observed_status_*` solely as observed source information. They must not drive `Project.status`, `docstatus`, tickets, state machines or workflow transitions.

### Calendar Event

- Native Event fields include `subject`, `description`, `starts_on`, `ends_on`, `all_day` and `location`. Original HERO start/end are stored in separate observational fields.
- A reviewed Project resolver may populate `Event.custom_hero_project`. Resources and partners use HERO-owned child tables; no automatic local Employee/User relations, invitations, reminders or outgoing calendar writes.
- The `is_recurring` source observation does not define an RRULE. Timezone and all-day behavior must pass the separate Calendar approval before any Calendar service execution. **Calendar remains `Pending Approval`.**

### External Task

- `HERO External Task` is a controlled external-data projection, **not** a local Frappe/ERPNext `Task` or executable workflow step.
- The external `target_user_id` remains observational; it is not a User/Employee assignment. A reviewed Project relation requires canonical external-to-local resolution. Completion and deletion attributes remain observations, not transitions or deletions.

## 3. Additional four-domain schema package

The reviewed four-domain package `hero-frappe-mapping-package-v2` declares **3 additional HERO Custom DocTypes, 35 native Custom Fields, 4 explicit relations and 75 original mapping definitions**. M-099 was approved as a subsequent specialized Customer V2 binding, resulting in **76 unique active mapping definitions** in the last reported isolated-site configuration.

These are **package/schema-adoption definitions**, not Frappe installation hooks. The 35 additions are separate from the nine installer-owned fields (there are 44 distinct field definitions across the two reviewed sets, if the full optional package is adopted without conflicts). Re-running the installer is not a mechanism to add the 35 package fields.

### HERO-owned Custom DocTypes (3)

| DocType | Kind | Fields | Contract |
| --- | --- | ---: | --- |
| `HERO External Task` | Standalone Custom DocType | 17 | External task observation with source identity and optional reviewed Project relation |
| `HERO Event Resource` | Child DocType (`istable=1`) | 4 | External resource ID, name, type and type ID |
| `HERO Event Partner` | Child DocType (`istable=1`) | 2 | External partner ID and name |

`HERO External Task` declares `custom_hero_author_user_id`, `custom_hero_company_id`, `custom_hero_target_user_id`, `custom_hero_title`, `custom_hero_comment`, `custom_hero_target_project_match_id`, `custom_hero_due_date`, `custom_hero_done_date`, `custom_hero_created`, `custom_hero_modified`, `custom_hero_start`, `custom_hero_end`, `custom_hero_is_deleted`, `custom_hero_source_id`, `custom_hero_key`, `custom_hero_source_instance` and `custom_hero_project` (Link → Project).

`HERO Event Resource` declares `custom_hero_source_id`, `custom_hero_resource_name`, `custom_hero_resource_type` and `custom_hero_type_id`.

`HERO Event Partner` declares `custom_hero_source_id` and `custom_hero_partner_name`.

### Additional Custom Fields on native DocTypes (35)

These entries reflect the reviewed **package definitions**. Frappe metadata on the target site is the authority for whether a particular definition was newly created or already existed.

#### `Customer` — 1 package field

| Field | Type | Link/Table target |
| --- | --- | --- |
| `custom_hero_is_contact_person` | `Check` | — |

#### `Project` — 13 package fields

| Field | Type | Link/Table target |
| --- | --- | --- |
| `custom_hero_observed_status_id` | `Data` | — |
| `custom_hero_observed_status_status_code` | `Data` | — |
| `custom_hero_observed_status_name` | `Small Text` | — |
| `custom_hero_observed_status_short_name` | `Small Text` | — |
| `custom_hero_observed_status_created` | `Small Text` | — |
| `custom_hero_observed_status_modified` | `Small Text` | — |
| `custom_hero_observed_status_step_id` | `Data` | — |
| `custom_hero_project_number` | `Small Text` | — |
| `custom_hero_project_type` | `Small Text` | — |
| `custom_hero_partner_notes` | `Small Text` | — |
| `custom_hero_customer_title` | `Small Text` | — |
| `custom_hero_customer_full_name` | `Small Text` | — |
| `custom_hero_address_full_address` | `Small Text` | — |

#### `Event` — 21 package fields

| Field | Type | Link/Table target |
| --- | --- | --- |
| `custom_hero_category_id` | `Data` | — |
| `custom_hero_project_match_id` | `Data` | — |
| `custom_hero_deleted` | `Data` | — |
| `custom_hero_color` | `Small Text` | — |
| `custom_hero_is_done` | `Data` | — |
| `custom_hero_is_recurring` | `Data` | — |
| `custom_hero_provider` | `Small Text` | — |
| `custom_hero_source_id` | `Data` | — |
| `custom_hero_modified` | `Data` | — |
| `custom_hero_created` | `Data` | — |
| `custom_hero_type` | `Small Text` | — |
| `custom_hero_localized_type` | `Small Text` | — |
| `custom_hero_partner_id` | `Data` | — |
| `custom_hero_readonly` | `Data` | — |
| `custom_hero_resources` | `Table` | `HERO Event Resource` |
| `custom_hero_partners` | `Table` | `HERO Event Partner` |
| `custom_hero_key` | `Data` | — (unique) |
| `custom_hero_source_instance` | `Data` | — |
| `custom_hero_project` | `Link` | `Project` |
| `custom_hero_original_start` | `Data` | — |
| `custom_hero_original_end` | `Data` | — |

### Explicit relations (4)

| Owner | Field | Kind | Target |
| --- | --- | --- | --- |
| `HERO External Task` | `custom_hero_project` | Link | `Project` |
| `Event` | `custom_hero_project` | Link | `Project` |
| `Event` | `custom_hero_resources` | Table | `HERO Event Resource` |
| `Event` | `custom_hero_partners` | Table | `HERO Event Partner` |

Two Event child-table fields are among the **21 Event Custom Fields** above, not additional fields to add again. Native `Contact.links` and `Address.links` use the standard Dynamic Link ownership contract; they are not extra HERO-created tables.

The package-defined observational flags are intentionally `Data` fields where required to preserve source `null` / `false` / `0` distinctions. Changing them to `Check` would be a separate data/schema migration and is not implicit in the batch.

## 4. Additional Customer contract candidates

Batch 12's completed evaluation retains the following exact boundary decisions:

| ID | Candidate or condition | Schema consequence |
| --- | --- | --- |
| M-076 | `title` / `title_custom` → `Contact.salutation` | No speculative Link or Salutation creation; controlled source value table required |
| M-082 | No `Address.house_number` | Do not split/derive address data or create a speculative number field |
| M-086 | `company_legal_form` → `Customer.custom_hero_company_legal_form` | **Approved business contract**: independent raw `Data`, read-only; requires separate new Custom Field adoption and mapping activation if not already present in site metadata; **not one of the 35 package fields** |
| M-087/M-088/M-093/M-094 | Additional Contact/Address, salutation and designation | No extra relations without explicit per-person identity, ownership, dedup and update policy |
| M-089 | Billing recipient | Protected policy boundary; source flag alone is insufficient |
| M-090/M-091/M-096/M-097 | Contact origin, reachability, fax, website | Require explicit semantics, target and update/retention rules before native writes |
| M-098 | Birth date | Intentionally excluded pending privacy/retention authorization |
| M-100/M-101 | External timestamps | Do not overwrite native Frappe `creation` / `modified` |
| M-102/M-103 | Task completion/assignee | Observational only; no local workflow/assignment projection |
| M-104/M-106/M-107 | Calendar recurrence/resources/partners | No generated recurrence, Employee/User bindings or invitations |

No source field's mere presence in GraphQL authorizes a new target field, relation or workflow effect.

## 5. Canonical identity, metadata and adoption

**`HERO Integration Link`** is the canonical external identity/provenance store. It carries source system, optional source instance, source type, external ID and unique source key, target DocType/name, payload hashes, mapping revision, sync status, conflict detail and last sync timestamp. Display-only `custom_hero_source_id` fields on native records do not replace this link.

**`HERO GraphQL Schema Snapshot`** stores the reviewed schema with an immutable fingerprint; **`HERO GraphQL Service`** stores saved read-only selections and revisions. **`HERO Frappe Mapping`** binds selected fields to approved Custom Fields or protected native purposes. A source field visible in the schema but absent from an approved query is not a permitted mapping input.

**`HERO Mapping Adoption`** records a reviewed import package, selected definitions, result and error details. The Mapping Editor performs upload and preview without schema writes. An authorized System Manager selects and confirms the necessary HERO-owned Custom DocTypes, `custom_hero_` fields, Link/Table relations and mapping definitions in one checklist; provisioning precedes mapping activation. Unknown transformations, conflicts with existing/foreign fields, unresolved Link targets or invalid revisions fail closed.

This adopted-schema path is **create-only and conflict-safe** for owned definitions. It never deletes or rewrites unrelated native/foreign structures. Frappe DDL cannot be assumed fully transactional: report materialized schema after a partial failure and require a new preview before retry. No package import, approval or `bench migrate` automatically performs a HERO business-data run.

The deterministic offline fixture [`hero_mapping_fixture_v1.json`](../hero_integration/tests/fixtures/hero_mapping_fixture_v1.json) is test-only, is not exported as a Frappe fixture, and is never imported during installation. The specific Customer V2 M-099 fixture test additionally verifies owned-Address persistence, null preservation, idempotence and rollback.

## 6. Acceptance and release parity

- Verify Frappe and ERPNext v16, correct installation site, approved schema fingerprint, code revision and app-owned metadata before attempting adoption.
- Check each new schema definition against `frappe.get_meta()`, field type, flags, link/table targets, permissions and module registration. Re-preview unchanged definitions after adoption.
- Regress Customer V1/V2 including M-099, Project/Company resolution, Event native fields and child tables, external Task projection, source identity, duplicates and rollback.
- Preserve Calendar `Pending Approval` until its explicit timezone/all-day and runtime gates are complete; keep scheduler/polling disabled. No live HERO requests or production migration are conferred by this document.
- The isolated-site four-domain configuration and the **GitHub `version-16` source checkout may differ**. Do not declare this document a released implementation until source, schema, runtime tests and the target Git revision are reconciled. In particular, the GitHub installer still contains only the nine baseline fields; the three new Custom DocTypes and 35 Custom Fields depend on the separate schema-adoption flow.

[Mapping matrix](MAPPING_MATRIX.md) · [Installation](../INSTALLATION.md)
