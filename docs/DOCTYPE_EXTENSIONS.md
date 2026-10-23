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
