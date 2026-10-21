# Feature matrix

| Area | State | Evidence or limitation |
| --- | --- | --- |
| ERPNext v16 prerequisite | Implemented and offline-tested | A pre-install guard rejects a missing, non-v16, or incomplete ERPNext before HERO writes; it never auto-installs ERPNext. |
| HERO Custom Fields | Implemented and offline-tested | Create-only, conflict-safe installer creates four Customer and five Project HERO fields. |
| HERO Customer read | Implemented and mock-tested | Fixed `HeroCustomers` query; no runtime field selection. |
| HERO Project Match read | Implemented and mock-tested | Fixed `HeroProjectMatches` query includes only validated title inputs; no runtime field selection. |
| ERPNext Project title | Implemented and mock-tested | Server-side `PV-<number> | Photovoltaik, <salutation> <customer>, <project address>` generation blocks incomplete or unknown data and never overwrites a conflict. |
| Mapping installation state | Implemented and offline-tested | Fresh installation and repeated migrate create no mapping definitions. |
| Mapping package import and export | Implemented and offline-tested | Deterministic versioned packages validate a GraphQL fingerprint, schema, metadata, permissions, types, relations, and conflicts. Upload has no writes. |
| Bulk schema and mapping adoption | Implemented and offline-tested | One checklist selects HERO-owned Custom DocTypes, Custom Fields, relations, and mappings. The confirmed action creates schema first and activates mappings after target validation. It does not migrate data or promise DDL rollback. |
| Customer mapping | Configured by administrator | Migration requires active Customer identity, record-name, type, and lifecycle bindings. |
| Project mapping | Configured by administrator | Migration requires active Project identity, record-name, Customer-link, and confirmed-Company bindings. |
| Canonical source link | Implemented and mock-tested | One HERO Integration Link carries source system, optional instance, entity type, external ID, unique source key, target, hashes and status. |
| GraphQL schema snapshots and graph viewer | Implemented and offline-tested | Saved introspection JSON produces a deterministic graph. The viewer does not call HERO. |
| Mapping field pickers and suggestions | Implemented and offline-tested | Saved operation selection sets and the installed site's Frappe metadata drive searchable bidirectional suggestions. Schema-only, protected, stale, incompatible, and unresolved child-relation candidates cannot be activated. |
| GraphQL service management | Implemented and offline-tested | Read-only services bind to a validated schema fingerprint. Mutations and subscriptions are rejected. |
| Custom direct mappings | Implemented and offline-tested | Active mappings use Frappe metadata and Document APIs. Protected and standard fields cannot be overridden. |
| Preview, idempotency, conflicts, retry | Implemented and mock-tested | Preview does not write targets; changed source or target becomes a conflict. |
| Manual and scheduled sync | Implemented and mock-tested | One-way HERO to Frappe; hourly hook is disabled until configured. |
| ERPNext persistence | Limited | Native v16 persistence and Custom Field acceptance are pending on a fresh owned VPS. |
| Helpdesk profile | Limited | Detected only when ERPNext and Helpdesk are installed; ERPNext owns Customer mirroring. |
| Attachments | Not available | No approved HERO attachment query contract. |
| Helpdesk tickets | Not available | No approved HERO ticket source or target write contract. |
| Reverse sync | Not available | No approved write contract. |

## RC acceptance-ready 15 evidence matrix

| REQ | Present implementation | Evidence | Remaining acceptance gate |
| --- | --- | --- | --- |
| RL-01–04 | System-Manager RPC gates; one Settings tab; HTTPS/host, Company and source settings | `api.py`, settings/mapping contract tests | Browser verification; user supplies credentials only in Settings. |
| RL-05–08 | Explicit saved-snapshot catalog, fingerprint binding, separate dynamic Query and Mutation mappings | `schema_operations.py`, `hero_graphql_mapping`, mapping tests | Real read-only HERO snapshot and review of discovered operations. |
| RL-09–11 | Separate mutation client, per-operation contract, AST/type validation, preview fingerprint, confirmation and redacted audit | `graphql_contract.py`, `graphql_mutation.py`, mutation tests | No live mutation was run; each live operation needs separate approval. |
| RL-12–18 | OFF/MONITOR/AUTO_SYNC settings, scheduler gate, trusted scheduler entry and preview no-write path | `polling.py`, `tasks.py`, `migration.py`, polling/scheduler tests | MONITOR/AUTO_SYNC runtime against an authorized live source and target. |
| RL-19–20 | One page/sidebar, five English tabs, canonical GraphQL Services route, explicit loader error state, scoped neutral cards | Desk, Node loader and Python UI-contract tests | Authenticated browser check in light/dark and narrow view. |
| RL-21–23 | Gates and test fixtures exist; no live HERO or 15-ProjectMatch run is claimed | source and focused tests only | User-authorized live read, MONITOR, AUTO_SYNC, Helpdesk and 5+5+5 ProjectMatch gates. |
| RL-24 | This matrix, mapping guide and manual protocol are maintained for the RC | repository documentation | v1.0.0 documentation/tag remain intentionally deferred. |
