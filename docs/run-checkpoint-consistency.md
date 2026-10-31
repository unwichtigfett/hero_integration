# VPS run checkpoint baseline and consistency

## Baseline selection

Step: `HERO_VPS_RUN_CHECKPOINT_CONTINUATION_08A`.

The inspected Bench checkout was clean on `hero_int_dev` at
`1a6ef11f45a662c1c160bf2f5d08f14c82632c29`. Its ancestry includes `c02d7b44`,
the completed business-number work, partial-project protections and the latest
confirmed-measure resolution. The other remaining local branch,
`work/polling-hardening-01`, diverges at `562fd0f8`; its name does not establish
that its implementation supersedes the current source.

The selected revision already owns approved saved reads, Mapping Root exchange
locks, runtime-plan generations/CAS, scheduler gates, source binding and Run
leases. These mechanisms are extended rather than replaced. The historical
step-08 report establishes requirements only; its unavailable Mac revision is
not a prerequisite or the source authority.

Development uses `/home/frappe/worktrees/hero-checkpoint-08a` in detached HEAD,
created from the inspected revision. No new branch, merge, rebase, reset, push,
Bench source change, site migration or deployment is part of this step. Local
commits remain reachable through the retained worktree HEAD.

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

New Run acceptance binds a versioned `checkpoint_contract_json` on the existing
Run. It covers site, source instance, confirmed binding revision, actor, action,
mode, primary reader and optional Customer dependency. Each reader binds its
service/mapping revisions and content, schema identity/content and the complete
selected active field-rule fingerprint. Credentials are never stored in this
evidence. An old site schema refuses new runs rather than silently dropping it.

The existing root exchange mutex and selected rule row locks fence each
acknowledgement. The existing active-run slot, owner, monotonic `started_on` and
live lease reject stale workers. A target, its identity links/relations, record
result and checkpoint commit together. Conflicts and interruptions roll back
unacknowledged writes; acknowledged records survive. Valid checkpoints renew
the existing lease.

Reader preflight and postflight use fresh read snapshots and release transactions
before HTTP. MONITOR additionally rechecks the canonical runtime generation,
source configuration, reconstructed reader/schema and selected rules after
HTTP. Its observation is incomplete coverage, never a business-data checkpoint.
No legacy offsets or unsupported checkpoint versions are promoted to trusted
state. Resume verifies the original evidence and replays from zero through
existing identity links; it never rebinds a changed contract silently.

Query/Mutation separation, MISSING/NULL behavior, Project title immutability,
and the existing fail-closed bound-only AUTO_SYNC writer remain unchanged. A
complete delta provider and distributed transport budgeting are not introduced.

## Verification boundaries

Offline regression tests cover contract drift, legacy evidence, owner/claim/lease
replacement, post-HTTP activation/rule/source changes, replay and blocked-run
outcomes. The existing generated-Task fixture now supplies source binding,
endpoint evidence and a genuinely terminating page sequence; native read
validation no longer holds rule locks across transport.

Opt-in `test_run_checkpoint_frappe` uses the already authorized development
site, actual ERPNext Customers and real MariaDB transactions. Only the Run
fixture uses a connection-local temporary table, avoiding a site schema change.
It verifies atomic target/checkpoint acknowledgement, rollback, interruption,
valid continuation, concurrent field-row updates and the shared publication
mutex with separate database connections. Synthetic fixtures are removed by
exact test-owned identities. These checks do not claim a deployed-schema or
live HERO end-to-end acceptance test.

The new Run field remains unapplied to the preserved site. Installing the
reviewed app schema and full queued-worker acceptance need a separately
requested development integration step. No production migration is authorized
or performed here.

## Verified result for this step

- Focused Python 3.14.10 regression: 327 passed, 75 subtests passed, exit 0.
- Authorized Frappe 16.54.0 / ERPNext 16.53.0 site checks: four passed, exit 0.
- Two independent MariaDB connections exercised both selected-rule row fencing
  and the mapping publication mutex.
- The actual Run reader released its existing advisory lock before the injected
  transport callback; no live HERO request was performed.
- The site retained its original Run schema. No synthetic test Customers or
  field rules remained after cleanup.
- Query/native-field/Task authority, business-number and Project-title
  regressions remain passing. A shared fake-module fixture now restores its
  parent-package alias as well as `sys.modules`, preventing order-dependent
  tests from patching the wrong executor module.
