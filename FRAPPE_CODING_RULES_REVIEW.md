# Frappe_Coding_Rules whole-app review — RC16

- **App / path / revision:** `hero_integration` at
  `/Users/luismann/PycharmProjects/hero_integration`,
  `928a004fdc95c4f915e389a2a5f52f7bd54e5ee7`.
- **Working tree at review start:** only pre-existing untracked `__pycache__`
  directories; no tracked source edits.
- **Framework:** declared Frappe `>=16.0.0-dev,<17.0.0` in `pyproject.toml`.
  The installed Bench revision was not inspected in this static review.
- **Scope:** whole app (`/proof_app`), including Python, JSON, Desk assets,
  packaging, hooks, tests and existing documentation.
- **Effect:** `READ_TARGET`; this report and `AGENTS.md` are separately
  authorized `WRITE_REPORT` / project-instruction changes. No app import,
  test, build, migration, installation, deployment or network call ran.
- **Validation level:** `STATIC`; runtime status is
  `EXECUTION_UNVERIFIED`.
- **Status:** `FAIL` — confirmed mandatory framework/project-contract
  violations are listed below. This does not claim a live HERO, browser,
  fresh-install, upgrade or deployment result.

## Confirmed mandatory findings

| ID / priority | Rule and authority | File:line | Evidence and consequence | Smallest correction | Verification needed |
| --- | --- | --- | --- | --- | --- |
| PA-001 / High | `FCR-API-003`, endpoint-contracts “Methods, authentication and access”; `C05` CSRF; **FRAMEWORK_REQUIREMENT** | `hero_integration/api.py:15`, `:406`, `:540`, `:582` through `:1098` | All whitelisted methods use bare `@frappe.whitelist()`. The reviewed Frappe-v16 contract permits GET/POST/PUT/DELETE when `methods` is omitted. This includes writes, queueing and `execute_graphql_service` at `:1098`, so an authenticated System Manager can reach a mutating command through a non-POST method. `only_for` does not make a GET CSRF-safe. | Restrict every mutating RPC to its intended unsafe method, normally POST; retain safe read RPCs only on their deliberate read contract. | Static endpoint-method regression plus isolated HTTP-method/CSRF checks; do not call HERO while testing. |
| PA-002 / High | `FCR-TX-001`; quality `B05`/`B06`; RC16 concurrency contract; **PROJECT_CONTRACT** | `hero_integration/services/migration.py:92-107`; `hero_integration/api.py:27-43` | `_claim_run` reads then writes the Single `active_run` without a row lock or conditional update. Distinct manual starts have distinct run-name job IDs, so `deduplicate=True` cannot serialize them. Concurrent workers can both observe an empty state and both claim a run. | Use one transaction-safe claim primitive with an explicit lock/conditional state transition and a testable loser result. | Isolated two-worker/race test, including scheduler and manual start paths. |
| PA-003 / High | `FCR-TX-001`; quality `B05`/`B06`; RC16 no-concurrent-mutation contract; **PROJECT_CONTRACT** | `hero_integration/api.py:1110-1119`; `hero_integration/hero_integration/doctype/hero_mutation_audit/hero_mutation_audit.json:4` | `frappe.db.exists({"status": "Approved"})` followed by inserting an audit row is a check-then-insert race. The only unique audit field is the random `correlation_id`, not one active-mutation key. Two confirmed requests can therefore both pass the check before either insert commits. | Add a transaction-safe single-active-mutation guard with durable recovery semantics; keep the existing no-retry outcome policy. | Isolated concurrent confirmation test proving at most one transport attempt. |
| PA-004 / High | `FCR-TX-001`; RC16 relationship/partial-write contract; **PROJECT_CONTRACT** | `hero_integration/services/migration.py:315-321`, `:453-497` | `_upsert` inserts the ERPNext target before `after_insert`. A `HomePhoneConflict` or `CustomerAddressError` is caught and converted to `Result("CONFLICT", ...)`, so the surrounding run continues and may commit the already inserted target without its integration link/required relation. | Make target, relationship and Link creation one atomic unit, or explicitly compensate only the app-created target before returning a handled conflict. | Isolated relation-failure test asserting no target, link or partial relation survives. |
| PA-005 / High | RC16 “every offered setting is effective”; quality `M54`; **PROJECT_CONTRACT** | `hero_integration/api.py:406-537`; `hero_integration/services/polling.py:14-126`; `hero_integration/sources/hero.py:32-66` | `polling_timezone`, filters, bootstrap, page size, pagination strategy, backfill, full inventory, reconciliation, retry limit/backoff, timeout, rate limit, concurrency and notifications are persisted/validated but have no production runtime readers. The active reader still uses fixed `PAGE_SIZE = 100`; `polling.py` only reads mode, window, interval and sources. | Either wire each exposed control into the authoritative scheduler/reader path or remove it from the available settings contract. | Focused isolated tests showing each retained setting changes the applicable runtime plan/behavior. |
| PA-006 / Medium | security `K01`; RC16 bounded pagination contract; **PROJECT_CONTRACT** | `hero_integration/services/polling.py:118-126` | `monitor()` calls `_count_pages()` until a short page. There is no configured page/time bound, repeated-offset guard, rate pacing or cancellation point. A continuously full/drifting source can keep the scheduler worker occupied indefinitely. The counter-hypothesis that `HeroSource` supplies a trustworthy terminal cursor is refuted: it derives the next offset locally whenever a page has exactly 100 rows. | Apply an explicit persisted bound and progress guard before another page request; record a bounded/incomplete result rather than loop indefinitely. | Mock transport test with repeated full pages and a bounded expected stop. |

## Unverified candidates and recommendations

- **UNVERIFIED:** declared Frappe compatibility is v16, but the installed
  framework revision and actual Bench Python/Node matrix were not inspected in
  this review. This is not a finding against the declaration.
- **UNVERIFIED:** the registered hook and patch paths exist as static files,
  but dotted-target import resolution was not executed because app imports are
  outside `/proof_app`.
- **RECOMMENDATION:** retain the existing static Desk contract checks and add
  narrowly scoped browser checks after the candidate can build; the theme
  change itself is source-covered but not rendered here.

## Applicability and coverage

| Matrix area | Status | Static evidence / result |
| --- | --- | --- |
| Identity and version | APPLIED | `pyproject.toml`, `hooks.py`, `modules.txt`; declared v16 range, installed revision unverified. |
| Structure and packaging | APPLIED | 104 Python files, 18 JSON files and 7 Desk-JS files parsed/read; canonical package, module, DocType, page, sidebar, patch and public-asset locations match the existing scaffold. |
| Architecture and style | APPLIED | Existing `client`, `sources`, `services`, `integrations` and DocType responsibilities were traced; no new architecture is recommended. |
| DocTypes and lifecycle | APPLIED | 13 source DocTypes are in the registered module; System Manager permissions and controller validation were inspected. No virtual or custom DocType mechanism is present. |
| Permissions and APIs | APPLIED | 41 `api.py` whitelist targets have a System Manager guard; `list_outputs` has an equivalent role check. PA-001 remains. Native DocType REST is limited by source permissions. |
| Data integrity and transactions | APPLIED | Document writes, direct patch writes, preview branches, deletion hook and Link uniqueness were inspected. PA-002 through PA-004 remain. |
| Jobs and concurrency | APPLIED | Hourly scheduler, queue calls and run/mutation guards were inspected. PA-002, PA-003, PA-005 and PA-006 remain. |
| Extension compatibility | APPLIED | `before_install`, `after_install`, `after_migrate`, hourly scheduler and `before_delete` doc events are registered; no override, monkeypatch, permission-query or class-extension hook is present. |
| Files and secrets | APPLIED | No uploads/filesystem writers were found. Outbound GraphQL uses HTTPS, allowlisted host, disabled proxies, TLS and no redirects; token retrieval uses `get_password` and source responses are redacted. |
| Frontend and rendering | APPLIED | Desk pages/assets and RPC targets were inspected; no `innerHTML`, template-rendering or executable-string sink was found. Backend guards remain authoritative. Browser rendering is unverified. |
| Migration and exports | APPLIED | One registered priority patch, source DocTypes, empty fixtures and mapping exports were inspected; migration was not run. |
| Tests and observability | APPLIED | Relevant unit/contract tests and redacted logging/output paths were inventoried. No test was executed. |
| Documentation and delivery | APPLIED | README, installation, feature/mapping matrices and manual acceptance were reviewed. They preserve separate live, installation, browser and release gates. |
| Special mechanisms | APPLIED / NOT_APPLICABLE | Outbound GraphQL and scheduler were reviewed. No portal, website route, report, print/PDF, webhook, realtime, cache hook, server script or client-script record mechanism was found. |

## Checks performed

- Read the installed `Frappe_Coding_Rules` 1.1.0 `proof-app`, app-dev,
  file-structure, API-conformance, code-review, UI-design and technical-writing
  skills with their applicable contracts.
- Performed static repository inventory and parsed all Python ASTs and JSON
  definitions. This is source inspection, not an app import or test execution.
- Traced hooks, patch registration, DocType permissions/unique fields,
  whitelist guards, Desk RPC call sites, outbound transport and write paths.

## RC16 release-gate relation

This static review neither replaces nor satisfies RC16's focused tests,
GitHub publication, fresh install, upgrade, target deployment or authenticated
browser verification. The theme commit `928a004…` remains the reviewed source
revision; a repair changes that revision and requires targeted re-review plus
the affected test/install/deployment evidence.

## Next safe step

Decide whether to authorize a narrowly scoped RC16 repair for PA-001 through
PA-006. Start with PA-001 and PA-003 because they protect externally effective
mutation entry paths; make no HERO request while adding isolated regressions.

## RC16 focused re-review — candidate `3457ae69a80419c7c633edbbc57cc370a4156f58`

This is a task-focused follow-up to the historical static `/proof_app` report,
not a new whole-app audit. It reviewed the changed APIs, hooks, DocTypes,
transaction boundaries, polling reader and their focused regressions.

| Finding | Follow-up result |
| --- | --- |
| PA-001 | CLOSED: POST-only decorators are exercised through Frappe's actual method validator. |
| PA-002 | CLOSED: two independent DB connections admit one run and persist a Blocked loser; no queue/HERO call is needed for this proof. |
| PA-003 | CLOSED: the unique active reservation is committed before any possible mutation transport; native race test proves one owner. |
| PA-004 | CLOSED: the target/relation/Link unit rolls back to a savepoint on handled relation failure. |
| PA-005 | CLOSED: retained polling settings are normalized by `runtime_plan` and passed to the configured reader/client; unsupported source abilities are rejected rather than invented. |
| PA-006 | CLOSED: page count is bounded, rate-paced and checks offset progress; incomplete scans are recorded rather than looping. |

Additional focused correction: the settings API now rejects CURSOR and timeout
values above 120 because the approved V1 source contract cannot execute them.
The app-wide wildcard update hook first scopes to a matching app-owned Link and
only writes a redacted local observation. It does not schedule an outbound
action. Browser rendering, real HERO transport, GitHub fresh installation,
upgrade and deployment are outside this read-only re-review and retain their
separate gates.
