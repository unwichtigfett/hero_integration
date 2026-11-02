# Code review remediation

Scope: the eight findings reported against release commit
`b8e8c22d77357f1b56554be7778f6b8fc6cc358a`, reconciled with development
baseline `c7ce2a1dd3daa2804487457ad34f21d31061c08e`.
The release object was unavailable locally. This report describes development
changes and does not certify that release commit or a deployed site.

| Finding | Development disposition |
| --- | --- |
| CR-01 | Separate Draft and Revision query hooks bind the DocType internally and accept a user-only call. Unsupported DocTypes fail closed. The installed Frappe v16 already supplied `doctype`; the wrappers also remove that version-dependent assumption. |
| CR-02 | Generic Sync Hero reconstructs the exact approved read plan before transport. The plan must select the bound ID and field for the matching source/object type and mapping definition/revision. A pre-read proves record membership and checks an existing remote baseline; a post-read must equal the transformed mutation input. Only the canonical stored field fingerprint is acknowledged. An ID response alone is insufficient. |
| CR-03 | Preview, API execution and the domain gate allow only `LOCAL_CHANGED`. Remote changes, mapping changes and reconciliation states cannot reach mutation transport. Acknowledgment must also match the reviewed local fingerprint. |
| CR-04 | Field binding resolution compares immutable identity and returns current acknowledgment/revision/reconciliation state. Duplicate-insert resolution uses the same check. A create/acknowledge/new-migration-run regression preserves the existing baseline. Object lifecycle reconciliation is not silently cleared or authorized. |
| CR-05 | Both maintained branches trigger CI. GitHub runs full offline and site suites with discovery floors and real exit codes. Woodpecker's existing strict gate remains; deployment now depends on its success. The two-connection reservation tests are enabled only on disposable CI sites. Required branch protection remains an external repository setting. |
| CR-06 | The maintained authorization contract is System Manager plus personal owner/actor isolation. The Administrator account is not the sole mapping administrator. No new role or broader access is introduced. |
| CR-07 | AUTO_SYNC continues to reuse the existing approved migration runner. B8 stays a separate bound-only preflight with its unproven provider/writer disabled. Both families now share one reservation boundary; repeated migration passes are not advertised as incremental delta synchronization. |
| CR-08 | Legacy and B8 claims use the same setup advisory lock and transaction-held Singles row lock. Current locking reads prevent stale snapshots from permitting concurrent claims. B8 never commits on the caller's behalf. |

## Writeback compatibility and limits

The generic readback deliberately uses the approved materialized query and its
exact variables. It does not widen the query, invent an ID filter or enumerate
additional unapproved pages. If the bound record is outside that request's
result, the pre-read blocks before mutation. Review a suitable read contract
or use an already approved native send flow.

Acknowledgments produced by the former ID-response hashing cannot establish a
canonical remote field baseline. They require explicit reconciliation; this
change does not rewrite or reinterpret existing fingerprints.

Mutation transport is never retried. A timeout, missing/duplicate record,
GraphQL error, omitted field or mismatched post-read leaves the binding
unconfirmed. A pre-read is not an external compare-and-swap transaction: HERO
can still change between reads and mutation unless the external API provides
an additional concurrency contract. No such contract is invented here.

## Evidence boundary

Offline domain/API/CI-gate tests exercise rejection before transport, exact
readback, typed fingerprints, resume identity, permissions and reservation
ordering. Node tests and shell syntax checks cover the neighboring assets and
CI scripts. The reservation suite includes real two-connection MariaDB tests
for B8/B8 and legacy/B8 races, guarded by
`HERO_RUN_RESERVATION_TEST_SITE` and intended only for disposable sites.

The editing environment cannot reach the site database or process manager.
Live MariaDB races, hosted CI, actual HERO execution and deployment remain
`EXECUTION_UNVERIFIED` until their respective runs succeed. Offline success
must not be presented as those runtime results.


## Field writeback follow-up

The follow-up review of `ed2238d842dd611e836c61e52a728da7a3fc454f`
identified two further field-writeback defects, addressed together:

- **R-02:** Preview fingerprints include the confirmed source instance,
  binding revision and endpoint fingerprint. The endpoint must match the
  approved snapshot. The transport revalidates this nonsecret proof before
  pre-read, mutation and post-read. A foreign or unconfirmed configuration
  cannot construct a client; reconfirming a changed connection invalidates
  the old preview. Source confirmation reuses the Settings/runtime rule.
- **R-01:** Form field writeback delegates to the same verified Sync Hero
  domain and transport. The already-unwrapped mutation result is handled
  correctly, but ID matching alone never means success. Audit success follows
  matching field readback and binding acknowledgment. Missing/ambiguous field
  bindings or an unavailable approved runtime/readback plan block sending.
  The UI disables ineligible confirmation and displays its reason.

Regression tests use the actual client response shape and cover successful
readback, wrong source, unconfirmed source, source switches between I/O steps,
changed preview provenance, timeouts, mismatched post-read and missing plans.
They make no real HERO requests. The new source guard applies to these two
field-writeback entrypoints; this is not a new acceptance claim for native
Send adapters or the separate manual GraphQL mutation endpoint.

Offline verification and independent review do not replace hosted CI or an
explicit target-site/HERO acceptance run. Branch protection remains an
external repository configuration requirement.


## Source changes during writeback

Source capture and each transport revalidation now use a separate read-only
connection to the site's primary database. One statement reads Settings and
the encrypted token from the same fresh snapshot. The connection closes before
any HERO request; the caller's database transaction is neither replaced nor
committed. Read or decryption failures block transport.

This detects source changes committed before each validation read, including
when the request retains an older MariaDB REPEATABLE READ snapshot. It does not
serialize a later configuration change with an external HTTP operation.

`test_writeback_source_frappe.py` reproduces a concurrent committed source or
confirmation change during the pre-read and requires rejection before mutation.
A positive control changes only the timeout and verifies that transport uses it
while the request still sees its original snapshot. The tests use separate
MariaDB connections and mocked HERO clients; both CI runners enable them only
on the disposable site selected by `HERO_WRITEBACK_SOURCE_TEST_SITE`.
Local offline tests do not establish execution of this database race test.
