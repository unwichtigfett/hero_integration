# HERO workspace

`hero-integration` is the only visible HERO Desk workspace.

## Unified Desk navigation

`hero-integration` opens the preserved Adapter host and contains exactly five
main tabs:
 - overview
 - API & Schema
 - Mapping
 - Synchronisation
 - Settings
API & Schema tab:
 - API Analyzer
 - Graph Viewer
 - GraphQL Services [Query | Mutation]

Mapping requires an explicit saved, validated snapshot selection.
No default to the newest snapshot.

## Runtime Frappe target catalog

The Mapping section discovers installed apps and their DocTypes from the active
site at request time. It then exposes App → DocType → Field choices from Frappe
metadata, including standard and Custom Fields.

Any field of another installed app can become a validated **Draft proposal**,
but the app does not activate it or use direct DocType writes.

## Mutation drafts

The Analyzer and Graph Viewer retain schema mutation nodes, input relations,
and return types.
Drafts never contain saved variables or credentials and are never
executed. Every execution fails closed when `HERO_MUTATION_EXECUTION_DISABLED`.

The CSS/DOM source port has not been visually inspected in a browser in this
step. Visual parity is therefore a manual step, not a claimed result.

## RPC inventory and bounded mapping

| Original RPC | Classification | Native result |
| --- | --- | --- |
| `get_context` | SAFE_READ | `workspace_context` from saved snapshots |
| `get_schema` | SAFE_READ | `workspace_schema` full saved introspection projection |
| `get_graph` | SAFE_READ | `workspace_graph` complete saved graph projection |
| `list_services`, `get_query` | SAFE_READ | saved `HERO GraphQL Service` records |
| `save_query`, `validate_saved_query`, `preview_query`, `delete_query` | SAFE_LOCAL_SERVICE_MANAGEMENT | permission-checked local read-only service definitions |
| `get_settings` | SAFE_READ | redacted native settings read |
| `save_settings` | SAFE_LOCAL_SERVICE_MANAGEMENT | native DocType validation; token is never returned |
| `execute_service` | WRITE_HERO_MUTATION | visible Execute control; both workspace compatibility and `execute_graphql_service` fail closed with default setting `HERO_MUTATION_EXECUTION_DISABLED` |
| `refresh_schema`, `synchronize_connection` | LEGACY_PROCESS_ONLY | visible overview actions disabled; they require old binding/transport state |

The source workspace displays Query and Mutation modes.

## Complete stored schema source

The latest read-only source snapshot is `231026`
`schema-fee37bfa55a02f7a4d09b6b902e6600a835c992c8fe6644c358f5e511e025201`
(modified 23-10-2026 01:07:07 UTC). Its source fingerprint is
`fee37bfa55a02f7a4d09b6b902e6600a835c992c8fe6644c358f5e511e025201`.
The source payload was transferred only to an untracked temporary location for
projection validation, not to Git. The actual source reports 307 types, 65
queries and 88 mutations.
