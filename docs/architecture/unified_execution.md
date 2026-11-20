# HERO migration and polling runtime

The entry point is `services.execution_runtime.execute`. Import workers resolve their handler from the stored action. Existing queue method names remain valid for jobs already in Redis.

| Trigger | Authority | Effect |
| --- | --- | --- |
| Manual area, full import or resume | Explicit migration request, source and applicable mapping permissions | The existing migration handler processes the approved scope and its dependencies. Polling may be OFF and the scheduler may be disabled. A queued job needs a worker. |
| Scheduled MONITOR | Enabled scheduler, due interval and approved read plan | Reads one bounded materialized page and records the observation. A single page does not establish full coverage. |
| Scheduled AUTO_SYNC | Enabled scheduler, due interval, approved automatic plan and proven change provider | Updates eligible fields of existing bound objects. Missing change coverage prevents cursor advancement. |
| Manual pending sync | Explicit request for the approved plan generation | Uses the same bound-object processor and change-provider checks. |
| Administrative one-shot | System Manager POST, explicit confirmation, request identity, approved plan generation and source revision, polling OFF | Compares known bound IDs or requests verified delta discovery. The request has its own persistent 50-identity budget and schedules no successor. |

## Native intake and verified baselines

Each persisted migration worker resolves the approved source, reader and mapping. `process_migration_record` acquires the source claim and checks integration-link provenance before invoking its domain adapter. The adapter owns native ERPNext validation, relationships and document creation. The runtime rereads the stored target and publishes object and eligible scalar field bindings in the same transaction. Preview operates on the existing read-only path. A pending review, deleted source, adopted Item or failed native validation cannot establish a new scalar baseline.

The source claim includes site, source instance, schema object type, destination DocType and external identity. The destination claim also checks the native record name. Existing claims with another mapping identity or revision require review. Post-success relationship work checks its originating run and mapping, and retains scalar acknowledgments made since the original read.

A runtime plan can use an approved saved migration query. Its schema, projection and variables pass through the existing GraphQL materializer. Historical logical service names resolve to the actual reviewed root. Universal B8 mappings continue to require their separate operation seal. Conditional and fragment-based saved selections require additional result-binding support.

## Budget and recovery

`services.execution_budget` reserves canonical identities on a separate MariaDB transaction before business processing. The root trial and its children use one budget. A batch of dependencies is admitted together. A business rollback keeps the reservation. A later source version of an admitted identity can be processed under the same slot. The limit applies to distinct processed identities. Source reads for cohort selection can return additional candidates.

Offset readers freeze Page Overlap when a run starts. The value is 0–20 and must be smaller than the effective page size. A full page advances by its size minus the overlap. The saved window contains identity and record fingerprints, so an unchanged repeated row can be suppressed and a changed row reaches the normal conflict checks. A positional checkpoint provides no complete change watermark. Native migration recovery can replay reads and resolve them by source identity.

## Bound writes and change discovery

The writer requires a scoped object binding, field binding, mapping revision, approved field rule and confirmed source and target baselines. An old integration link alone is insufficient. The processor locks the binding and target, checks Frappe permissions, and calls the native document save only for permitted changes. The save, acknowledgment and change receipt share one transaction. Receipts are scoped to the execution contract. A conflict preserves the local field and prevents confirmation of the new cursor. `HERO Sync Change Event` stores field fingerprints. Outgoing webhook delivery remains owned by Frappe.

Fast discovery accepts version indicators and fetches exact detail only for a bound identity whose version lacks an accepted receipt. The stored HERO schema inspected for this implementation has fingerprint `079eb3720c26929386b692aaf04dab4ac356d70f0f090910d0e6bebe8c1e0a86`. It exposes ID filters and pagination for selected queries. It supplies no verified contract for complete ordered changes, equal timestamps or removals. Automatic Fast Mode is unavailable for that source. The separate comparison of known IDs requires an approved reader that supports exact filtering and never advances a global delta cursor.

Operators can inspect the integration run, polling event, sync run, budget use and confirmed position through the existing synchronization page and ledgers. An exact repeated one-shot request returns its recorded outcome. An abandoned sync receipt requires an explicit administrative action with the matching fingerprint and absence of the execution lease.

## Verification boundary

Synthetic regressions exercise Fast metadata/detail selection, overlap, replay and existing migration handlers. Isolated Frappe, MariaDB and Redis tests cover concurrent admission at identity 51, queue dispatch with polling OFF, native document events, rollback before acknowledgment, conflict preservation and relationship recovery. The reference-schema validation used the stored introspection document. No live HERO request or external webhook delivery was part of acceptance.

Local native edits append a redacted `HERO Integration Record` in the document transaction. Its identity includes the object binding, native save timestamp and changed field fingerprints. A rollback removes the observation. A later edit returning a field to its previous value creates a new transition. Polling and migration scope their writes to suppress echoes. Reading an observation starts no outbound work.

## Live run progress

System Managers can follow executions on the Migration / Synchronization page. Its read-only progress view projects committed run counters, identity receipts, child checkpoints, pending relationships and the root budget. Replayed identities consume one slot. The source-row read count can include overlap. Document outcomes refer to primary targets reported by the handler. Older runs may lack specific counters and display a dash.

The full-run card lists the frozen stages and their children. A successful full run also requires terminal mandatory relationships and reconciliation. Budget occupancy and elapsed time are displayed separately. With no authoritative source total the activity bar is indeterminate. Five minutes without a confirmation displays a waiting message. It does not stop the worker.

The Desk page rereads its authorized projection on native list updates and every five seconds while visible. It releases subscriptions and timers on close. Progress refresh reads site data only. The additive fields and lookup indexes are applied through the existing Frappe DocType synchronization.
