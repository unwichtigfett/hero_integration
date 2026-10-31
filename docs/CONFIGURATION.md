# Configuration

`HERO Source Instance` is the stable identity namespace paired with every
`ProjectMatch.id`. Leave the explicit `default` value only for the original
source; use a distinct confirmed value for a separate HERO source.

Open **HERO Integration Settings** as a System Manager after installation.

1. Enter the HTTPS GraphQL endpoint.
   {{ https://login.hero-software.de/api/external/v7/graphql }}
2. Add exactly that hostname, without a scheme or path, to **Allowed HERO
   Hosts**. Use one hostname per line.
   {{ login.hero-software.de }}
3. Enter your HERO-API token in **HERO API Token**. Frappe stores this as
   a `Password` field, it is never copied into a migration run or queue job.
4. Keep the default timeout or choose a whole number between 1 and 120 seconds.
5. Enable **Confirm Source Binding** and save. HERO exposes no account
   identity that could be checked, so every change of endpoint, token or
   source instance opens a new binding revision. Runs, MONITOR, AUTO_SYNC and
   Pending Sync stay blocked until a System Manager confirms it. The token is
   never used as an identity.

When **Enabled**, the API URL and an API token are set, **Save Settings**
loads the live HERO schema right away. This also verifies the token: an
invalid token shows an error, a valid one stores the schema as a validated
snapshot. After that, the schema is reloaded every **Schema refresh
(minutes)**, 10 through 240, default 60. An unchanged schema keeps its
snapshot and fingerprint; only a changed schema creates a new snapshot.
Only a verified token is refreshed on the schedule, so an invalid token never
causes repeated HERO requests. **Reset** discards unsaved changes in the form.

One HERO account belongs to one business, so there is no Company setting.
Imported Projects use the ERPNext **Default Company** (Global Defaults); a site
with exactly one Company uses that Company. The app checks that the Company
exists, is readable by the current user, and is not disabled.

Use **HERO Migration Center** to start a Preview before an execution run.
A preview creates source-link record. Execute creates
only records with a complete explicitly activated mapping. Existing records
are compared field by field against the last acknowledged baseline:

- HERO changed, Frappe unchanged: the HERO value is written.
- Frappe changed, HERO unchanged: the local value is kept and its old baseline
  stays, so a later HERO change of that field is a conflict.
- Both changed to the same value: converged, the baseline advances without a
  write.
- Both changed to different values: conflict; nothing is overwritten.

A field missing from the HERO response is neither a change nor a clear. An
explicit HERO `null` or empty value is compared but never written (no-clear
policy). Nothing is ever deleted.

HERO data that ERPNext would reject does not stop a run:

- An email address ERPNext rejects is left out; the Customer is imported.
- A phone text such as `+49 151 … Mieterin` keeps its first valid number;
  the full text is added as a comment on the Customer.
- An incomplete primary address is left out; the Customer is imported.
- An address without a HERO country is created with Germany.
- Project titles know the project types PV, KLIM, ELEK, SONS, ABS and DS
  (by code or name); another type blocks the Project and names its value.
- `ProjectMatch.id` 0 means no Project.
- An Event without a title gets its event type as subject.
- Any other record ERPNext or its database rejects is rolled back alone and
  marked `BLOCKED_RECORD`; see the Error Log. A blocked Customer blocks only
  its own Projects.

HERO customer IDs use the exact `Customer.nr` where it is a valid, available
ERPNext name: `2222` stays `2222`, and `0022` retains its leading zeros.
`customer_name` remains the person's or company's name. The existing
`custom_hero_customer_number` mapping preserves the original string, even if a
legacy mapping specifies Trim. Missing or unsuitable customer numbers use a
`HERO-CUSTOMER-<UUID>` technical ID with an explanation, not a competing KD
business number. Unrelated manual customers keep the site's naming settings.

New Project IDs use the confirmed trade and exact `ProjectMatch.display_id`,
for example `PV-3333` or `ELEK-1769`. The shared trade catalog validates the
measure; an unknown code or name is ignored when the other value is
confirmed, while two different confirmed trades require review. `project_nr` remains separate
and the protected project-title rule is unchanged. Missing or invalid project
IDs retain native Project naming and report the constraint.

Technical HERO IDs and source instances remain the identity keys. Reimports
reuse linked IDs, including existing KD customers and legacy projects. Changes
or explicit clears of an established business number require review; omitted
source fields are not treated as clears. Number collisions never merge,
suffix, rename or cross-link records. A failed concurrent identity claim rolls
back the new target and its relations.

`hero_integration.services.business_number_preview.preview_rows()` reads
existing links, business numbers, collisions and native referencing records.
`write_csv(path, project_trades=None)` writes a new CSV without overwriting an
existing file or changing the database. Projects use the stored mapped
`custom_hero_project_type` or an explicitly reviewed source-scoped configuration,
for example `{"source-a": {"201": "PV"}}`. Project titles are never used to guess
a trade. Without usable trade evidence the preview marks the project for
review. The preview covers metadata-defined static and dynamic links, including
child rows and Single fields; external references and arbitrary text fields
remain outside its coverage. A positive `safe_rename` is preliminary, not
permission: actual renames require separate authorization, a database backup
and a fresh reference/collision review. This change performs no renames.

Create a reviewed GraphQL schema snapshot before configuring mappings. The
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
run. The scheduler ticks every minute and starts polling only when mode,
timezone window and the configured 1..1440-minute interval allow it. The
interval counts from the last successful and the last started poll, so a
failing poll is not retried on every tick. Polling is one-way from HERO to
Frappe and waits while a migration run is active. Out-of-range polling
settings block polling with a visible Skipped event instead of falling back
to defaults.

A run is one queue job with short, per-record acknowledgement transactions.
Each target/identity update and its Run checkpoint commit together, fenced by
the existing run owner, monotonic claim timestamp, live lease and active slot.
A failure rolls back only unacknowledged work; previously committed records
remain. Resume validates the original bound contract and replays from offset
zero through existing identity links. It never treats a stored offset as proof
of a stable HERO dataset. Legacy runs without bound evidence require a new
approved run, rather than silently receiving a new contract.

The Run's `checkpoint_contract_json` binds site/source, confirmed source revision,
reader and mapping revisions/content, schema identity/content and the effective
selected field rules (including the Customer dependency of a Project run).
Mapping-root and rule locks protect checkpoint acknowledgement, never external
HTTP. MONITOR revalidates runtime generation, source configuration, schema,
reader and rules after HTTP before recording an observation. New runs fail
closed if the reviewed Run schema has not been installed. No schema migration
or deployment is implicit in these checks.

The existing run lease is renewed by valid checkpoints; a worker stopped beyond
the lease window can be replaced without allowing its stale claim to commit.

HERO reads are checked before any request: a document must contain exactly
one GraphQL query operation. Every HERO client in one worker process shares
the configured request rate per HERO host; retries cover only HTTP 429, 5xx
and transport failures, with jittered backoff or a bounded Retry-After.
Offset pagination ends only on an empty page, because a short page can be a
server page cap. An offset is a scan position, not a watermark, and a record
missing from HERO is never treated as a deletion.

Attachments, Helpdesk tickets, and reverse synchronization are unavailable.
The app does not infer a source contract or a target mapping for them.
