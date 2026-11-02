# HERO Integration

HERO Integration connects HERO GraphQL with Frappe/ERPNext v16. It provides saved read services, a Mapping Editor, native ERPNext adapters and guarded synchronization. Installing the app does **not** contact HERO or activate any mappings.

The installation branch is [`hero_int_1.0`](https://github.com/unwichtigfett/hero_integration/tree/hero_int_1.0). Development continues on `hero_int_dev`; these branches can differ. See the [installation guide](installation_guide_1.0.md).

## What the source supports

- **Customer:** Native Customer, primary Contact and Address using HERO Integration Link identities. Shared Contacts and Addresses need proven update authority.
- **Project:** A resolved Customer link, independently confirmed ERPNext Company and protected server-generated title. HERO Project document names use the reserved `-H` suffix.
- **Task:** A scoped native ERPNext Task intake exists alongside the legacy `HERO External Task` observation projection. Selected fields are reconciled against baselines; assignment, completion and deadlines need additional acceptance.
- **Calendar:** Native Event projection with project resolution and conflict safeguards. Timezone, all-day, recurrence and participant semantics remain approval-bound.
- **GraphQL:** Saved, validated Query services can execute explicitly through a read-only workspace endpoint. Generated Task queries remain drafts until approval. Mutation drafts grant no external execution authority.
- **Mapping:** Personal Drafts, revisions, directional Query/Mutation exchange and explicit activation gates.
- **Synchronization:** Manual runs, checkpoints, conflict reporting and polling controls. Code coverage alone does not establish live HERO acceptance. Automatic polling currently enumerates Customer and Project, not all four areas.

Existing reviewed Query documents retain their contracts; generated operations never silently replace them.

## Safety and approval

A saved Draft, schema preview, imported package or validated service is **not** permission to synchronize. Before execution, check the source instance and binding, schema fingerprint, active mapping and selected rule content, read-service revision, permissions and recovery evidence.

Integration Links, not names or email addresses, own external identity. Omitted fields stay unchanged. Do not interpret unconfirmed NULL/empty values as permission to clear, delete, complete or unassign. Legacy Task lifecycle and ToDo paths require explicit negative testing before use.

Use HTTPS and an allowlisted HERO host. The token stays in a Frappe `Password` field and must not be exposed in Desk responses, logs or queue arguments.

## Language

HERO Integration uses English source text and includes a German translation
catalog. It follows Frappe v16 language selection: a language set on the User
record takes precedence; otherwise Frappe uses the language in System Settings.
There is no separate HERO language setting. Sign out and sign back in after
changing a language so Frappe refreshes the session and Desk translations.

Technical names and user-provided or external-system content are retained as
received. A message without a matching translation remains in its source
language. Terms such as Mapping and Polling remain in English in the German UI.

## Desk appearance

The native Frappe appearance selector includes **Navy**, a GitHub-style dark
color palette. Choose it alongside Light, Dark and Automatic under Appearance.
The choice is saved per user in `User.desk_theme`. Navy uses Frappe's dark
component behavior and changes color tokens only. Existing users keep their
current theme. Install/migrate registers the extra Select option; uninstall
returns Navy users to Dark and removes only that option.

## Documentation

| Topic | Documents |
| --- | --- |
| Installation and operation | [Install](installation_guide_1.0.md) · [Configure](docs/configuration_guide_1.0.md) · [Workspace](docs/ui_overview_1.0.md) |
| Current source capabilities | [Feature matrix](docs/feature_matrix_1.0.md) · [Synchronization handoff](docs/sync_handoff_1.0.md) |
| Mapping and data model | [Mapping decision](docs/mapping_1.0.md) · [Mapping baseline](docs/mapping_matrix_1.0.md) · [DocType extensions](docs/doctype_extensions_1.0.md) · [Query field contract](docs/query_fieldcontract_1.0.md) |
| Quality and evidence | [Manual acceptance](docs/manual_acceptance_1.0.md) · [CI](docs/ci_installtest_1.0.md) · [Run checkpoints](docs/run_checkpoints_1.0.md) · [Portability](docs/portability_report_1.0.md) |
| Architecture and provenance | [App integration](docs/app_integration_1.0.md) · [File topology revision 00](docs/architecture/hero-integration_f-topology_00.md) · [Materializer](docs/materializer_1.0.md) |
| Historical mapping decisions | [Reviewed legacy mapping](docs/mapping/hero_mapping_1.0.md) · [Original proposal](docs/mapping/mapping_plan_1.0.md) |

## Verification state

Offline and disposable-site CI checks are useful evidence for their exact commit and test scope. They do not prove live HERO execution, installed target metadata, active mapping/service approval, production synchronization or recovery.

**Documentation reviewed:** 2 November 2026. **Runtime acceptance:** `EXECUTION_UNVERIFIED`.
