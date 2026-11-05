# Complete JSON mapping packages

The Mapping page remains a list with JSON import and export. A mapping file
can include all nine migration areas: customers, projects, calendar events, tasks,
products, documents, pictures, services and employees. Project histories belong to
the project reader and project migration; they do not require a tenth migration.

## Import contract

The Mapping page exchanges definitions only. Its Query tab accepts
`hero-integration-mappings-v1`: the schema fingerprint, field mappings, required
custom fields/DocTypes and read queries. Select a matching validated snapshot.
Its Mutation tab accepts `hero-personal-mapping-portable-v2` with direction
`FRAPPE_TO_MUTATION`. Both imports preview changes and require confirmation before
activating mappings. Mapping files contain no settings, full GraphQL snapshot or
mutation contracts. Configuration packages are rejected with a Settings hint.

Settings exchanges `hero-integration-config-v2`: the complete schema snapshot,
allowlisted operational settings, Query mappings and readers, optional
`mutation_mappings` and optional `mutation_contracts`. The latter carry the eight
registered update operations and Project logbook creation, covering nine areas;
documents and pictures share one file-update contract. Ten mutation profiles use
nine operation contracts. Contracts identify their exact mapping UUIDs, areas,
variable-only query and requested Draft/Active status. An explicitly confirmed
import creates missing contracts and approves requested Active contracts locally.
Existing differences block the import for reconciliation; identical contracts
are retained. No remote operation is executed during validation or approval.

Credentials, Password fields, URLs/hosts, notification recipients, execution and
deletion switches, runtime history and business data are excluded. The locally
confirmed source binding cannot be changed by an import. Install this application
revision before importing files using these extensions.

An explicitly confirmed replacement import provisions declared integration fields,
publishes the read mappings through their normal revision history, approves each
reader, and retires superseded read mappings. It saves mutation definitions as
personal drafts when imported through Settings. It does not enable execution,
start migrations, download files or issue HERO mutations. Prepared mutation
assignments appear in the existing Mutation list with Draft status; activate them
through the Mutation mapping JSON import after reviewing the required contracts.
Full configuration export includes the current user's prepared mapped updates as
well as active mapped updates. Query mapping export includes Query definitions only.

Before writeback activation, the target must have a separately reviewed, active
mutation contract and validated service. Sending then requires native permissions,
a unique source binding, the approved Query mapping, unchanged source and local
baselines, a fresh preview fingerprint and explicit confirmation. The update
profiles cannot create or delete entities. The separate Project logbook profile
permits only the explicit creation described below.

## Native data and relationships

Native ERPNext fields take precedence over additional integration fields. Customer
names use an explicit company/full/first/last-name fallback. Contact phone and email
values use native child rows. Secondary addresses, customer contact persons, bank
accounts and project addresses use source-bound native records with conflict
baselines. Equal names, email addresses or account numbers never authorize merging
unbound records. Repeated imports retain one source binding and preserve local edits.

Products retain read-only stock observations; importing a mapping never posts
inventory. Explicit source sales-price-list bindings identify native Item Price
records, their currency and whether a missing Price List may be created. Unknown
source price lists block the record instead of silently dropping prices. A mapped
product identifier remains distinct from the version identifier required by HERO's
update mutation.

Employee imports use the confirmed ERPNext company and the agreed joining date,
2026-01-01. New local users are active, receive only the Employee role and no welcome
email. HERO roles, account types and permissions do not grant ERPNext privileges.
Birth date and gender remain optional.

## Project logbook

The `PROJECT_LOGBOOK_V1` mutation profile maps native `Comment.content` through
`PLAIN_TEXT` to `add_logbook_entry.logbook_entry.custom_text`. It belongs to
`HeroProjectMatches`, alongside the separate Project update profile. Native
`reference_doctype`/`reference_name` prove the Project parent; the synchronized
source binding supplies `target_id`, and the schema-proven target is
`project_match`. JSON cannot supply remote IDs, author credentials, permissions
or arbitrary mutation input. HERO assigns the remote author and timestamps;
these members are not writable in `LogbookEntryInput`.

The Project form offers one **Send to HERO** button. Its server-built preview shows
all configured data changes and all eligible unsent native activities together,
with confirm or cancel. Field-level Project send buttons and the separate activity
selector are no longer mounted. Opening, cancelling or saving never sends a mutation. `comment_types` explicitly selects manual comments and
native automatic timeline events such as assignments and workflow changes.
The optional, closed `version_source` maps native `Version.data` to a change
summary, including status changes and child-row change counts. Hidden and Password
fields are omitted from that summary. The Project hook records native Versions
for opted-in, source-bound Projects when native change tracking is off; it defers
to Frappe when tracking is on. Managed HERO imports never create outbound change
activities. No new custom fields or duplicate timeline comments are needed.
Imported histories and private entries outside the Project timeline cannot become
new outbound activities.
Global execution, native Comment/Project permissions, source binding, snapshot,
contract scope and the approved Project/history readback are independently checked.

Each local activity (Comment or Version) has a stable audit reservation across mapping revisions and
edits. Confirmation never permits an automatic retry after an uncertain create.
A durable Project marker blocks its logbook import until the result is verified;
the returned history identity is persisted against the original Comment before
the subsequent readback. The inbound mapped importer retains the original activity's
content, author and native timestamps, preventing an import echo. Remote or local
changes raise a conflict. An unknown response leaves the Project logbook blocked
for manual reconciliation. Legacy raw-ID history import rejects Projects with an
outbound marker; these require the approved mapped Project reader.

Configuration import prepares all ten profiles as drafts and can import their
nine reviewed operation contracts. Mutation mapping import then activates the
profiles after confirmation. Neither import executes HERO operations.

The approved project query explicitly selects the history fields, collection
identity, visibility and context paths. One project per page allows its independent
history cursor to advance using the exact approved document. Even short history
pages are followed until an empty page; repeated identities and exhausted bounds
fail visibly.

Entries map native Comment content, subject, author, email and source timestamps.
Weather, attachment identities and the other declared history context are retained
in the existing context field. Restricted entries stay outside the shared project
timeline. Existing comments are source-bound, deduplicated and reconciled; local
edits raise a conflict instead of being overwritten. Preview validates histories
without creating a Project or Comment.

## Coverage limits

Coverage reports distinguish native mappings, declared relationships, derived
aliases, technical identities and deliberately excluded operational/security data.
They are not a claim that arbitrary recursive GraphQL objects should become ERPNext
fields. Authentication settings, external ACLs, transient capabilities and calculated
search strings are not duplicated. Warehouse ledger migration and generated sales
or purchasing documents are separate workflows.

Writable coverage follows the actual HERO input schema and the effective ERPNext
metadata. Output-only values, account identities, lifecycle operations, locale
regions without an explicit policy and relationships without a verified inverse
are not silently converted into writable assignments. Import validation is not
proof of successful live HERO writeback; no live mutations are required to import
or verify a mapping package.

## Identical snapshots and repeated imports

Mapping import compares saved old and selected target schemas before changing
snapshot references. Equal normalized schemas, recomputed fingerprints, source
and endpoint permit an atomic reference update of the current mapping/rule,
reader/service and mutation-contract graph. Approval and mapping revisions remain
unchanged; native validators recheck the group before and after the transition.
Different schemas fail with a schema-change error rather than a generic draft
error. Immutable histories retain their original snapshot references; historical
Customer-rule provenance accepts a different snapshot name only after the same
source/schema/endpoint proof. Private snapshot-bound Query drafts require explicit
reconciliation and are never rewritten silently.

Preview simulates and rolls back the reference transition. The confirmed import
rechecks it under locks. Query imports perform this DML after schema-provisioning
DDL, so a later failure can roll back the complete metadata transition. Unchanged
active Mutation mappings are retained without attempting to publish their unsaved
baseline drafts; only real changes need a saved publication revision.

## Compatibility with newer HERO schemas

The Mapping import/export paths also accept a newer snapshot whose unrelated
schema members changed. They compare the actual saved and incoming Query
selections, argument contracts/defaults, configured mutation input paths,
server-owned identities, required readback and response IDs against both schemas.
Required inputs, used enum domains, wrappers and relevant omitted defaults remain
part of the contract. Unused optional additions, descriptions and ordering alone
do not block existing mappings. A changed used dependency reports its path.

For a different global schema fingerprint, compatible mappings retain their
existing approved snapshot and fingerprint. The selected newer snapshot is the
compatibility-validation target; no unrelated new schema capabilities or approval
are granted. Incoming file fingerprints must match one of the two verified
snapshots before the importer selects the approved context. Query imports repeat
this proof after schema DDL and before mapping DML. The earlier reference-only
transition still applies to truly identical complete schemas. Multiple different
active approval contexts require separate imports.


## Combined Project delivery

Project identifiers and generated titles (`name`, `project_name`, naming series and
imported project number) remain local. Both update mapping validators and runtime
preparation reject naming writebacks. Version summaries omit these fields; native
Edit/rename comments cannot become outbound logbook entries. Other configured
Project data and eligible activities use the single confirmation.

Preview reads narrow the approved Project selection to the schema-proven singular
`project_match(project_match_id: ...)` root with the same object type and fields.
Only paging and ordering arguments can be replaced; business filters are never
silently dropped. Child logbook pages still complete and revalidate their source.
The outer Project list is not scanned. Singular-root dependencies also participate
in saved-schema compatibility checks. Identical transport reads are shared only
inside one preview, never across sends.

The actor-bound confirmation signs the ordered activity list and the native data
preview token. Data sends first. Each activity is then freshly authorized and sent
with its own native token. A separate activity delivery token tolerates only the
baseline hashes advanced by the preceding verified data send; content, Project
stamp, mapping, contract, source and target identities remain bound. A native legacy
phase save may provide an exact before/after stamp receipt: only that proven save
can advance the activity delivery context; a concurrent Project edit still blocks.
The ordinary activity confirmation always uses the current full fingerprint. No new activity
is added after confirmation. A failed or uncertain step stops delivery and reports
confirmed partial results; native durable receipts prevent duplicate logbook sends.

Project activity summaries omit read-only calculated totals, virtual fields and
fields with restricted permission levels. Each activity is labelled with its
native kind and timestamp in the combined confirmation.

A legacy Project Notes baseline is compared using its original Trim projection;
the native Notes baseline uses the current approved canonical projection. Missing
or inconsistent partial baselines remain conflicts. Only verified outbound
acknowledgement retires the replaced legacy hash keys. Customer payload changes
alone do not block an unchanged Project.customer relation when the synchronized
same-source identity exactly matches the freshly read HERO customer_id. This
exception never authorizes changing that relation or writing to the Customer.

HERO logbook readback accepts the exact sent plain text and HERO's `nl2br`
representation, which inserts `<br />` before an existing newline. Verification
removes only that added representation before comparing; deliberate blank lines,
literal tags and entities stay significant. Inbound echoes compare the original
HERO `custom_text`, independently of generated display headings. Identity, Project
parent and native activity checks still apply.

System Managers may reconcile an existing pending outbound receipt only after its
unique failed audit, original local payload, exact remote identity, Project and
acknowledged text hash are proven again. This operation updates local metadata in
the caller's transaction and cannot create another HERO entry. An in-flight audit
or a genuine mismatch remains blocked. The Project dialog closes and reports
successful delivery only after every confirmed step succeeds.


Changes to `Project.notes` are excluded from automatic logbook summaries: Notes
travel through the Project data mapping only. A Notes-only Version is not an
outbound activity; mixed Versions retain other eligible changes such as status.
Native manual Comments still transfer their own text unchanged. Existing native
Versions and previously created HERO entries are retained.

## Test imports with a record limit

In Settings → Testing, **Max. records per area** limits manually started
previews and imports to 1–100 source records per area. The default is five.
Execute imports still write ERPNext data; Preview keeps its preview behavior.
Full Migration applies the same limit separately to every area, so five
employees and five products may both be processed. Unchanged, skipped and
failed records also count toward the limit; related ERPNext documents do not
each consume another source-record slot.

The run freezes this setting at start. Its child runs and background slices
retain that limit; changing Settings affects new runs only. Reaching the limit
finishes the bounded test rather than automatically importing the remainder.

A HERO response can contain more records than the configured limit. Run details
therefore show each area's processed count and received count separately:
**5/5 processed · 26 received** means five employee records were processed,
although HERO returned 26. The run-list column **Created / processed** is not
a progress fraction against the total HERO inventory. Older test-preview runs
retain their historical HERO-request-budget display.

### Update safeguards and compatible snapshot rebinding

HERO person IDs resolve through synchronized Integration Links of the selected
source instance. Partner IDs follow the native Employee-to-User relation; user
IDs use their source-bound User link. Legacy ID fields alone are not identity
proof. The source context also applies when comparing writeback baselines.

Product updates protect the managed supplier row, including its supplier part
number. Local changes require reconciliation before any item or price write.
Old links without that baseline may adopt an existing row only when it exactly
matches the incoming supplier part number. Unmapped supplier rows remain local.
Without a stock-unit mapping, updates use the Item's current unit for price
validation.

Contacts with one shared primary phone/mobile row are split when the mapped
numbers diverge. Unmapped numbers remain unchanged, and saved communication
values are checked before confirming synchronization. Calendar native status
bindings retain their selected completion flag through the event adapter.

Compatible schema rebinding validates existing `SEND_TO_HERO` and legacy field
writebacks using their respective validators, including their source fields,
mutation documents and contracts. Those profiles do not need conversion to a
new mapping format merely to accept an equivalent schema snapshot.

Employee readbacks use the complete approved unpaged partner list once, with the
same 10,000-record and identity checks as employee intake. File readbacks unwrap
the selected connection edge nodes while retaining saved-query validation.
The deployment migration removes the two global uniqueness constraints from
legacy User person-ID observation fields; source-bound Integration Links retain
their unique source keys. A product rollback also discards both master caches so
subsequent records can recreate masters rolled back with the failed record.

Calendar writebacks interpret remote times with the remote all-day flag. Any
write of the start/end/all-day group verifies all three remote baselines before
sending, including members whose wire value is unchanged. Field aliases retain
the same semantics. Query replacement checks native parents after compatible
snapshot rebinding and remaps explicit mutation rule references to the actual
published rules in that area. File checkpoints restart on a mapping change;
scalar and collection price rules must use distinct ERPNext price lists. The
read-only Task author User link is provisioned on installation and upgrade.

A partial product writeback advances only confirmed field baselines. The whole
Item digest is renewed only when every owned field matches its individual
confirmed hash; subsequent imports check those hashes before applying upstream
changes. One-time native-field copies require the exact mapping revision changed
by that migration, its source-bound synchronized link and unchanged baselines for
both the legacy mirror and native destination. Missing evidence is left to normal
reconciliation, and rejected records are never copied. Project field delivery
also works without a configured logbook profile; invalid or ambiguous existing
profiles still block delivery.

An Item deleted in ERPNext is reported as a binding conflict instead of being
counted unchanged or recreated automatically. If any HERO project assignee cannot
be resolved, existing imported assignments are retained and the Project result
requires review. Other mapped Project data, logbook entries and native relations
still run; a fully resolved empty assignment list can close imported assignments.
