# HERO Integration v1.0 release completion — 2026-11-19 target

Status: RELEASED on dev.tailbbed27.ts.net (vps1), 2026-11-18 UTC.
The assessment and candidate preparation below describe the pre-deployment state. The final execution record is documented at the end of this report.

## Candidate and provenance

- Candidate branch: `release/close-20261119` in `/root/workspaces/hero-release-close-20261119`.
- Integrated code commit: `b7cba6b349453a396e8df9cb5f4d1ce51865242b`.
- Base: Bench development `6eb2eaa5c536f27e8d5641e903dc73f86f2a98cb`.
- Preserved the four pre-existing Bench edits: Project workspace CSS/JS, workspace Node tests and German translation. The original working copy was not modified. Its binary diff remained identical, SHA-256 `73c64b04d1192d430b0fb8621b28b388f6ce255ba1a610ac4cbc63d614663ee2`.
- Integrated the source-scoped configuration importer from `2a953cdfd1a4586c141756001dbf4e839a24dd68`. This Opt-derived patch applied to the Dev base; its mutation parser import was adapted to the existing Dev `graphql_contract` owner. 74 focused importer/exchange tests passed. No Opt-wide materializer or CI architecture merge was performed.
- Older Dev and Opt release-candidate worktrees were inspected and retained. Their CI architecture branches are not substituted for this newer development base. No branches or foreign work were discarded.
- The accepted topology remains revision 01. This task explicitly supersedes the mandatory-address decision only within the approved release scope.

## Ten decision groups

| Decision | Status | Evidence / behavior |
| --- | --- | --- |
| 1. Release basis and preservation | VERIFIED | Explicit base, isolated candidate, preserved edits and bounded importer integration above |
| 2. Imported image categories | VERIFIED | Enabled mapped value first; native idempotent HERO-Import fallback before File insert/save; disabled fallback names retained; legacy unmapped empty values preserved |
| 3. Optional project addresses | VERIFIED | Existing values preserved; usable readable customer default only fills empty values; missing/incomplete addresses optional; old reqd flag removed idempotently |
| 4. One POST migration start | VERIFIED | Unused file_transfer.start removed; central api.sync_area_start HTTP-method/role checks, run persistence and enqueue-after-commit exercised |
| 5. Strict security, practical validation | VERIFIED | Native File saves replace bypassing File DB updates; mapping, authorization, identities and baselines retained; address organizational requirement removed |
| 6. Record isolation and retry | VERIFIED | Existing per-record savepoint rolls back rejected row and permits next row; existing identity/checkpoint/reset mechanisms retained; no new retry engine |
| 7. ERPNext and mapping authority | VERIFIED | Source-bound repeat does not duplicate; local mapped conflict preserved; unmapped Project notes preserved; approved read evaluation retained before category fallback |
| 8. Architecture limits | IMPLEMENTED | Existing owners extended; no framework, new workflow infrastructure or large-module decomposition |
| 9. Lead plus two agents | IMPLEMENTED | Disjoint file/API and address scopes; lead reviewed/integrated shared candidate and performed site acceptance; no recursive delegation |
| 10. Bounded acceptance and reproducible delivery | VERIFIED | Tests below; scoped local commits, clean candidate and release artifact; deployment separately gated |

## Changes

File import now calls native File saves for updates, metadata repairs and relinks, under the existing managed-target scope. Blob cleanup and commit ownership remain in the existing transfer service. A mapped category is accepted only after approved mapping evaluation. Missing/invalid categories do not waive an invalid mapping.

Project address metadata and server/client behavior share one optional rule. Existing native mapped Project address relationships retain their separate existing links and synchronization owner. No migration ordering workaround is needed.

Administrative `integration_events.list_outputs` adds a normalized `outcome` alongside detailed existing results: SUCCESS, FAILED, SKIPPED_DEPENDENCY, CONFLICT. Existing durable codes remain unchanged. PENDING_REVIEW/UNAVAILABLE summarize unresolved prerequisites; detailed result/action identify the reason. BLOCKED_RECORD/unknown codes summarize FAILED. Conflict and ambiguous/rejected identities summarize CONFLICT.

## Actual verification

Isolated site: `hero-close.localhost`, database `hero_release_close`, dedicated containers `hero-close-app`, `hero-close-db`, `hero-close-redis`, network `hero-close-net`. No production volumes, site configuration, HERO token or live source data were copied. No workers/scheduler or published ports are running for this test stack. Frappe 16.54.0, ERPNext 16.53.0, HERO Integration 1.0.14, Python 3.14.

Fresh-site setup initially lacked Company, Transit Warehouse Type, mapping-owned address field and default Address Template. Synthetic fixtures corrected these harness prerequisites. The interrupted ERPNext installation was resumed with its supported force-install option in this disposable database. These setup failures were not classified as product defects.

| Check | Actual result |
| --- | --- |
| Address offline regressions | 20 Python tests passed |
| Existing per-record/data rules | 24 Python tests passed |
| File transfer/mappings/categories/migration | 111 Python tests passed |
| Importer/config exchange/logbook exchange + output outcomes | 81 Python tests passed |
| Address and Project workspace client tests | 41 Node tests passed |
| Project import/update without address, duplicate prevention, local conflict and unmapped notes | PASS on isolated MariaDB via real migration._upsert |
| Failed row rollback followed by independent valid row | PASS on isolated MariaDB |
| Imported image missing category; fallback provisioning, File insert and native update | PASS on isolated site |
| Native mapped Project address relation, repeated without duplicate | PASS after synthetic field/template provisioning |
| Existing required metadata upgrade, repeated ensure_fields | PASS on isolated site |
| GET denial, Guest write denial, authorized POST handler, run persistence and actual Redis enqueue | PASS; exercised frappe.handler.execute_cmd and frappe.app.sync_database; no job appears before commit, one is queued after commit |
| Scoped diff/commit review | PASS |

Acceptance is six combined site scenarios covering the nine requested cases. Failed harness paths alone were retried; no full CI/E2E campaign was run. The runner asserts executed count and rejects skipped tests. Site tests use synthetic records and controlled mapping outputs/configuration prerequisites; no external HERO transport or production account was exercised. Full HTTP network/session transport and worker execution of a complete real HERO import are EXECUTION_UNVERIFIED. This is not a new assertion that those previously implemented paths were live-tested here.

Evidence logs: `/root/hero-release-close-20261119-evidence/`. The initial combined log records four passing scenarios and a fixture failure; relation, schema-upgrade and POST transaction logs record their targeted successful follow-ups. Source imports/exchange are covered by the lead test log. Worker-owned offline results are recorded above from their executed reports.

## Runtime and deployment

Existing Dev backend image: `hero-lead-connector-dev:1.0.0-4e7900d`, image ID `sha256:aec93e3ff6032993b9395ccb7b5aaa1d377e6ce7a0cb3c8e8e9ae1074c0b0b70`. Existing frontend: `hero-project-address-dev:6eb2eaa5c536`. The three changed service baselines and Project workspace JS inside the running backend matched commit 6eb2eaa by SHA-256. This is a scoped source comparison, not a claim that all image contents match Git.

Opt remains on `hero-opt-direct-config:1.0.14-20261116t182645z`. No running Dev/Opt container was replaced. The test app uses the existing Dev image with the candidate mounted at the app path; a new immutable release image has NOT been built. Runtime image labels alone are not deployment proof.

The candidate Git tree is clean after integration/report commits. Original Bench retains its four uncommitted UI edits. Testsite contains only synthetic fixtures and queued synthetic jobs; no worker can execute them. It is retained for inspection, with sites in the test app's tmpfs (not a durable backup).

## Blockers, residual risks and final action

No confirmed critical release blocker remains in the approved changed paths. No demonstrated data loss, unauthorized write, uncontrolled overwrite or duplicate creation remains in the acceptance evidence. Unexecuted live HERO transport and production deployment remain explicitly unverified. Wider performance, UI and full-CI coverage are accepted follow-up scope, not additional release phases.

The final required action is explicit approval of the target deployment and target-site migration. Before deployment, build an immutable image from the candidate while retaining all installed apps (including lead_connector), then apply the usual target backup/migration/build procedure within that separate authorization. Do not deploy an older image or remove installed apps. A commit or this report is not RELEASED status.

## Final deployment execution — 2026-11-18 UTC

Following explicit deployment approval, the prepared deployment script completed
with FLOW_RC=0 and TEE_RC=0 on root@vps1 for dev.tailbbed27.ts.net.
The deployed source remains 3a10e7cf8d4d3b9ec8e632c0bf1d9baf5bce0a23
(code b7cba6b349453a396e8df9cb5f4d1ce51865242b); this documentation update
does not rebuild or change the deployed image.

- Image tag: hero-integration-release:1.0.14-3a10e7cf8d4d.
- Image digest: sha256:25b144df80e72e9d0366b30055ad5d030754585aa743c76eecd83fdab314446c.
- Full database, public/private file and recovery configuration backup completed; all five manifest hashes verified on the host.
- Site migration and after_migrate hooks completed. Project Address is optional and configuration_import_ids exists.
- All six application services run the approved image; backend and websocket are healthy. Local and HTTPS ping returned HTTP 200/pong.
- The planned restoration of original maintenance/pause flags was brought forward after successful migration because the dependent frontend start waited on a backend healthcheck blocked by maintenance HTTP 503. The prepared script then completed normally; no application source or image was changed.
- HERO Integration version is 1.0.14. Maintenance is off; original scheduler settings are restored. HERO polling remains OFF and no active HERO migration exists. HERO_LIVE_IMPORT=NOT_EXECUTED.
- No complete database recovery was performed. No remaining deployment blocker was identified.

Full redacted execution evidence and return codes are retained on vps1 at
/root/hero-release-image-20261118/execution/RESULTS.txt. Protected backups remain
on the host and are intentionally excluded from Git.
