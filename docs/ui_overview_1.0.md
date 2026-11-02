# HERO Desk workspace

The `hero-integration` Desk workspace has five views: **Overview**, **API & Schema**, **Mapping**, **Synchronisation** and **Settings**. API & Schema contains API Analyzer, Graph Viewer and GraphQL Services (Query / Mutation).

## Mapping and target fields

The editor requires an explicitly selected, saved and validated schema snapshot. It discovers installed apps, DocTypes and fields from the active site. Compatible targets are **Draft proposals**, not automatically active mappings or permission to write. Personal Drafts, revisions and Query/Mutation directions have separate authorization rules.

## Query and Mutation execution

| Action | Actual behavior |
| --- | --- |
| Workspace context, schema and graph | Read saved validated snapshot information. |
| GraphQL service list/detail/save/validate/delete | Permission-checked **local** management of saved service definitions. |
| `workspace_service_execute` | **Explicit read-only Query execution** for System Manager. Requires validated saved Query, matching revision/snapshot and checked variables. Mutations are rejected before transport; no migration or record acknowledgment occurs. |
| `execute_graphql_service` | **Separate mutation endpoint** guarded by active contract, global switch, role/confirmation and one-shot audit/transport. A Mutation Draft alone cannot execute. |
| Mapping Draft and exchange endpoints | Local changes with owner, CAS, snapshot and publication guards; no automatic runtime permission. |
| Settings and synchronization endpoints | Perform only their specific allowed reads/configuration/run requests after server-side checks. |

Old compatibility RPC names and the prior classification of every `execute_service` call as a HERO mutation are **not** a reliable account of the current code. Review the actual endpoints in [`hero_integration/api.py`](../hero_integration/api.py).

## Acceptance boundary

Source tests and earlier visual checks do not establish the behavior of the current deployed Desk UI. Verify the exact branch/commit, employee isolation, administrator controls, narrow layouts, Frappe permissions and browser runtime on the target site.

Schema source payloads and credentials stay outside Git. Generated GraphQL Query documents do not bypass the active service/mapping and source-binding contracts.
