# Feature matrix

| Area | State | Evidence or limitation |
| --- | --- | --- |
| ERPNext v16 prerequisite | Implemented and offline-tested | A pre-install guard rejects missing, non-v16, or incomplete ERPNext before HERO writes. It never auto-installs ERPNext. Fresh-site acceptance is a separate step. |
| HERO Custom Fields | Implemented and offline-tested | The create-only, conflict-safe baseline installer defines four Customer and five Project HERO fields. Extended schemas add further provisioned Custom Fields. |
| HERO Customer read | Implemented and mock-tested | Fixed `HeroCustomers` source query; no runtime field selection in the native reader. |
| HERO Project Match read | Implemented and mock-tested | Fixed `HeroProjectMatches` source query. Native Project creation and naming use only validated inputs. |
| HERO Calendar mapping | Configured; approval pending | Mapping definitions for `HeroCalendarEvents` can be technically Active. The native migration runner has no Calendar read/import phase. |
| HERO Task mapping | Configured; execution limited | Approved mapping definitions for `HeroTasks` can be technically Active. The native migration runner has no Task read/import phase. |
| ERPNext Project title | Implemented and mock-tested | The server generates `PV-<number> \| Photovoltaik, <salutation> <customer>, <project address>`. Generation blocks incomplete or unknown data and never overwrites a conflict. A changed HERO customer name or salutation alone never renames an existing Project. Trade, number and address changes regenerate a locally unchanged title. A missing salutation does not block. Titles over the field limit stay rejected for manual migration. |
| Administrator review of held records | Implemented and offline-tested | The import creates these records as `Pending Review` and notifies the administrators: a new Project with a missing or incomplete address (title without address), a new Project without an unambiguous Customer link, and a new recurring Event. HERO supplies no recurrence rule, so only the delivered occurrence is imported. Runs never update a held record. Tasks/Events of a held Project wait. Administrators can only Approve (normal sync) or Reject. Reject deletes the target unless other records reference it, and the HERO record is never imported again. Every Execute run also sends one notice that lists all records it could not import, grouped by reason. |
| Mapping installation state | Implemented and offline-tested | Fresh installation and repeated migrate create no mapping definitions. Active definitions are site configuration, not an installation default. |
| Mapping package import and export | Implemented and offline-tested | Deterministic versioned packages validate GraphQL fingerprints, schema, metadata, permissions, types, relations, and conflicts. Upload writes nothing. |
| Bulk schema and mapping adoption | Implemented and offline-tested | One checklist selects HERO-owned Custom DocTypes, Custom Fields, relations, and mappings. Confirmed adoption provisions schema before mapping activation. It does not migrate business data or guarantee DDL rollback. |
| Customer mapping | V1 approved; V2 pending | Customer V1 mappings are Active. Customer V2 adds draft M-099 for `Address.address_line2`, but the native Address persistence path does not write that field yet. |
| Project mapping | Approved; configured | Project mappings are technically Active. Migration requires source identity, record name, Customer link, and confirmed Company bindings. |
| Article and Item migration | Not implemented | Requested future scope. No approved executable Item mapping and no verified Article/Product migration exist. |
| Canonical source link | Implemented and mock-tested | One HERO Integration Link carries source system, optional instance, entity type, external ID, unique source key, target, hashes, mapping version, and status. |
| GraphQL schema snapshots and graph viewer | Implemented and offline-tested | Saved introspection JSON produces a deterministic graph. The viewer does not call HERO. |
| Mapping field pickers and suggestions | Implemented and offline-tested | Saved operation selection sets and the installed site's Frappe metadata drive searchable bidirectional suggestions. Incompatible, protected, stale, or unresolved targets cannot be activated. |
| GraphQL service management | Implemented and offline-tested | Saved query services are schema-bound. Mutation drafts can be inspected and validated locally, but mutation execution is disabled. Subscriptions are not supported for execution. |
| API Analyzer and workspace | Implemented; browser acceptance pending | The Desk workspace exposes API & Schema, Mapping, Synchronisation, and Settings views. Authenticated visual/browser acceptance is not yet evidenced. |
| Custom direct mappings | Implemented and offline-tested | Approved writable targets use Frappe metadata and Document APIs. Generic mappings cannot override protected and standard fields. |
| Preview, idempotency, conflicts, retry | Implemented and mock-tested | Preview does not write targets. Changed source or target data is reported as a conflict, never silently overwritten. |
| Manual migration | Implemented and mock-tested | The native runner supports only Customer and Project phases. Project import requires a completed Customer phase. The runner does not implement Calendar, Task, and Item imports. |
| Scheduled sync | Implemented; disabled pending acceptance | Configuration and active-run state guard the one-way HERO-to-Frappe hourly hook. No successful live scheduled run is evidenced. |
| ERPNext persistence | Implemented; live acceptance pending | Native Customer, Contact, Address, and Project write paths have offline/mock coverage. End-to-end live HERO-to-ERPNext persistence is unverified. |
| Helpdesk profile | Limited | The app detects the optional Helpdesk installation. ERPNext stays responsible for Customer mirroring. |
| CRM / LEADS | Not connected | Installed CRM provides no working LEADS adapter or HERO-to-CRM migration. |
| Attachments | Not available | No approved HERO attachment query or transfer contract. |
| Helpdesk tickets | Not available | No approved HERO ticket source or target write contract. |
| Reverse sync and deletion | Not available | No approved reverse-write contract or authorization for source-driven deletion of target business records. |
| Live integration acceptance | EXECUTION_UNVERIFIED | No completed live HERO query/import, production synchronization, or end-to-end browser acceptance is evidenced for the reviewed revision. |
