# GraphQL materializer

- Pure-Python package: `hero_integration/graphql_protocol`; provenance: `SOURCE_PROVENANCE.json`.
- Functions: deterministic operation rendering, typed values, selections, response decoding and request fingerprints.
- Dependencies: no Frappe site, database, HTTP transport or `hero_connector` runtime.
- Generated reads remain drafts until their mapping and service are approved and revalidated.
- Generated mutations do not grant write permissions or bypass source, audit and confirmation controls.

## Runtime boundary

`GraphQLOperationMaterializer` is the shared compiler for saved documents,
generated operations, analyzer templates, auxiliary reads and schema discovery.
`mapping_contract_v1.materialize_saved_query` and `materialize_saved_mutation`
validate the stored schema before compiling saved documents. Direct migration
pages require their schema snapshot; there is no raw-query fallback. Generated
pages are compiled again with the actual paging variables before transport.

`graphql_schema_cache` reuses compiled graphql-core schemas by exact snapshot
content within one process. Typed generation similarly reuses its immutable
schema representations. Each cache retains at most four entries; snapshots over
two million characters are processed without being retained. Mutable dictionary
inputs are serialized and detached before caching. Changed content creates a new
entry; invalid schema builds are not retained. Stored snapshot status, identity
and expected fingerprint are checked on every call, including cache hits.

No mapping approval, source-binding authority, actor, request variables, response
data or target state is cached. Workers build schemas on first use after startup
or eviction. Shared schema objects are internal read-only compilation state;
callers must not mutate them. Saved-document compilation does not build the
additional schema representations used only by typed operation generation.

Derived query projections also have a pure bounded cache: at most 16 plans per
live graphql-core schema, keyed by the exact original document and required/
preserved response paths. Schema keys are weak references, so these plans do not
retain evicted schemas. Documents or path lists above 65,536 characters bypass
this cache. Plans contain only projected text and variable names. Runtime values
are coerced and the emitted query is validated on every request.

Complete documents retain aliases, multiple root selections, named and inline
fragments, directives, variable defaults and nested input values. A document
containing several operations requires an explicit `operation_name`; the whole
document is validated before the selected operation and its transitive fragments
are emitted. Variables are coerced against the selected operation. Mutation
requests also require their request scope and retain the existing execution,
confirmation, source-binding and audit gates.

Typed operation generation supports nested arguments, interface/union selections,
finite deep selection paths and query batches with independent variable scopes.
Bounded schema discovery does not impose the same depth bound on a selected path.
`operation_template` creates schema-validated unsaved drafts without inventing
required runtime values.

Selection paths determine the requested fields. For example, choosing only
`product_id`, `base_data.name` and `base_price` generates a product query containing
exactly those three leaf fields. Required identity/lifecycle/dependency fields
still need to be selected by the owning migration contract. Versioned migration
mappings derive a smaller wire selection automatically from the same required
paths used for approval validation. This includes transformation dependencies,
native child relations, lifecycle flags, identity fields and logbook requirements.
Known native provenance/assignment inputs already present in the saved query are
retained as well. The stored approval document and its seal are not rewritten.
Manual queries, lookup contracts with unknown consumers and legacy fixed-shape
contracts retain their explicit selections.

Projection never adds fields, changes filters, paging arguments, aliases or
directive conditions. It narrows named/inline fragments and removes only variables
whose approved field selections were removed. Missing required fields fail before
transport. Fewer fields can reduce response size and server work, but HERO latency
still requires a real transport measurement.

Typed read batches can combine ordinary selections and interface/union selections
from the same snapshot by using one consistent schema representation. Batch
construction does not schedule requests automatically. Query and Mutation are
separate operation kinds; a document with several named operations still executes
one selected operation. Mutation grouping requires its own execution/audit design.

`query_draft` and `mutation_draft` now extract service metadata from the shared
GraphQL AST and validation boundary. The old `graphql_contract` backend and its
token parser have been removed. `mutation_binding_contract` retains only the
binding-specific restrictions and the established contract fingerprint format.
Historical query templates remain where saved-service compatibility and JSON
exchange require them; execution still passes through the materializer.

## Removed editor surfaces and retained exchange

The unused picker, suggestion, mapping-list/detail/binding-list, candidate preview,
new-definition seed/create, revision-history RPC, discard RPC, field-rule activation
and manual snapshot-inheritance RPC have been removed. Their unused implementation
modules `mapping_suggestions`, `mapping_draft_creation` and `mapping_rule_write`
have also been removed.

The currently used simple mapping workflow and its draft publication services
remain. JSON exchange retains portable identity enrollment, package previews,
revision-checked saves, import replacement and configuration adoption. The internal
draft discard/replacement lifecycle remains because imports use it. Revision
records remain accessible through native Frappe permissions. Site metadata and
field validation shared by imports remain available.

## Relationship realization after migration

`deferred_references` stores private source observations in `HERO Pending Reference`
and all dependency identities in `HERO Pending Dependency`, including dependencies
that already exist. Observations belong to the exact source, area, source identity
and originating run. A newer observation supersedes earlier open work without
discarding its recorded dependency IDs.

| Area | Initial import | Separate post-success service |
| --- | --- | --- |
| Customers | Customer scalar values and source observations | Primary/secondary contacts, addresses and configured native child relationships |
| Projects | Project scalar values and all observed customer/assignee IDs | Customer reference and native assignments |
| Tasks | Task scalar/lifecycle observations and project/author/assignee IDs | Project reference and native task assignments/lifecycle relationships |
| Employees | User/Employee observations and source identities | Employee–User association and the owned User object binding |
| Services | Item values and unresolved calculation rows retaining component IDs | Material/Activity Type calculation references and private attachments |
| Documents/pictures | Private files and observed Customer/Project identities | Target attachment after dependency and ownership validation |

Calendar resource/partner IDs remain source observations; the calendar adapter
does not turn them into native Event participants. Product classification, units,
prices and supplier-name rows retain their existing native scalar-derived setup;
they do not resolve external HERO identity dependencies.

Only a non-preview originating run in `Completed` permits realization. Queued,
running, blocked, failed, cancelled and conflict-completed runs cannot authorize
these writes. Existing local relationships and target edits remain protected by
their normal baselines. Generic observations also capture the original imported
binding and source hashes; the worker locks and verifies that proof before replay.
Employee, service and file workers verify their frozen source/target evidence.

Versioned Task/Project/Event workers use a dedicated reference/assignment path
instead of replaying scalar intake, Event time processing or Project logbook
processing. Event binding updates retain the native privacy/outbound checks and
carry forward unselected scalar baselines without acknowledgement. Unselected scalar
baselines are carried forward without acknowledgement. Customer reconciliation
restricts its target writes to contact/relation fields in the successful replay
context. Employee realization validates the imported baselines, creates/verifies
the owned User binding and updates the Employee–User association directly; it does
not repeat employee scalar mapping. Native ERPNext save hooks still apply.

Successful area completion queues `reconcile_completed_run`, which processes one
bounded page for that origin only. Dependency updates wake matching observations;
the scheduled `recover_pending` pass handles lost notifications. Worker validation
failures remain visible as conflicts. A missing dependency remains waiting.
Recovery reads the bounded pending page, successful origin runs and dependency
rows in groups. Source-scoped integration links, native target existence and
enabled User/object-binding evidence are deduplicated and loaded in chunks of
500 IDs per kind. Partner resolution retains its Employee/User ownership checks.
This snapshot only decides which jobs to enqueue; it is never passed to a writer
or retained across recovery calls. `resolve_one` still checks dependencies and
authority freshly within its own transaction. No native document saves, conflict
checks or per-record commits have been removed.
Failure handlers roll back and lock/re-read the committed reference state before
marking a conflict. A successfully committed, superseded or independently reviewed
observation is never reclassified by an exception from an after-commit callback.
Error logs retain the exception type and bounded code-frame locations without
exception values, local variables or source payloads. Generic source observations
must be nonempty JSON objects before any realization writes; missing observations
require a fresh import of the affected source record. They cannot be reconstructed
from a target document or treated as empty successful input.
Resolved observations clear their private source record while retaining dependency
identities. Signed download URLs and credentials are excluded from stored records;
service attachments refresh only the owning service identity, company and file
metadata/URL before download. They do not reload the service calculation.

## Imported Project logbook baselines

Mapped logbook entries keep separate source and persisted Comment hashes. The
target baseline is read after insertion or update, including native HTML
sanitization, default comment email and mapped timestamps. Repeating an import
therefore does not mistake its own native normalization for a local edit.
Unbound system comments, including migration-created Project creation activity,
are outside the source-scoped `project_history` comparison.

An older pre-save baseline can be repaired without rewriting the Comment only
when the source is unchanged, the original source and target hashes coincide,
and every selected field matches the expected native representation. The existing
narrow line-break equivalence remains supported. Actual local changes or an
unprovable baseline still require review; outbound activity receipts retain their
separate comparison rules. Relationship-only reconciliation does not replay the
Project logbook import.

## Full-import relationship completion

A terminal mapping child commits its reservation cleanup and advances its active
parent directly through the existing locked full-run executor. The next area
still receives its own queued job; no extra queue notification is needed between
the Project completion and Event creation. Duplicate notifications re-read the
current child under the full-run lock, and cancelled parents remain stopped.
Cleanup failures after a durable child completion still attempt this handoff;
a retry of a terminal child repairs handoff without repeating scalar import.
This removes a notification dependency but does not reconstruct the cause of a
previously lost job or restart cancelled imports.

Missing dependencies remain Waiting while areas are still being imported; ready
relationships may be realized after their own successful origin run. Completeness
is assessed only after every scalar area of the full import has finished. The
final pass processes one bounded page per job in dependency order: Employees,
Customers, Projects, Events, Tasks, Services, Documents and Pictures. Products are
already imported by then. Each stage has its own cursor and is checkpointed.

Both ordinary and bounded test full imports reconcile only their recorded child
runs, never an unrelated source-wide backlog. After the dependency stages, a
bounded final assessment marks still-Waiting observations as conflicts while
preserving their frozen source records. Existing conflicts are also counted. A
full import with unresolved relationships ends as Completed With Conflicts and
reports the count instead of appearing successfully complete with indefinite
Waiting entries. Failed/cancelled origins never acquire write authority.

`file_migration.retry_full_relationships(run_name)` restarts only this final pass
for an already finished full import. It requires System Manager and write access,
a current confirmed source binding, a complete scalar checkpoint and matching
frozen child/source associations. It preserves the child scope and test cohort;
no scalar import or fresh source enumeration restarts. It does not reset genuine
conflicts or reconstruct cleared observations. The operations project provides
`reconcile-opt-full.sh` for invoking it on the opt site after deployment.

## Runtime measurement

Set the site configuration key `hero_performance_metrics` to `1` to enable
aggregate instrumentation for migration runs and binding workers/recovery. The
default is disabled. A running outer operation retains its setting until exit;
later operations read the current setting. Each outer operation emits one JSON
record through `frappe.logger('hero_performance', allow_site=True)` with fixed
phase names, call/error counts and total milliseconds. Arguments, record/source
identities, documents, variables, credentials and exception messages are excluded.
Logging failure cannot change the operation's result or transaction outcome.

Phases cover read approval checks, compilation, transport, HTTP attempts, rate and
retry waits, JSON decoding, projection records, target upserts, bulk availability
and binding worker/page totals. Times are inclusive: nested phase totals overlap
and must not be summed. `hero.http_attempt` includes response-body reading;
`hero.request` includes client setup, retries and decoding. These aggregates are
diagnostics, not server-only HERO timings or SQL query counts. SQL counts require
a separate database profiler. `performance.collect()` provides the same scoped
in-memory aggregates to offline benchmarks without enabling log output.

## Migration and validation

Deploy the app with the normal site migration so the updated pending-reference
DocType, including `realization_started`, is installed. Pending legacy generic
observations without the original binding proof require a fresh area import.
Service checkpoints include migration version `service-migration-v2`; an old
checkpoint is rejected rather than silently resumed under the changed contract.

The offline runner is `ci/run-offline-tests.py`. Materializer tests cover typed
generation and complete-document semantics; exchange tests cover the retained JSON
interfaces. The opt-in `test_deferred_synthetic_frappe` tests use native Frappe
documents and MariaDB to check success gating, dependency arrival, local conflicts,
stale source proofs, origin isolation, Employee bindings and private attachments.
`test_exchange_synthetic_frappe` verifies Query field-rule and Mutation package
round trips through native draft/revision storage and a fresh target source context,
including identical re-exports and retained rule IDs. Its HERO transports are
blocked explicitly. These checks do not establish deployed-site, browser, live HERO
mutation or hosted CI results.

`test_graphql_schema_cache` covers warm-cache content changes, stale fingerprints,
revoked snapshot validation, independent source provenance, per-request variables
and mutation scopes, eviction, oversized entries and malformed snapshots.
`test_query_projection` covers response aliases, reused/transitive fragments,
directives, dropped variables, preserved IDs, live reader integration, mixed read
batches and projection-cache isolation/eviction. Native binding tests also reject
full Task/Project/Employee intake calls during relationship realization and check
that local scalar values and their old field baselines survive.


## Independent development and release lane

The materializer worktree is `/home/dev/codex-wt-graphql-materializer` on
`hero_int_dev_opt`. It is independent of the standard app checkout used by
`hero_int_dev`. The opt Docker stack is maintained in the operations project
`/home/dev/frappe-vps1-docker`; its project, data and Redis volumes are separate.
Two sites inside one Bench would share app code and are not used for this split.

A push through `pipe --opt start` runs GitHub Actions and the Woodpecker install
test before promotion from `hero_int_dev_opt` to `hero_int_opt`. Normal development
continues to promote from `hero_int_dev` to `hero_int_1.0`. The opt lane uses
`opt-v<version>` tags and can create its release branch on the first successful
promotion. Both lanes retain non-forced atomic branch/tag publication and reject
a diverged release branch. The shared standard test-site updater is not invoked
by the opt lane. Actual opt development-site deployment is a separate operation,
`pipe --opt deploy`, after the operations installer has run as root.
