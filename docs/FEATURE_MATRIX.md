# Feature matrix

| Area | State | Evidence or limitation |
| --- | --- | --- |
| ERPNext v16 prerequisite | Implemented and offline-tested | A pre-install guard rejects missing, non-v16, or incomplete ERPNext before HERO writes; it never auto-installs ERPNext. ERPNext v16 was reported in the VPS inventory, but fresh-site acceptance remains separate. |
| HERO Custom Fields | Implemented and offline-tested | The create-only, conflict-safe baseline installer defines four Customer and five Project HERO fields. The last recorded extended VPS schema contained 35 provisioned Custom Fields in total. |
| HERO Customer read | Implemented and mock-tested | Fixed `HeroCustomers` source query; no runtime field selection in the native reader. |
| HERO Project Match read | Implemented and mock-tested | Fixed `HeroProjectMatches` source query; only validated inputs are used for native Project creation and naming. |
| HERO Calendar mapping | Configured; approval pending | 25 mapping definitions were recorded as technically Active for `HeroCalendarEvents`. The current native migration runner has no Calendar read/import phase. |
| HERO Task mapping | Configured; execution limited | 15 technically Active, approved mapping definitions were recorded for `HeroTasks`. The current native migration runner has no Task read/import phase. |
| ERPNext Project title | Implemented and mock-tested | Server-side `PV-<number> \| Photovoltaik, <salutation> <customer>, <project address>` generation blocks incomplete or unknown data and never overwrites a conflict. |
| Mapping installation state | Implemented and offline-tested | Fresh installation and repeated migrate create no mapping definitions. The last VPS inventory reported 75 technically Active definitions; this is site configuration, not an installation default. |
| Mapping package import and export | Implemented and offline-tested | Deterministic versioned packages validate GraphQL fingerprints, schema, metadata, permissions, types, relations, and conflicts. Upload has no writes. |
| Bulk schema and mapping adoption | Implemented and offline-tested | One checklist selects HERO-owned Custom DocTypes, Custom Fields, relations, and mappings. Confirmed adoption provisions schema before mapping activation; it does not migrate business data or guarantee DDL rollback. |
| Customer mapping | V1 approved; V2 pending | Customer V1 has 14 Active mappings in the recorded inventory. Customer V2 adds draft M-099 for `Address.address_line2`, but the native Address persistence path does not yet write that field. |
| Project mapping | Approved; configured | 21 technically Active mappings were recorded. Migration requires source identity, record name, Customer link, and confirmed Company bindings. |
| Article and Item migration | Not implemented | Requested future scope; no approved executable Item mapping or verified Article/Product migration. |
| Canonical source link | Implemented and mock-tested | One HERO Integration Link carries source system, optional instance, entity type, external ID, unique source key, target, hashes, mapping version, and status. |
| GraphQL schema snapshots and graph viewer | Implemented and offline-tested | Saved introspection JSON produces a deterministic graph. The viewer does not call HERO. |
| Mapping field pickers and suggestions | Implemented and offline-tested | Saved operation selection sets and the installed site's Frappe metadata drive searchable bidirectional suggestions. Incompatible, protected, stale, or unresolved targets cannot be activated. |
| GraphQL service management | Implemented and offline-tested | Saved query services are schema-bound. Mutation drafts can be inspected and validated locally, but mutation execution is disabled; subscriptions are not supported for execution. |
| API Analyzer and workspace | Implemented; browser acceptance pending | The Desk workspace exposes API & Schema, Mapping, Synchronisation, and Settings views. Authenticated visual/browser acceptance is not yet evidenced. |
| Custom direct mappings | Implemented and offline-tested | Approved writable targets use Frappe metadata and Document APIs. Protected and standard fields cannot be overridden by generic mappings. |
| Preview, idempotency, conflicts, retry | Implemented and mock-tested | Preview does not write targets; changed source or target data is reported as a conflict instead of silently overwritten. |
| Manual migration | Implemented and mock-tested | The native runner supports Customer and Project phases only, with a completed Customer phase required before Project import. Calendar, Task, and Item imports are not implemented in that runner. |
| Scheduled sync | Implemented; disabled pending acceptance | One-way HERO-to-Frappe hourly hook is guarded by configuration and active-run state. No successful live scheduled run is evidenced. |
| ERPNext persistence | Implemented; live acceptance pending | Native Customer, Contact, Address, and Project write paths have offline/mock coverage. End-to-end live HERO-to-ERPNext persistence remains unverified. |
| Helpdesk profile | Limited | The optional Helpdesk installation is detected; ERPNext remains responsible for Customer mirroring. |
| CRM / LEADS | Not connected | Installed CRM does not establish a functioning LEADS adapter or HERO-to-CRM migration. |
| Attachments | Not available | No approved HERO attachment query or transfer contract. |
| Helpdesk tickets | Not available | No approved HERO ticket source or target write contract. |
| Reverse sync and deletion | Not available | No approved reverse-write contract or authorization for source-driven deletion of target business records. |
| Live integration acceptance | EXECUTION_UNVERIFIED | No completed live HERO query/import, production synchronization, or end-to-end browser acceptance is evidenced for the reviewed revision. |
