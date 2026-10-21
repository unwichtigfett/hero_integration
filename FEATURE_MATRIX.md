# Feature matrix

| Area | State | Evidence or limitation |
| --- | --- | --- |
| ERPNext v16 prerequisite | Implemented and offline-tested | A pre-install guard rejects a missing, non-v16, or incomplete ERPNext before HERO writes; it never auto-installs ERPNext. |
| HERO Custom Fields | Implemented and offline-tested | Create-only, conflict-safe installer creates four Customer and five Project HERO fields. |
| HERO Customer read | Limited V1 contract | Fixed `HeroCustomers` query; saved editable query contracts are not yet executed by the reader. |
| HERO Project Match read | Limited V1 contract | Fixed `HeroProjectMatches` query includes only validated title inputs; saved editable query contracts are not yet executed by the reader. |
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
| Preview and comparison state | Implemented and mock-tested | Preview does not write target, Link, conflict or observation state. Links retain separate successful and observed protected field-hash state. |
| Field conflict handling | Implemented and mock-tested | A HERO-only mapped field change can update through Frappe validation. A same-field change on both sides creates a durable Link conflict and an internal System-Manager notification; no overwrite occurs. |
| External mutation outcome | Implemented and offline-tested | Local duplicate/concurrency protection exists, but no HERO idempotency or exactly-once claim is made. Ambiguous results are audited and never retried automatically. |
| Central deletion gate | Implemented and isolated-native-tested | Default-OFF `Allow deletion` is registered on Frappe `on_trash` for every HERO-owned DocType and rechecked by the service RPC; it does not grant foreign ERPNext deletions. |
| Durable general output | Implemented and source-tested | Existing `HERO Integration Record` is extended as a versioned, paginated, System-Manager-only technical output projection; no general event broker or Core consumer is required. |
| Manual and scheduled sync | Implemented and isolated-native-tested | One-way HERO to Frappe; durable run reservation admits one concurrent worker and records the loser as Blocked. |
| ERPNext persistence | Limited | Native v16 persistence and Custom Field acceptance are pending on a fresh owned VPS. |
| Helpdesk profile | Limited, isolated checked | The profile reports missing Helpdesk and disabled ticket migration without implying compatibility; ERPNext owns Customer mirroring. |
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

## RC16 delta matrix

| Requirement | Existing implementation reused | RC16 delta | Evidence / remaining gate |
| --- | --- | --- | --- |
| Generic technical output | `HERO Integration Record` | Version, action, origin, correlation, observation time and redacted field-name projection; paginated System-Manager API | `services/integration_events.py`; Bench/Fresh-install evidence remains required. |
| Priority 0–10 | Existing classic mapping runtime and package | Default 5, strict validation, `0` retained, lossless 100→5 patch; ambiguous historic values disabled | `direct_mapping.py`, `normalize_mapping_priorities.py`, mapping tests. |
| Change detection/conflict | Existing Link hashes and migration path | Successful and observed values separated; field-hash overlap detects bilateral same-field conflict; Frappe `on_update` writes a technical observation only; preview stays no-write | `change_detection.py`, `migration.py`, isolated native and migration tests. |
| Admin conflict signal | Existing internal Frappe platform | Configurable recipients with enabled System-Manager fallback; durable Notification Log when evidence changes | `conflict_notifications.py`; delivery needs isolated Bench verification. |
| Deletion gate | Existing Settings and app-owned DocTypes | Default-OFF `on_trash` hook plus service RPC recheck | isolated `frappe.delete_doc()` regression; user browser acceptance remains required. |
