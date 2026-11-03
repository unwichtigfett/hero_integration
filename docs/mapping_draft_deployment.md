# Mapping configuration transfer

Code deployment and mapping import are separate operations. `pipe deploy`
updates code and app metadata. The site-specific draft deployment hook is no
longer registered: a migration must not replay a historical mapping bundle or
fail because a target draft differs from the development site's hash.

Transfer mappings through **Settings → Configuration package → Import package**.
The complete JSON uses `hero-integration-config-v2` and explicitly declares
`replace_mappings: true`. The existing preview and confirmation remain the entry
point. A replacement file contains nine areas exactly once:

| Area | Operation |
| --- | --- |
| Customers / contacts | `HeroCustomers` |
| Projects | `HeroProjectMatches` |
| Calendar | `HeroCalendarEvents` |
| Tasks | `HeroTasks` |
| Employees | `HeroEmployees` |
| Products | `HeroProducts` |
| Services | `HeroServices` |
| Documents | `HeroDocuments` |
| Pictures | `HeroPictures` |

Full Migration orchestrates these areas; it is not another mapping area.
Customer relationships include the native contact and address handling. The
current complete package contains 122 selected rules, 60 custom fields, two
custom child DocTypes and four relations.

The file carries complete selected field rules, transformation options, required
schema declarations, nine saved read queries and their mapping configurations.
Customer/person naming fallbacks and buying/selling price lists remain distinct
rules. Identical mappings are not duplicated. Target-side service identifiers,
current revisions and approvals are resolved on import; source-site document
names and execution seals must not be reused as approvals.

Replacement makes the file's selected mappings authoritative for the included
areas. Existing definitions and drafts are handled through their owning mapping
services so revisions remain traceable. Native Customer/Project/Calendar/Task
contracts remain native; Products/Services/Documents/Pictures/Employees use
Query adapter v2. Mapping import sends no HERO request and imports no business
records. Obsolete active rules and parent mappings are disabled; selected
rules remain active. Verified historical predecessors remain available for
lineage checks. Open drafts belonging to the importing user are discarded
through the normal draft service; other users' private drafts remain intact.

Deploy the required importer code first, then select the JSON file, inspect the
preview and confirm the import. This is the existing import-and-activate flow,
not the historical draft-only deployment format. Existing native parents for
Customer, Project, Calendar and Task must be present on the target; this file
is a replacement package for the existing site, not a fresh-site bootstrap.
Schema provisioning precedes mapping publication; newly required schema can
remain installed if a later mapping validation fails. Missing or revoked read
approval blocks migration; there is no implicit built-in mapping fallback.

The historical `deployment/consolidated_mappings_20261103.json` remains an audit
input. It is not the JSON file accepted by the Settings importer and is no
longer applied by `bench migrate`.

Verification used the real local Frappe API in a rollback transaction: all nine
approved readers were available, a repeated import made no replacements, and
export/reimport retained every area unchanged. The same checks passed with the
legacy Calendar selection and stale approval restored temporarily. Business
document counts remained unchanged. These checks do not substitute for the
user-run Docker deployment and JSON import.
