# HERO Desk workspace

- Views: Overview, API & Schema, Mapping, Synchronisation and Settings.
- Mapping Editor: selected validated schema, site metadata, personal drafts and explicit publication.
- `workspace_context`, `workspace_schema`, `workspace_graph`: saved snapshot information.
- `workspace_service_execute`: Explicit read-only Query execution for System Manager; validated service, matching snapshot and checked variables required. Mutations are rejected before transport.
- `execute_graphql_service`: separate guarded Mutation endpoint with active contract and execution controls.
- Synchronisation: area runs, relationship status, conflicts, checkpoints and polling receipts.
- Polling status counts automatic executions separately from manual imports and skipped ticks.
- Visible page refresh: every 30 seconds idle, every 5 seconds during a run; hidden tabs do not refresh.
- Runtime changes require the matching app revision, site migrations and built frontend assets.
- Source tests and earlier visual checks do not establish the behavior of the deployed UI; verify permissions, employee isolation and layouts on the target site.
- Endpoint definitions and permission checks: [`api.py`](../hero_integration/api.py).
