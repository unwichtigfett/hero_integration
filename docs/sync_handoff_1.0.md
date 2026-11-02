# Synchronization handoff — Customer, Project, Task and Calendar

**Source review:** 2 November 2026 · **Delivery:** partial  
**External execution:** not authorized · **Target runtime:** unverified

A mapping Draft, approved Query document and execution permission are separate. Check the source instance/binding, schema fingerprint, selected active rules, service revision, site permissions and recovery evidence before running anything.

## Implemented source behavior

| Area | Current contract |
| --- | --- |
| **Customer** | Stable HERO Integration Link identity; native Customer, primary Contact and Address adapters. Shared updates without proven authority become conflicts. Names/emails are not identities. |
| **Project** | HERO identity, resolved Customer, separately confirmed ERPNext Company and server-generated title. Partner notes stay separate. HERO Project document names use `-H`. |
| **Task** | Separate native Task intake: stable system/instance/type/source ID, required subject, selected optional fields, conflict baselines and idempotent replay. Source deletion must not delete or close the Task. |
| **Task read service** | Saved owned Draft can generate a read-only service Draft. Seal binds rules, source and snapshot. Publication is not read approval; generated pages recheck mapping/service revisions. |
| **Mapping exchange** | Independent Query, Mutation and personal Draft boundaries. Preview protects complete target context, identities and provenance. Schema provisioning and runtime authority are separate. |
| **Calendar** | Native Event identity, project resolver through existing readable Integration Link, omission preservation and conflict-aware updates. Partner/resource observations are not native participants. |
| **Other safeguards** | Read service approval, rule locking, checkpoints and source-binding validation are implemented in source. Task lifecycle and guarded ToDo/assignment-policy code also exist, but need site-specific acceptance. |

## Pending or unverified

- **Task:** external deadline NULL/timezone semantics, completion/unassignment edge cases, initial ToDo policy and transfer acceptance, framework assignment-intent patch.
- **Calendar:** source timezone, all-day boundaries, recurrence, Customer and participant/resource identity. Unknown recurrence is blocked; true recurrence needs review. Existing Event time-group conflicts hold changes while independent fields may proceed.
- **Exchange:** business binding/action import and complete import preview, particularly when the target lacks a validated snapshot.
- **Runtime:** target database/schema, permissions, external HERO execution, polling, recovery and full mandatory-area acceptance.

## Rules that remain in force

- Never infer identity or permissions from names, emails, Project/Partner IDs or an unreviewed source value.
- Omitted values are not clears. **Unverified NULL/empty semantics must not authorize clearing, deletion, completion or unassignment.** Legacy Task lifecycle paths still need negative tests against this requirement.
- Mutation metadata does not authorize a write to HERO. A System Manager must approve exact active mapping/service revisions and the applicable action gates.
- Preview the entire target context before importing; protect target provenance and run schema provisioning only when needed.
- Articles, versions, prices, inventory and accounting are **outside the current mapping scope**.

**Acceptance:** `PARTIAL / EXECUTION_UNVERIFIED`. Full acceptance requires evidence for every agreed mandatory area, the target runtime and recovery.

## Orchestration and reservation contract

The production `AUTO_SYNC` scheduler reuses the existing reviewed migration
runner for each enabled area. These are repeated migration passes; they must
not be described as a complete incremental delta feed. They retain the same
source binding, read approval, mapping checks, checkpoints and recovery path
as manual migration requests.

B8 Pending Sync remains a separate, explicitly requested bound-only preflight.
An unproven provider and the missing atomic baseline writer remain blocked;
this contract does not activate a second writer or grant automatic execution.
B8 can replace orchestration only after its provider, writer and recovery
contracts have separate acceptance evidence.

Both run families reserve through `services/run_reservation.py`: the same
connection-level setup lock and durable `tabSingles` row lock serialize
current checks of legacy and B8 run records. A RUNNING B8 record prevents a
legacy migration claim, and an active legacy slot prevents a B8 claim. Two
B8 run IDs cannot both claim the site. B8 adapters never commit on behalf of
their caller; reservation visibility follows the enclosing transaction.
An uncertain/orphan legacy slot is not silently reclaimed by B8.

Offline ordering checks cover the reservation boundary. The separate
`test_sync_run_reservation_frappe` suite requires an explicitly selected
**disposable** site and verifies two real MariaDB connections; merely adding
or passing offline tests is not a concurrency or deployment acceptance.
