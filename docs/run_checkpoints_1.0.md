# Run checkpoint baseline and consistency

> **Historical scoped test evidence.** The test counts, Frappe/ERPNext versions and DB checks below belong to the documented checkpoint implementation step. They are not proof of a newly executed suite on the latest branch, deployed target schema, live HERO request or full queued-worker recovery acceptance. Preserve the original evidence and verify current runtime separately.

## Baseline selection

The checkpoint contract builds on mechanisms the app already has: approved saved
reads, Mapping Root exchange locks, runtime-plan generations/CAS, scheduler gates,
source binding and Run leases. It extends these mechanisms instead of replacing them.

## Reconciliation at the selected baseline

| Guarantee | Initial classification | Evidence / remaining gap |
| --- | --- | --- |
| Source-instance binding | IMPLEMENTED_VERIFIED | Confirmed source revisions and endpoint-bound snapshots; source-binding tests |
| Reader/schema binding | IMPLEMENTED_UNVERIFIED | Saved service/mapping revision checks exist; no durable effective-content checkpoint |
| Effective active rule content | MISSING | Plans are loaded again while processing; the Run did not store their content evidence |
| MONITOR stale acknowledgement rejection | MISSING | Validation before HTTP, no validation after HTTP |
| Per-record target/checkpoint acknowledgement | MISSING | Run changes were saved inside one attempt-wide transaction |
| Run generation/ownership | IMPLEMENTED_UNVERIFIED | Monotonic claim timestamp, active slot and lease exist, but were not checked at every acknowledgement |
| Concurrent activation fencing | MISSING | Mapping locks exist; the checkpoint boundary did not consistently use them |
| Recovery and replay | IMPLEMENTED_UNVERIFIED | Unsafe offsets fail closed; previously committed acknowledgements require per-record transactions |

## Implemented contract

Each newly accepted Run binds a versioned `checkpoint_contract_json` on the existing
Run. The contract covers site, source instance, confirmed binding revision, actor,
action, mode, primary reader and optional Customer dependency. Each reader binds its
service/mapping revisions and content and its schema identity/content. It also binds
the complete fingerprint of the selected active field rules. This evidence never
stores credentials. A site with an old schema refuses new runs instead of silently
dropping the contract.

The existing root exchange mutex and the selected rule row locks fence each
acknowledgement. The existing active-run slot, owner, monotonic `started_on` and
live lease reject stale workers. A target, its identity links/relations, the record
result and the checkpoint commit together. Conflicts and interruptions roll back
unacknowledged writes. Acknowledged records survive. Valid checkpoints renew the
existing lease.

Reader preflight and postflight use fresh read snapshots and release transactions
before HTTP. After HTTP, MONITOR also rechecks the canonical runtime generation,
source configuration, reconstructed reader/schema and selected rules. Its
observation counts as incomplete coverage, never as a business-data checkpoint.
Legacy offsets and unsupported checkpoint versions never become trusted state.
Resume verifies the original evidence and replays from zero through existing
identity links. It never silently rebinds a changed contract.

These parts stay unchanged: Query/Mutation separation, MISSING/NULL behavior,
Project title immutability and the fail-closed bound-only AUTO_SYNC writer. The
contract adds no complete delta provider and no distributed transport budgeting.

## Verification boundaries

Offline regression tests cover contract drift, legacy evidence, owner/claim/lease
replacement, post-HTTP activation/rule/source changes, replay and blocked-run
outcomes. The generated-Task fixture supplies source binding, endpoint evidence
and a page sequence that really terminates. Native read validation does not hold
rule locks across transport.

The opt-in `test_run_checkpoint_frappe` runs against a development site with real
ERPNext Customers and real MariaDB transactions. Only the Run fixture uses a
connection-local temporary table, so the site schema stays unchanged. The test
verifies atomic target/checkpoint acknowledgement, rollback, interruption, valid
continuation and concurrent field-row updates. It checks the shared publication
mutex with separate database connections. Cleanup removes synthetic fixtures by
their exact test-owned identities.

These checks are not a deployed-schema test and not a live HERO end-to-end
acceptance test. Applying the new Run field to a site and full queued-worker
acceptance are separate steps.

## Verified result for this step

- Focused Python 3.14.10 regression: 327 passed, 75 subtests passed, exit 0.
- Frappe 16.54.0 / ERPNext 16.53.0 development site checks: four passed, exit 0.
- Two independent MariaDB connections exercised both selected-rule row fencing
  and the mapping publication mutex.
- The Run reader released its advisory lock before the injected transport
  callback. No live HERO request was made.
- The site kept its original Run schema. No synthetic test Customers or field
  rules remained after cleanup.
- Query/native-field/Task authority, business-number and Project-title
  regressions pass. A shared fake-module fixture restores its parent-package
  alias as well as `sys.modules`. This stops order-dependent tests from patching
  the wrong executor module.
