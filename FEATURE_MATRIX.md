# Mapping Matrix

**HERO Integration — four-domain mapping contract (Frappe/ERPNext v16)**  
**Updated:** 23 October 2026  
**Scope:** Customer, Project Match, Calendar Events and Tasks

This document specifies the reviewed source-to-target contracts, approved native projections, and deliberately excluded mappings. The consolidated batch task `HERO_4_DOMAIN_REMAINING_MAPPINGS_BATCH_12` is treated as **completed (PASS)** for the purposes of this documentation. Completion of the batch review or preparation of guarded code does **not** imply that blocked semantics have been approved or that unactivated mapping definitions are active.

## Current four-domain baseline

| Read operation | Selected active definitions | Target | Service approval |
| --- | ---: | --- | --- |
| `HeroCustomers` (V2) | 15 | `Customer`, native `Contact` / `Address` projections | Approved |
| `HeroProjectMatches` | 21 | `Project` | Approved |
| `HeroCalendarEvents` | 25 | `Event`, two HERO child tables | **Pending Approval** |
| `HeroTasks` | 15 | `HERO External Task` | Approved |
| **Unique active mapping definitions** | **76** | | |

Customer V1 remains separately approved and selects 14 of the same 15 Customer definitions; V2 adds M-099. V1 and V2 are alternatives selected explicitly for a run, **not** two automatic passes. Calendar mapping definitions can exist as Active while the Calendar *service* remains `Pending Approval`; this is not an authorization to execute it. Scheduler and polling remain off.

The reviewed isolated-site schema snapshot is `2b55ter0bc`, normalized fingerprint `fee37bfa55a02f7a4d09b6b902e6600a835c992c8fe6644c358f5e511e025201`. This fingerprint is distinct from the original `hero_connector` export digest and historical fingerprints; do not interchange them.

The 76 rows below represent the 75-definition four-domain import contract plus the separately approved M-099 Customer V2 native binding. IDs are mapping `definition_id` values except **M-099**, which is an audit identifier for the added Address binding. See [DocType extensions](DOCTYPE_EXTENSIONS.md) for schema details.

## Mapping definitions

Source paths in each table are relative to the operation's collection item, respectively `contacts[]`, `project_matches[]`, `calendar_events[]`, and `tasks[]`. “Declared target” reproduces the reviewed mapping contract; protected native purposes can persist through an adapter rather than a direct field assignment.

### `HeroCustomers` — Customer

| Definition ID | Source path | Declared target | Mapping purpose |
| --- | --- | --- | --- |
| `customer-source-id` | `id` | `Customer.custom_hero_source_id` | Source Identity |
| `customer-company-name` | `company_name` | `Customer.customer_name` | Record Name |
| `customer-full-name` | `full_name` | `Customer.customer_name` | Record Name |
| `customer-type` | `company_name` | `Customer.customer_type` | Customer Type |
| `customer-number` | `nr` | `Customer.custom_hero_customer_number` | Field |
| `customer-category` | `category` | `Customer.custom_hero_customer_category` | Field |
| `customer-source-type` | `type` | `Customer.custom_hero_customer_type` | Field |
| `customer-first-name` | `first_name` | `Customer.first_name` | Customer Primary Contact |
| `customer-last-name` | `last_name` | `Customer.last_name` | Customer Primary Contact |
| `customer-email` | `email` | `Customer.email_id` | Customer Primary Contact |
| `customer-mobile` | `phone_mobile` | `Customer.mobile_no` | Customer Primary Contact |
| `customer-home-phone` | `phone_home` | `Contact Phone.phone` | Customer Home Phone |
| `customer-deleted` | `is_deleted` | `HERO Integration Link.sync_status` | Lifecycle Flag |
| `customer-contact-person` | `is_contact_person` | `Customer.custom_hero_is_contact_person` | Field |
| `M-099` (Customer V2) | `address.line_2` | `Address.address_line2` | Protected native primary-Address binding |

**Contract boundary:** The Customer identity and record name use protected purposes. First name, last name, email and phones use the primary-Contact adapter; the declared Customer/contact convenience fields are not permission to mutate arbitrary Contacts. `phone_home` is appended only for an exclusively linked Contact. Primary Address uses an owned native Address relation, retains existing data for missing or blank `line_2`, and rejects shared-address updates. M-099 is handled by that adapter, not an unrestricted generic resolver.

### `HeroProjectMatches` — Project

| Definition ID | Source path | Declared target | Mapping purpose |
| --- | --- | --- | --- |
| `project-source-id` | `id` | `Project.custom_hero_source_id` | Source Identity |
| `project-name` | `project_title` | `Project.project_name` | Record Name |
| `project-customer` | `customer_id` | `Project.customer` | Customer Relation |
| `project-company` | `customer_id` | `Project.company` | Confirmed Company |
| `project-related-id` | `project_id` | `Project.custom_hero_related_project_id` | Field |
| `project-display-id` | `display_id` | `Project.custom_hero_display_id` | Field |
| `project-created` | `created` | `Project.custom_hero_source_created` | Field |
| `project-modified` | `modified` | `Project.custom_hero_source_modified` | Field |
| `project-number` | `project_nr` | `Project.custom_hero_project_number` | Field |
| `project-source-type` | `project_type` | `Project.custom_hero_project_type` | Field |
| `project-partner-notes` | `partner_notes` | `Project.custom_hero_partner_notes` | Field |
| `project-customer-title` | `customer.title` | `Project.custom_hero_customer_title` | Field |
| `project-customer-full-name` | `customer.full_name` | `Project.custom_hero_customer_full_name` | Field |
| `project-address-full-address` | `address.full_address` | `Project.custom_hero_address_full_address` | Field |
| `HeroProjectMatches:custom_hero_observed_status_id` | `current_project_match_status.id` | `Project.custom_hero_observed_status_id` | Field |
| `HeroProjectMatches:custom_hero_observed_status_status_code` | `current_project_match_status.status_code` | `Project.custom_hero_observed_status_status_code` | Field |
| `HeroProjectMatches:custom_hero_observed_status_name` | `current_project_match_status.name` | `Project.custom_hero_observed_status_name` | Field |
| `HeroProjectMatches:custom_hero_observed_status_short_name` | `current_project_match_status.short_name` | `Project.custom_hero_observed_status_short_name` | Field |
| `HeroProjectMatches:custom_hero_observed_status_created` | `current_project_match_status.created` | `Project.custom_hero_observed_status_created` | Field |
| `HeroProjectMatches:custom_hero_observed_status_modified` | `current_project_match_status.modified` | `Project.custom_hero_observed_status_modified` | Field |
| `HeroProjectMatches:custom_hero_observed_status_step_id` | `current_project_match_status.step_id` | `Project.custom_hero_observed_status_step_id` | Field |

**Contract boundary:** The Customer relation resolves through `HERO Integration Link`; `Project.company` is obtained only from a separately confirmed local ERPNext Company setting, **not** the HERO `customer_id`. Project title uses the validated native naming contract. All seven `custom_hero_observed_status_*` fields are observational and never mutate native project status, `docstatus`, state machines or workflows.

### `HeroCalendarEvents` — Event

| Definition ID | Source path | Declared target | Mapping purpose |
| --- | --- | --- | --- |
| `HeroCalendarEvents:custom_hero_category_id` | `category_id` | `Event.custom_hero_category_id` | Field |
| `HeroCalendarEvents:custom_hero_project_match_id` | `project_match_id` | `Event.custom_hero_project_match_id` | Field |
| `HeroCalendarEvents:subject` | `title` | `Event.subject` | Native Binding |
| `HeroCalendarEvents:description` | `description` | `Event.description` | Native Binding |
| `HeroCalendarEvents:starts_on` | `start` | `Event.starts_on` | Native Binding |
| `HeroCalendarEvents:ends_on` | `end` | `Event.ends_on` | Native Binding |
| `HeroCalendarEvents:all_day` | `all_day` | `Event.all_day` | Native Binding |
| `HeroCalendarEvents:custom_hero_deleted` | `deleted` | `Event.custom_hero_deleted` | Field |
| `HeroCalendarEvents:custom_hero_color` | `color` | `Event.custom_hero_color` | Field |
| `HeroCalendarEvents:custom_hero_is_done` | `is_done` | `Event.custom_hero_is_done` | Field |
| `HeroCalendarEvents:custom_hero_is_recurring` | `is_recurring` | `Event.custom_hero_is_recurring` | Field |
| `HeroCalendarEvents:custom_hero_provider` | `provider` | `Event.custom_hero_provider` | Field |
| `HeroCalendarEvents:custom_hero_source_id` | `id` | `Event.custom_hero_source_id` | Source Identity |
| `HeroCalendarEvents:custom_hero_modified` | `modified` | `Event.custom_hero_modified` | Field |
| `HeroCalendarEvents:custom_hero_created` | `created` | `Event.custom_hero_created` | Field |
| `HeroCalendarEvents:custom_hero_type` | `type` | `Event.custom_hero_type` | Field |
| `HeroCalendarEvents:custom_hero_localized_type` | `localized_type` | `Event.custom_hero_localized_type` | Field |
| `HeroCalendarEvents:location` | `full_address` | `Event.location` | Native Binding |
| `HeroCalendarEvents:custom_hero_partner_id` | `partner_id` | `Event.custom_hero_partner_id` | Field |
| `HeroCalendarEvents:custom_hero_readonly` | `readonly` | `Event.custom_hero_readonly` | Field |
| `HeroCalendarEvents:custom_hero_resources` | `resources` | `Event.custom_hero_resources` | Native Binding |
| `HeroCalendarEvents:custom_hero_partners` | `partners` | `Event.custom_hero_partners` | Native Binding |
| `HeroCalendarEvents:custom_hero_project` | `project_match_id` | `Event.custom_hero_project` | Project Relation |
| `HeroCalendarEvents:custom_hero_original_start` | `start` | `Event.custom_hero_original_start` | Native Binding |
| `HeroCalendarEvents:custom_hero_original_end` | `end` | `Event.custom_hero_original_end` | Native Binding |

**Contract boundary:** Native event properties include subject, description, starts/ends, all-day and location; original source times are preserved separately. Resource and partner collections are stored as HERO child observations, not `Employee`/`User` links or invitations. The recurring flag is not a recurrence rule. No Calendar run until timezone/all-day semantics and service acceptance are approved.

### `HeroTasks` — HERO External Task

| Definition ID | Source path | Declared target | Mapping purpose |
| --- | --- | --- | --- |
| `HeroTasks:custom_hero_author_user_id` | `author_user_id` | `HERO External Task.custom_hero_author_user_id` | Field |
| `HeroTasks:custom_hero_company_id` | `company_id` | `HERO External Task.custom_hero_company_id` | Field |
| `HeroTasks:custom_hero_target_user_id` | `target_user_id` | `HERO External Task.custom_hero_target_user_id` | Field |
| `HeroTasks:custom_hero_title` | `title` | `HERO External Task.custom_hero_title` | Field |
| `HeroTasks:custom_hero_comment` | `comment` | `HERO External Task.custom_hero_comment` | Field |
| `HeroTasks:custom_hero_target_project_match_id` | `target_project_match_id` | `HERO External Task.custom_hero_target_project_match_id` | Field |
| `HeroTasks:custom_hero_due_date` | `due_date` | `HERO External Task.custom_hero_due_date` | Field |
| `HeroTasks:custom_hero_done_date` | `done_date` | `HERO External Task.custom_hero_done_date` | Field |
| `HeroTasks:custom_hero_created` | `created` | `HERO External Task.custom_hero_created` | Field |
| `HeroTasks:custom_hero_modified` | `modified` | `HERO External Task.custom_hero_modified` | Field |
| `HeroTasks:custom_hero_start` | `start` | `HERO External Task.custom_hero_start` | Field |
| `HeroTasks:custom_hero_end` | `end` | `HERO External Task.custom_hero_end` | Field |
| `HeroTasks:custom_hero_is_deleted` | `is_deleted` | `HERO External Task.custom_hero_is_deleted` | Field |
| `HeroTasks:custom_hero_source_id` | `id` | `HERO External Task.custom_hero_source_id` | Source Identity |
| `HeroTasks:custom_hero_project` | `target_project_match_id` | `HERO External Task.custom_hero_project` | Project Relation |

**Contract boundary:** External tasks remain in `HERO External Task`; `target_user_id` stays an external identifier. Project references require canonical resolution. No local ERPNext/Frappe Task creation, assignment, workflow transition, or derived completion state.

## Closed Customer V2 change

M-099 selects `contacts[].address.line_2` and persists a nonblank, normalized value into `Address.address_line2` using the existing, identity-safe Customer Address adapter. The generic `MappingPlan.values()` does not resolve that specialized native binding independently. The previously failing complete Customer V2 synthetic fixture passed after the focused handoff fix, together with null/blank preservation, shared-Address checks, idempotence, stale-revision guards and rollback.

Customer V2 binding `HERO-MAP-00087` rev. 2 is Active; mapping `HERO-GQL-MAP-00088` rev. 1 is Active; service `HERO-GQL-00089` rev. 1 is Approved. Customer V1 service `HERO-GQL-00080` and mapping `HERO-GQL-MAP-00079` retain their approved/active revision 1. Their execution remains an explicit choice.

## Consolidated residual contract decisions

The field-coverage audit contains **108 review positions**, which are not the same as active mapping-definition count. Its Delta-02 baseline had 88 positions recorded as implemented and 20 residual positions. Batch 12 is accepted as completed, with the following protection and business-contract outcomes. A documented or prepared candidate is not an activated mapping.

| Audit ID | Scope | Decision / retained gate |
| --- | --- | --- |
| M-076 | Customer salutation | Controlled exact-value `Customer.title` / `title_custom` → existing `Contact.salutation` bindings only. Source-value inventory yielded **zero verified Customer values**, so no binding is activated. Person/company ownership still requires proof. |
| M-082 | House number | **Deferred:** GraphQL `Address.house_number` is absent; retain `street` as `Address.address_line1` unchanged. No heuristic splitting. |
| M-086 | Company legal form | **Business contract approved:** raw `Customer.company_legal_form` → proposed read-only `Customer.custom_hero_company_legal_form` (`Data`), independent of ERPNext Company, Customer Type and Customer Group. New schema adoption or mapping activation is separately gated. |
| M-087 | Extra-contact salutation | Requires M-076 vocabulary plus validated identity and relation of each additional Contact; no assignment by name. |
| M-088 | Contact designation | `Customer.position` is a source candidate; native `Contact.designation` requires proven person/contact ownership. No automatic role or permission changes. |
| M-089 | Billing recipient | **Policy blocked:** `is_invoice_recipient` does not by itself establish a billing/authorization model. |
| M-090 | Contact source | `Customer.source` exists; controlled meaning, target and retention remain unapproved. |
| M-091 | Reachability | `Customer.reachability` is `Int`; its scale, meaning and destination remain unapproved. |
| M-093 | Additional contacts | `Customer.contacts[]` exists but identity, deduplication and shared-Contact policy are not approved. |
| M-094 | Additional addresses | `Customer.customer_addresses[]` exists; membership, primary selection, deletion and Dynamic Link ownership need approval. |
| M-096 | Fax | `Customer.phone_fax` exists; type and native contact-phone representation require approval. |
| M-097 | Website | `Customer.url` exists; link ownership, URL normalization and overwrite policy require approval before using `Customer.website`. |
| M-098 | Birth date | **Intentionally excluded:** identity-safe source use and privacy/retention authorization are missing. |
| M-100 | HERO created timestamp | **Intentionally excluded** from native `creation`; a separate external observation needs its own contract. |
| M-101 | HERO modified timestamp | **Intentionally excluded** from native `modified`; a separate external observation needs its own contract. |
| M-102 | Task completion | `tasks(is_done)` is a filter, not proof of a result field. `done_date` may be observed; no native state transition. |
| M-103 | Task assignee | External `target_user_id` is not a native `User`/`Employee` link. Resolver and authorization remain required. |
| M-104 | Calendar recurrence | `is_recurring` is only a source observation; no RRULE or generated recurring Event instances. |
| M-106 | Calendar resource | External resource IDs remain `HERO Event Resource` rows, not `Employee` links. |
| M-107 | Calendar partner | External partner IDs remain `HERO Event Partner` rows, not `User` links or invitations. |

## Package adoption and execution boundaries

- Reviewed package format: `hero-frappe-mapping-package-v2` with `format_version`, `schema_fingerprint`, `schema.custom_doctypes`, `schema.custom_fields`, `schema.relations` and `mappings`.
- Upload and preview are non-adopting; they must not provision metadata or import business records. Selected changes require authorized System Manager confirmation, schema-first validation and revision-bound mapping activation.
- Site-specific custom field mappings are restricted to approved writable targets. Native identity, company, contact, address, relationships, status and lifecycle use explicit protected purposes and existing adapters.
- Unknown transformations, missing target metadata, unapproved relations, incompatible revisions and unsupported values fail closed. Existing/foreign fields and DocTypes must not be overwritten or deleted by adoption.
- Frappe DDL is not guaranteed to roll back. A schema-adoption result must record the actual created definitions and be re-previewable before any retry.
- Synthetic offline data are located at [`hero_mapping_fixture_v1.json`](../hero_integration/tests/fixtures/hero_mapping_fixture_v1.json). This fixture is **not** a Frappe installation fixture and is never imported automatically. Other isolated fixtures are test-only.
- Source-to-target identity belongs to `HERO Integration Link`, scoped by source system/instance/entity/ID. Hashes, run/record status and mapping revision are retained as provenance; naming similarity does not establish identity.
- No live HERO requests, scheduled polling, automatic sync, production business-data migration, Calendar execution, native workflow transitions, invitations, or reverse synchronization follow from mapping approval alone.

## Release consistency

This document describes the **accepted isolated-site four-domain contract**. A clean Git checkout is not assumed to contain the same runtime changes or adopted site metadata. Before publishing a release, compare the Git revision with the isolated site's four-operation registry, DocType/controller definitions, mapping and service revisions, import preview, and focused fixture regressions. **Do not claim the four-domain deployment is present merely because this Markdown file is committed.**

[Installation](../INSTALLATION.md) · [DocType extensions](DOCTYPE_EXTENSIONS.md)
