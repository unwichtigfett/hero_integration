# Manual acceptance — HERO Integration

Use an **owned, disposable Frappe/ERPNext v16 site** and synthetic records. This checklist is not permission to contact production HERO or run external mutations. Record the exact app commit, Frappe/ERPNext versions, site metadata and test results.

## Installation and mapping

1. Install ERPNext first and then HERO Integration. Verify app-owned DocTypes, installer-owned fields and the expected permissions. Repeat the migration; it must preserve existing configuration.
2. Confirm the local Company independently. A HERO Customer or Project ID must never select an ERPNext Company.
3. Confirm the source instance, binding revision and validated snapshot. Never assume the newest schema is approved.
4. Start with no active mappings. Test personal Draft save/no-op/discard/revision, role isolation, stale revision handling and explicit activation.
5. Preview Query, Mutation and personal Draft imports independently, against the **complete active context**. Confirm field-rule identities, owner/provenance, missing target references and no unintended replacement. A new schema requires separate provisioning review.
6. Validate and execute a synthetic Query through the read-only service path. Mutations must fail before transport. A generated Task Query requires specific mapping/service approval.

## Native records and conflicts

7. Exercise Customer and Project imports: stable Integration Link, native Contact/Address, shared-record conflict, approved Customer relation, independently confirmed Company, `-H` Project name and protected title.
8. Exercise native Task creation with a subject and optional selected description. Repeat imports, change source and target separately and together, then test omitted values, blank/NULL text, deleted source and mapping drift.
9. Test Task deadlines, completion and ToDo/assignment **as separate gates**. Require explicit timezone/NULL rules, valid User/Employee/Company binding, role permission and the installed assignment-intent patch where applicable. Include local ToDo edits and transfer conflicts.
10. Exercise native Event creation with confirmed source timezone/all-day rules. Test Project identity/read permission, omitted/NULL relations, unknown and true recurrence, local edits and incomplete existing time groups. Raw participants/resources must not create native user links.

## Runs and recovery

11. Check source instance, fingerprint, mapping/rule content and service revision at execution. Inject stale approval, concurrent publication, source change and missing permissions; no unapproved checkpoint may commit.
12. Simulate failure between records. Verify acknowledged records survive, unacknowledged changes roll back, replay stays idempotent and stale claims cannot commit. Polling must not begin simply because mappings exist.
13. Treat live HERO requests, production-like worker runs, data recovery and target permission acceptance as **separately authorized** tests, with their own results and rollback evidence.

For every case record `PASS`, `FAIL`, `BLOCKED` or `EXECUTION_UNVERIFIED` and distinguish product defects from a missing deployment, permission gate or test harness problem. See [Run checkpoints](run_checkpoints_1.0.md) for the earlier scoped evidence.
