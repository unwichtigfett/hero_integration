# Configuration

> This guide explains configuration, not authorization to execute. Verify the source binding, active mapping and service revisions, schema fingerprint and permissions before any run.

`HERO Source Instance` is the stable identity namespace paired with every
`ProjectMatch.id`. Keep the explicit `default` value only for the original
source. Give a separate HERO source its own confirmed value.

After installation, open **HERO Integration Settings** as a System Manager.

## Mapping administration permission

The mapping administrator is a user with the Frappe **System Manager** role;
this is not restricted to the built-in user named `Administrator`. Such users
may create, edit, import and explicitly activate mappings after all schema,
revision and target-write checks pass. They need write permission on each
selected target DocType; child tables are checked through their parent DocTypes.
The role does not bypass these checks or authorize a HERO mutation by itself.

Personal Mapping Drafts, revisions and their Version records remain private to
the owning user, including when another user also has System Manager. A
publication must use the personal draft API and its current revision checks.

## Connection and source binding

1. Enter the HTTPS GraphQL endpoint.
   {{ https://login.hero-software.de/api/external/v7/graphql }}
2. Add exactly that hostname to **Allowed HERO Hosts**, without a scheme or
   path. Use one hostname per line.
   {{ login.hero-software.de }}
3. Enter your HERO-API token in **HERO API Token**. Frappe stores it as a
   `Password` field. The app never copies it into a migration run or queue job.
4. Keep the default timeout or choose a whole number between 1 and 120 seconds.
5. Enable **Confirm Source Binding** and save. HERO exposes no account
   identity to check. Every change of endpoint, token or source instance
   therefore opens a new binding revision. Runs, MONITOR, AUTO_SYNC and
   Pending Sync stay blocked until a System Manager confirms it. The token
   never serves as an identity.

When **Enabled**, the API URL and an API token are set, **Save Settings**
loads the live HERO schema at once. This step verifies the token. An invalid
token shows an error. A valid token stores the schema as a validated snapshot.

The app then reloads the schema every **Schema refresh (minutes)**: 10 through
240, default 60. An unchanged schema keeps its snapshot and fingerprint. Only a
changed schema creates a new snapshot. The schedule refreshes only a verified
token, so an invalid token never causes repeated HERO requests. **Reset**
discards unsaved changes in the form.

One HERO account belongs to one business, so there is no Company setting.
Imported Projects use the ERPNext **Default Company** (Global Defaults). A site
with exactly one Company uses that Company. The app checks that the Company
exists, is readable by the current user, and is not disabled.

Start a Preview in **HERO Migration Center** before an execution run. A preview
creates source-link records. Execute creates only records with a complete,
explicitly activated mapping. The app compares existing records field by field
against the last acknowledged baseline:

- HERO changed, Frappe unchanged: the app writes the HERO value.
- Frappe changed, HERO unchanged: the app keeps the local value and its old
  baseline. A later HERO change of that field becomes a conflict.
- Both changed to the same value: converged. The baseline advances without a
  write.
- Both changed to different values: conflict. Nothing is overwritten.

A field missing from the HERO response is neither a change nor a clear. The app
compares an explicit HERO `null` or empty value but never writes it (no-clear
policy). Nothing is ever deleted.

HERO data that ERPNext would reject does not stop a run:

- The app leaves out an email address ERPNext rejects and imports the Customer.
- A phone text such as `+49 151 … Mieterin` keeps its first valid number. The
  full text becomes a comment on the Customer.
- The app leaves out an incomplete primary address and imports the Customer.
- An address without a HERO country gets Germany.
- Project titles know the project types PV, KLIM, ELEK, SONS, ABS and DS
  (by code or name). Another type blocks the Project and names its value.
- `ProjectMatch.id` 0 means no Project.
- An Event without a title gets its event type as subject.
- The app rolls back any other record that ERPNext or its database rejects,
  alone, and marks it `BLOCKED_RECORD`. See the Error Log. A blocked Customer
  blocks only its own Projects.

Customer IDs use the exact `Customer.nr` when it is a valid, available ERPNext
name. `2222` stays `2222`, and `0022` keeps its leading zeros. `customer_name`
remains the person's or company's name. The existing
`custom_hero_customer_number` mapping preserves the original string, even if a
legacy mapping specifies Trim. A missing or unsuitable customer number gets a
`HERO-CUSTOMER-<UUID>` technical ID with an explanation, not a competing
business number. Unrelated manual customers keep the site's naming settings.

New HERO-linked Project document names use the confirmed trade and exact `ProjectMatch.display_id` with the reserved `-H` suffix, such as `PV-3333-H` or `ELEK-1769-H`. This ERPNext document name differs from the protected server title (`project_name`) and the integration's external source identity. The shared trade catalog validates the
measure. When one value (code or name) is confirmed, the app ignores an unknown
other value. Two different confirmed trades require review. `project_nr`
remains separate, and the protected project-title rule still applies. A missing
or invalid project ID keeps native Project naming and reports the constraint.

Technical HERO IDs and source instances remain the identity keys. Reimports
reuse linked IDs, including existing customers with legacy numbers and legacy
projects. Changes or explicit clears of an established business number require
review. Omitted source fields do not count as clears. Number collisions never
merge, suffix, rename or cross-link records. A failed concurrent identity claim
rolls back the new target and its relations.

`hero_integration.services.business_number_preview.preview_rows()` reads
existing links, business numbers, collisions and native referencing records.
`write_csv(path, project_trades=None)` writes a new CSV. It never overwrites an
existing file or changes the database.

Projects use the stored mapped `custom_hero_project_type` or an explicitly
reviewed source-scoped configuration, for example `{"source-a": {"201": "PV"}}`.
The preview never guesses a trade from a project title. Without usable trade
evidence it marks the project for review.

The preview covers metadata-defined static and dynamic links, including child
rows and Single fields. External references and arbitrary text fields stay
outside its coverage. A positive `safe_rename` is preliminary, not permission.
An actual rename requires separate authorization, a database backup and a fresh
reference and collision review. The preview performs no renames.

Create a reviewed GraphQL schema snapshot before you configure mappings. The
Mapping Editor exports deterministic JSON packages with the snapshot
fingerprint and optional HERO-owned schema definitions. Upload validates the
fingerprint, source contract, Frappe metadata, permissions, transformations,
relations, and conflicts. It changes neither schema nor mappings.

The upload fills one checklist. It groups Custom DocTypes, Custom Fields,
relations, and mappings. Select the required entries and confirm **Apply
selection** once. The app creates only `HERO ` Custom DocTypes and
`custom_hero_` Custom Fields. It never changes native ERPNext fields, foreign
Custom Fields, permissions, or data. It creates schema before it activates the
selected mappings. A schema failure leaves the selected mappings inactive. The
app records the checked package and selection in **HERO Mapping Adoption**. A
System Manager can retry that failed record without changing its package.

Enable **Scheduled Sync** only after a successful manual preview and execute
run. The scheduler ticks every minute. It starts polling only when mode,
timezone window and the configured 1..1440-minute interval allow it. The
interval counts from the last successful and the last started poll, so the
scheduler does not retry a failing poll on every tick.

Polling runs one way, from HERO to Frappe. It waits while a migration run is
active. Out-of-range polling settings block polling with a visible Skipped
event. They never fall back to defaults.

A run is one queue job with short, per-record acknowledgement transactions.
Each target/identity update commits together with its Run checkpoint. The
existing run owner, monotonic claim timestamp, live lease and active slot fence
that commit. A failure rolls back only unacknowledged work. Previously
committed records remain.

Resume validates the original bound contract and replays from offset zero
through existing identity links. It never treats a stored offset as proof of a
stable HERO dataset. A legacy run without bound evidence requires a new
approved run. It never silently receives a new contract.

The Run's `checkpoint_contract_json` binds site/source, confirmed source revision,
reader and mapping revisions/content, schema identity/content and the effective
selected field rules. This includes the Customer dependency of a Project run.
Mapping-root and rule locks protect checkpoint acknowledgement, never external
HTTP. After HTTP, MONITOR revalidates runtime generation, source configuration,
schema, reader and rules before it records an observation. New runs fail closed
if the reviewed Run schema is not installed. These checks never imply a schema
migration or deployment.

Valid checkpoints renew the existing run lease. When a worker stops beyond the
lease window, another worker can replace it. The stale claim cannot commit.

The app checks every HERO read before the request. A document must contain
exactly one GraphQL query operation. All HERO clients in one worker process
share the configured request rate per HERO host. Retries cover only HTTP 429,
5xx and transport failures, with jittered backoff or a bounded Retry-After.

Offset pagination ends only on an empty page, because a short page can be a
server page cap. An offset is a scan position, not a watermark. A record
missing from HERO never counts as a deletion.

## Native Task and Calendar boundaries

Customer and Project remain the original native migration phases. The current source also contains separately guarded **native Task intake** and **native Event projection** paths. Their presence does not prove that a target site's read service, mapping, source instance or DocTypes are approved.

- Task creation requires source/Task identity, approved rules and a nonempty subject. Optional fields use baselines. Assignment and ToDo state, deadline NULL/timezone interpretation and completion are separate acceptance concerns. The optional assignment-intent framework extension must be verified before relying on protected ToDo operations.
- Calendar requires reviewed timezone and all-day semantics. Unknown recurrence flags are blocked; recurrence without a source rule is held. Raw partner/resource observations do not establish native participants or a Customer relationship.
- Saved, validated Query documents can be run explicitly through the read-only workspace executor. Mutation drafts are not execution grants.
- Automatic polling currently enumerates **Customer and Project** only, even though manual synchronization areas can show Task and Event projections.

Attachments and HERO Helpdesk-ticket transfer have no approved current source contract. General reverse synchronization is unavailable; restricted writeback actions are separate, opt-in operations.

See [Feature matrix](feature_matrix_1.0.md) and [Synchronization handoff](sync_handoff_1.0.md) for remaining gates.
The app infers no source contract or target mapping for them.
