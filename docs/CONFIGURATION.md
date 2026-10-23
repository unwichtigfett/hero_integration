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

Select an existing ERPNext Company to own all imported Projects and enable
**Confirm Company for HERO Projects**. The app checks that the Company exists,
is readable by the current user, and is not disabled.

Use **HERO Migration Center** to start a Preview before an execution run.
A preview creates source-link record. Execute creates
only records with a complete explicitly activated mapping. Existing records are skipped or
reported as conflicts; the app does not silently update or delete them.

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
run. The registered hourly job is one-way from HERO to Frappe. It skips when a
run is active and persists the active-run guard in **HERO Integration Sync
State**.

Attachments, Helpdesk tickets, and reverse synchronization are unavailable.
The app does not infer a source contract or a target mapping for them.
