# DocType Extensions

**HERO Integration — four-domain Frappe/ERPNext v16 schema contract**  
**Original four-domain package baseline:** 23 October 2026  
**Applies to:** Customer, Project, Calendar Events and external Tasks

> **Current source note (2 November 2026):** The installer-owned nine fields and additional 35-field package below are a historical baseline, **not** the complete current source-controlled DocType inventory. Verify installed metadata against the exact commit. Later native Task and assignment policy/state DocTypes are separate from the legacy external Task package.

> **Native fields and labels (3 November 2026):** HERO values that duplicate a native ERPNext field are written to that field. HERO `contacts[].type` (`private`/`commercial`) sets the native Customer Type via the `Customer Type From HERO Type` transformation. `project_matches[].partner_notes` is stored in the native Project Notes via `Plain Text To HTML`, and Send to HERO returns it as plain text. Calendar `color` is written to Event Color, and `is_done` sets the native Event status (`Completed`/`Open`). The superseded fields `custom_hero_customer_type`, `custom_hero_is_contact_person`, `custom_hero_partner_notes`, `custom_hero_project_type_copy`, `custom_hero_color` and `custom_hero_is_done` are hidden, not deleted: older mapping revisions still declare them, and their columns keep the old values. The installer no longer creates `custom_hero_customer_type`. Labels of app fields carry no "HERO" prefix. The origin is recorded only as metadata: the `custom_hero_` fieldname and the Custom Field module `HERO Integration`. Labels are display only; provisioning and installation do not treat a different label as schema drift. Technical sync fields (Event keys and IDs, Task `custom_hero_due_date`, which matches the native Expected End Date) are hidden on the form. Patch `v0_3.native_field_moves` revises the active read mappings through the draft/publish path. The Customer, Project and Calendar read services then need a new approval.

This document separates three schema sets: source-controlled app DocTypes, the **nine** installer-managed Custom Fields and the **35** additional Custom Field definitions in the four-domain mapping package. A completed review does not override an explicit schema-adoption, service-approval or business-data execution gate.

## 1. Installation and schema ownership

HERO Integration requires Frappe v16 and ERPNext v16 on the selected site. `before_install` checks for an installed/importable ERPNext v16. It also checks the native `Customer`, `Contact`, `Contact Email`, `Contact Phone`, `Address` and `Project` DocTypes and the editable native `Project.notes` contract. The app does not set `required_apps`, so it never auto-installs ERPNext.

`after_install` and `after_migrate` run `hero_integration.install.ensure_hero_source_fields()`. This function calls `create_custom_fields(..., update=False)` for **nine installer-owned definitions**. It keeps compatible existing definitions and fails explicitly on incompatible ones. These hooks do **not** install the separate four-domain mapping package, its three additional DocTypes or its 35 additional Custom Fields.

The following source-controlled HERO Integration DocTypes belong to the app's Frappe module. The Mapping Editor does not import them as Custom DocTypes: `HERO Frappe Mapping`, `HERO GraphQL Schema Snapshot`, `HERO GraphQL Service`, `HERO Integration Link`, `HERO Integration Record`, `HERO Integration Run`, `HERO Integration Settings`, `HERO Integration Sync State`, and `HERO Mapping Adoption`.

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

The app's lifecycle hooks install these nine definitions. No manual Customize Form step is needed.

## 2. Native record projections

### Customer and associated records

- Protected Customer mapping purposes govern `Customer.customer_name`, customer type and source identity. External HERO company names never create or select an ERPNext `Company`.
- The approved primary contact is a native `Contact` with its own `Contact Email`/`Contact Phone` child records and a Customer Dynamic Link. First/last name, email and mobile flow through the existing primary-Contact adapter. The adapter never edits an unrelated Contact.
- `phone_home` is added at most once to the primary Contact. This happens only if that Contact belongs exclusively to this Customer. Shared or ambiguous Contacts must not be silently changed or merged.
- The primary address is a native `Address` linked to the Customer by an approved relation. `address.street` fills `Address.address_line1` without splitting out a house number. The approved Customer V2 addition M-099 maps a nonblank `address.line_2` into `Address.address_line2`. `null`, empty or whitespace values keep an existing target value. Changes to shared addresses are blocked.
- Country references must resolve to real local Frappe/ERPNext values. Additional `contacts[]` and `customer_addresses[]` are not projected without identity and lifecycle contracts.
- M-076 salutation uses only an approved explicit table of HERO source values → existing `Salutation.name`. The binding stays inactive until eligible source values are documented. `title_custom` is not an automatic fallback.

### Project

- `Project.project_name` uses the validated native Project title contract. `Project.customer` resolves through `HERO Integration Link`.
- `Project.company` comes only from the ERPNext Default Company, never directly from a HERO Customer ID. `Project.notes` stays the native ERPNext field. Optional external partner notes are stored separately.
- Seven external project-match status attributes are stored under `custom_hero_observed_status_*` as observed source information only. They must not drive `Project.status`, `docstatus`, tickets, state machines or workflow transitions.

### Calendar Event

- Native Event fields include `subject`, `description`, `starts_on`, `ends_on`, `all_day` and `location`. Separate observational fields keep the original HERO start and end.
- A reviewed Project resolver may fill `Event.custom_hero_project`. Resources and partners use HERO-owned child tables. The app creates no local Employee/User relations, invitations, reminders or outgoing calendar writes.
- The `is_recurring` source observation does not define an RRULE. Timezone and all-day behavior must pass the separate Calendar approval before any Calendar service run. **Calendar stays `Pending Approval`.**

### External Task

- `HERO External Task` is a controlled external-data projection, **not** a local Frappe/ERPNext `Task` or executable workflow step.
- In the legacy external Task projection, `target_user_id` is observational, not a native assignment. Its Project link must be verified; completion and deletion remain observations.

### Native Task intake — separate source path

The current source also supports guarded native ERPNext `Task` creation and updates, using a confirmed source instance/Task identity, an approved read/mapping contract and the existing Integration Link. A subject is required; optional fields follow selected rules and conflict baselines. Task lifecycle and ToDo handling have further policy, Frappe-patch and runtime requirements. The legacy `HERO External Task` mapping does not activate native Task behavior.

## 3. Additional four-domain schema package

The reviewed four-domain package `hero-frappe-mapping-package-v2` declares **3 additional HERO Custom DocTypes, 35 native Custom Fields, 4 explicit relations and 75 original mapping definitions**. M-099 adds a specialized Customer V2 binding with its own approval. Together they give **76 unique active mapping definitions**.

These are **package/schema-adoption definitions**, not Frappe installation hooks. The 35 package fields are separate from the nine installer-owned fields. If the full optional package is adopted without conflicts, both sets hold 44 distinct field definitions. Re-running the installer does not add the 35 package fields.

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

These entries reflect the reviewed **package definitions**. Frappe metadata on the target site decides whether a definition was newly created or already existed.

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

The two Event child-table fields belong to the **21 Event Custom Fields** above. Do not add them again. Native `Contact.links` and `Address.links` use the standard Dynamic Link ownership contract. They are not extra HERO-created tables.

The package defines observational flags as `Data` fields where needed to keep the source distinction between `null`, `false` and `0`. Changing them to `Check` requires a separate data/schema migration.

## 4. Additional Customer contract candidates

The review sets the following boundary decisions:

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

A source field's presence in GraphQL alone never authorizes a new target field, relation or workflow effect.

## 5. Canonical identity, metadata and adoption

**`HERO Integration Link`** is the canonical store for external identity and provenance. It holds source system, optional source instance, source type, external ID and unique source key. It also holds target DocType/name, payload hashes, mapping revision, sync status, conflict detail and last sync timestamp. Display-only `custom_hero_source_id` fields on native records do not replace this link.

**`HERO GraphQL Schema Snapshot`** stores the reviewed schema with an immutable fingerprint. **`HERO GraphQL Service`** stores saved read-only selections and revisions. **`HERO Frappe Mapping`** binds selected fields to approved Custom Fields or protected native purposes. A source field that appears in the schema but not in an approved query is not a permitted mapping input.

**`HERO Mapping Adoption`** records a reviewed import package, selected definitions, result and error details. The Mapping Editor runs upload and preview without schema writes. An authorized System Manager selects and confirms the needed HERO-owned Custom DocTypes, `custom_hero_` fields, Link/Table relations and mapping definitions in one checklist. Provisioning runs before mapping activation. Unknown transformations, conflicts with existing/foreign fields, unresolved Link targets or invalid revisions fail closed.

This adoption path is **create-only and conflict-safe** for owned definitions. It never deletes or rewrites unrelated native/foreign structures. Frappe DDL is not fully transactional. After a partial failure, report the created schema and require a new preview before retry. No package import, approval or `bench migrate` starts a HERO business-data run.

The deterministic offline fixture [`hero_mapping_fixture_v1.json`](../hero_integration/tests/fixtures/hero_mapping_fixture_v1.json) is test-only. It is not exported as a Frappe fixture and is never imported during installation. The Customer V2 M-099 fixture test also verifies owned-Address persistence, null preservation, idempotence and rollback.

## 6. Acceptance and release parity

- Before adoption, verify Frappe and ERPNext v16, the installation site, the approved schema fingerprint, the code revision and app-owned metadata.
- Check each new schema definition against `frappe.get_meta()`: field type, flags, link/table targets, permissions and module registration. After adoption, preview unchanged definitions again.
- Run regression tests for Customer V1/V2 including M-099, Project/Company resolution, Event native fields and child tables, external Task projection, source identity, duplicates and rollback.
- Keep Calendar at `Pending Approval` until its explicit timezone/all-day and runtime gates are complete. Keep scheduler/polling disabled. This document authorizes no live HERO requests and no production migration.
- An adopted four-domain site configuration and a checkout of `hero_int_1.0` or `hero_int_dev` can differ. Reconcile the exact Git commit, installed DocTypes, schema, active read service/mapping revisions and target tests before asserting release parity. The three additional Custom DocTypes and 35 Custom Fields still depend on their guarded schema-adoption flow.

[Mapping matrix](mapping_matrix_1.0.md) · [Installation](../installation_guide_1.0.md)
