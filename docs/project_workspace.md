# Project workspace extension

## Scope and status

The Custom design augments the existing seven native Project tabs using
`public/js/project_workspace.js`, registered after `hero_project_send.js`.
`public/css/project_workspace.css` is loaded only when a Project form is
rendered. Native fields are not recreated, moved, made writable or saved by
the extension. Existing workflow, required fields, timeline, dashboard, upload
limits and HERO preview/confirmation remain authoritative.

The initial source-only step was followed by a separately authorized dev
activation on 2026-11-17. Source, runtime reads and asset delivery are verified;
**authenticated-browser acceptance is still outstanding**. No database/schema
migration was performed.

## Views and data ownership

| View | Behavior and boundary |
| --- | --- |
| Details | Two context cards: Project Address on the left and the customer's Primary Contact on the right. No redundant Customer/address card or custom title card. The native Project Name field sits directly above Customer in one section. |
| Documents | Project-attached non-image Files, selection and permission-checked PDF page images with original-file fallback. No embedded browser PDF plug-in is required. Sales Orders/Invoices open separately using native filtered lists; they are not presented as attached Files. |
| Images | Authorized raster Files grouped and filtered by administrator-owned categories. Classification uses native File saves with optimistic concurrency. SVG/active formats are not embedded. Successful custom panels suppress duplicate native galleries; disposal or a failed/denied read restores them. |
| Appointments | Event list, selection and participant/week grid for the loaded page. Exact Project references or Dynamic Links are required. Native Event creation prefills the explicit Project reference. No name matching, availability calculation or recurrence expansion is performed. The native Calendar remains available for those workflows. |
| Tasks | Server-paged search/status filter, list and read-only board of the loaded page. Native Task creation and forms retain assignments, dependency rules and transitions; the native list retains Gantt/full boards. |
| Assignments | A native, writable ERPNext Project Responsible User Link and the native Project User table appear above the direct Project ToDo list, following the HTML reference hierarchy. Task assignments, team membership and responsibility remain distinct. |
| Advanced | Native costs, notifications, source fields and actions remain in place; permission-filtered ERP list navigation is added. |

The existing tab fieldnames are retained. Missing
tabs are not invented; existing native layout and permission visibility remain
the fallback. Scheduling and employee availability are not inferred from
prototype data.
Native privacy/attachment limits are unchanged. Preview links are restricted to
same-origin `/files/` and `/private/files/`; unsupported/remote Files remain
reachable through their native form. Large, protected or unsupported PDFs can
be opened explicitly through “Open file”.

English is the source/fallback language. New UI messages have German entries
in the existing `translations/de.csv`. Language, date/time format and timezone
continue to follow native Frappe user settings; record names and user content
are not translated.

## Read RPC

`GET hero_integration.services.project_workspace.get_panel`

Arguments: `project` (required saved Project name), `section` (one of `details`,
`documents`, `images`, `appointments`, `tasks`, `assignments`), `start`
(nonnegative candidate offset, at most 100000), optional `search` (at most 120
characters), and optional native Task `status`.
Images additionally accept `category` or `uncategorized=1` (mutually exclusive);
these filter the stored File Link before paging. Image rows include the
permitted category and saved `modified` value. Enabled category names supply
the HTML-style group headings/counts and category-specific upload tiles,
including empty categories. The bounded catalog reports truncation explicitly.

The RPC checks authentication and Project read permission before any related
query, applies native field-level filtering/masking before following links,
uses permission-aware `get_list`, then checks each returned document again and
applies native field-level filtering/masking. No `get_all`, raw SQL, identity
switch, database write, queueing or external request is used.

The Frappe RPC `message` payload contains `project`, saved `modified`,
`section`, `state`, `rows`, `next_start`, and `context` for Details. Related
DocType denial is `state=permission_denied`, not an empty success. Project
denial, invalid input and unexpected failures use normal Frappe errors.
Pages scan at most 20 candidates plus one lookahead. Concurrent permission
changes or stricter field/link checks can produce fewer rows, including an
empty page with a valid next page. Appointment queries join native Dynamic
Links and then validate the full reference pair on each authorized Event.
This query contract still requires real-engine integration verification.

Client request generations, Project identity and current route reject stale
responses. Customer edits invalidate in-flight customer context. Related data
is explicitly identified as saved-state data when the form is dirty/stale.
Errors keep native fields usable and offer retry without exposing raw errors.

## Offline verification

From the repository root, with Python and Node available:

```sh
python3 -B -m unittest hero_integration.tests.test_project_workspace -v
python3 -B -m unittest hero_integration.tests.test_project_workspace_extensions -v
python3 -B -m unittest hero_integration.tests.test_project_pdf_preview -v
node --test hero_integration/tests/test_project_workspace.mjs hero_integration/tests/test_logbook_send.mjs
```

These tests use synthetic documents, a fake Frappe read boundary and a small
DOM/form contract double. They open no site/database and do not prove live
query-engine joins, role configuration, browser PDF behavior or visual parity.
The existing logbook tests cover preservation of the HERO confirmation flow.

The two real PDFium worker tests skip if PDFium is unavailable on the host;
execute them separately in the candidate runtime before rollout. The browser
fixture checks decoded PDF-page PNGs, next-page navigation and zero
`iframe`/`object`/`embed` elements or original-PDF browser requests.

## Plug-in-independent PDF preview

The former empty-sandbox PDF iframe can be blocked by Chromium-family PDF
viewers. The replacement does not loosen frame policy or require disabling
Brave Shields: `GET hero_integration.services.project_pdf_preview.get_page`
checks authentication, Project/File read and field permissions, exact parent
association and File timestamp on every page. It reads only contained local
site files, never remote URLs, and returns a PNG data URL in a private/no-store
JSON response. No preview File, image or business record is persisted.

The existing runtime's PDFium package is now an explicit app dependency
(`pypdfium2>=5.14.0,<6`); Pillow remains part of the Frappe runtime. No CDN,
third-party document viewer, browser extension or package download is used
during preview. PDFium forms and JavaScript/XFA initialization are not enabled;
static annotations are rasterized without interactive actions. The UI states
that form fields and signature validation require opening the original.

One nonblocking renderer lock per site bounds concurrency. A fresh isolated
Python child receives only PDF bytes and the validated page number, with a
minimal environment and no document URLs/credentials. Linux resource limits
are 384 MiB address space, 3/4 seconds CPU soft/hard and zero file/core output;
the parent applies an 8-second wall timeout. Input is capped at 10 MiB, documents
at 200 pages and output at 2 million pixels / 4 MiB PNG. These are process and
resource controls, not a claim of a complete OS security sandbox.

The UI displays one rasterized page at a time, retains original-file access,
supports previous/next page and discards stale results after document/project
changes. Large, encrypted, busy or invalid inputs fail closed with a retry or
original-file fallback. No PDF plug-in or PDF content executes in the page.

### PDF preview activation — 2026-11-17

Functional revision `dc9b610f989e03fc6265cf54bbbc660ccae132a5` is active on
dev in `hero-project-pdf-preview-dev:dc9b610f989e` (image ID
`sha256:2d449ee9d00d01f021eaabd5eb7cad98754d3d84c620d1c900ded5f3d73071c9`).
The already-installed PDFium 5.14.0 runtime was reused; no package download,
schema migration or new preview files were needed.

97 distinct test cases passed across host, isolated PDFium runtime and
synthetic Chromium UI checks. The two real PDFium tests skipped on the host
were executed successfully in the runtime. Both actual private dev PDFs
(9 and 2 pages) rendered successfully, including next-page verification.
Two existing non-manager users could render permitted content; Guest HTTP
access was denied. Served assets, six running app services and unchanged
Project revisions were verified.

Brave itself was not automated; the verified correction removes its embedded
PDF viewer entirely, without changing browser protections or site framing
policy. Authenticated manual Brave acceptance remains a separate check.
Protected backup/rollout evidence is under
`/opt/hero-auto-deploy/patches/project-pdf-preview-20261117/dev/`.

An optional real-DOM layout check serves only allowlisted synthetic fixtures on
localhost and uses a throwaway Chromium profile:

```sh
CHROMIUM_BINARY=/path/to/chrome python3 -B -m unittest hero_integration.tests.test_project_workspace_browser -v
```

It checks desktop and narrow layouts, decoded raster images, keyboard focus
and retained native-control placeholders. It is not an authenticated Frappe
end-to-end test. Root-only test execution needs Chromium's `--no-sandbox`;
no existing browser profile, credentials or live site is used.

## Administrator-owned image categories

`Project Image Category` is a normal app-owned DocType. Authenticated users can
read its configuration; only System Managers can create, rename, disable or
delete categories. Native Link deletion safeguards apply. “Manage image
categories” in the Images toolbar opens its native list for administrators.
The initial one-time patch seeds `Dach` and `Elektro`; subsequent ordinary
migrations do not recreate administrator-renamed/deleted category defaults.

`File.custom_project_image_category` is a create-only Link. Existing
unclassified images remain unchanged until explicitly assigned. New or newly
attached Project images require a category; an assigned category cannot be
cleared. No category is inferred
from filenames, image content or HERO's document/image type.

`POST hero_integration.services.project_image_categories.set_image_category`
requires Project write permission, File write permission, an actual image
attached to that exact Project and the expected File timestamp. It uses native
`save()`. The File validation hook also enforces the rule for native CRUD.
Disabled categories cannot be newly assigned; existing references remain
usable. Changing classification or moving a categorized File requires
the affected Projects' write permissions. Category changes do not send data
to HERO.

Before upload, a required native Link prompt selects an enabled category.
`get_upload_options` checks Project write/category read and returns the
category's managed native File folder. The unmodified native FileUploader
posts to `upload_file`, retaining chunking, CSRF, privacy, extension/size limits,
existing-file access checks and the Project attachment quota. The File
validation hook resolves the *explicit stored folder-to-category Link* while
creating the File, so categorization is part of the original insert, not a
second best-effort save. Missing/retired bindings reject the upload and use
native rollback cleanup. Existing filenames/content never determine category.

The hidden, application-managed `Project Image Category.upload_folder` Link is
created with each category. Its native logical folders are retained on category
deletion rather than risking deletion of unrelated files; no physical file
directories or existing image categories are moved. The persistent category
remains the File Link, not the folder: reorganizing an existing File does not
silently reclassify it.

## Compact native fields and local responsibility

The scoped `compact_project_workspace` patch modifies only the configured
seven-view profile. Its existing field-order/section-label Property Setters
are updated without removing any business fields. An explicit first section
removes the redundant opening section, Project Name immediately precedes
Customer, and the protected HERO responsible observation moves to Advanced
source information.

`Project.custom_project_responsible` is a separate optional native User Link,
saved only through ordinary Project saving. A server validate hook accepts
only enabled System Users for new/changed selections; unchanged historical
references do not block unrelated updates. The original HERO ID guard runs
first. This choice neither adds Project Users/ToDos nor grants access/roles
nor automatically changes HERO responsibility. The protected HERO observation
is never unlocked.

The address label is “Project Address” / “Projektanschrift”. The tab-to-heading top gap is
removed. “Project sidebar” collapses only the form's right sidebar and restores
the content width. Its session-only choice survives form refresh; the global
workspace navigation and intrinsic native visibility are untouched.

Visual reference: the two Project HTML files in `ssh_transfer` have identical
SHA-256 `41fe92e272e490933d4bbd2635656056bc2cff5ae22af7302e5798420ed9e5ed`.
Image groups/add tiles and team-before-assignment hierarchy follow that
reference; the user's subsequent simplifications take precedence. Prototype
names and business records are never used as production data.

## One per-user Design package

The native Appearance dialog has one **Design** choice:
`Native` / `Custom` (German: `Nativ` / `Angepasst`). It is not a collection of
per-feature flags. Existing users retain Custom until explicitly changed.
The hidden User preference is updated by an authenticated own-user-only POST.
Changing designs requires confirmation/reload and blocks on a dirty native
form; it never saves or discards business edits automatically.

Custom enables the Project presentation, Navy palette and site-logo/rail
arrangement. Native disables those appearance assets and supplies a private
native Project metadata projection through the supported `getdoctype`
override. It copies the shared metadata, restores only known package visual
labels/collapse settings and native field order, removes only the package's
data-free presentation controls, and keeps every business field, permission,
read-only/required/hidden flag and integration action. Shared metadata and the
global stored Project layout are never changed by a user's choice.

Local responsibility and mandatory image categorization remain business
features in both designs. Native retains a standard toolbar image-upload
action because a category is still required. The PDF read API and HERO guards
remain active independently of presentation. A saved Navy color preference
renders as built-in Dark while Native is active, without destroying that
preference; the custom palette is offered only with Custom.

The standalone CRM/Helpdesk Navy additions follow the same user preference;
functional Desk-return behavior and the environment/version indicator remain
independent. Site-wide business reports/status semantics, configured company
identity and anonymous login configuration are not rewritten by a personal
appearance choice.

### Usability/design package activation — 2026-11-17

The package was activated from functional revision
`dd0de4f094971db82f008c737bf22bec7084b6c3`, following the implementation commit
`5c7f97ded64e189f3a7977346e5d4b8469566ea1`. The scoped native patch added only
the own-user Design preference, local Project Responsible Link, managed
category-upload folder Link and affected presentation settings. No full
migration was run.

161 targeted cases passed across Python, Node and synthetic Chromium checks.
Live native tests verified both design projections without shared-cache
mutation, own-user preference changes without manager privileges, normal
Project responsibility saving, rejection of uncategorized native image inserts
and category assignment in the initial native File insert. All test writes
were rolled back. Project revisions, stored User preferences and existing image
classification were retained.

The real browser fixture checks the two-card layout, address left/contact
right, native draft/required/read-only control preservation, no tab-heading gap
and the right-sidebar toggle without affecting workspace navigation. Production
business data is not used in the fixture.

Global design scripts use an explicit package asset version so previously
cached Navy/logo scripts cannot bypass the Native choice. Protected metadata
snapshots, database/files backup and runtime verification are under
`/opt/hero-auto-deploy/patches/project-design-package-20261117/dev/`.

## Optional local Project Address

`custom_project_address` is an optional native Address Link in both designs.
An existing value is preserved on saves, customer changes and edit toggles.
A readable, enabled, complete customer primary address fills an empty field
when the manual override is off. Missing or incomplete defaults leave it empty.
New explicit selections still require native link validity and read access.
The global Project hook uses this same rule for forms, REST and imports.

`ensure_fields` idempotently removes the old required flag through a Property
Setter, without bulk-saving historical Projects. The existing narrow retirement
of the invalid legacy `custom_adresse` autofetch remains. Run the normal app
migration on the intended target before use; production migration requires approval.
Native HERO-mapped project address relations keep their existing owner and
separate target links; no parallel relationship synchronization is introduced.

The draft controller fills only empty values and retains native dirty tracking,
read permissions, field permissions and asynchronous stale-response protection.
The read-only context endpoint returns only permitted postal text.

## HERO construction-site address (separate compatibility read)

This source-only endpoint is retained for existing consumers. It no longer
owns the workspace's local Project Address. It never populates the new local
Link implicitly; mapping-owned source addresses and confirmed HERO identities
remain separate and unchanged.

`GET hero_integration.services.project_site_address.get_site_address` checks
Project read permission before accessing bindings, credentials, cache or
upstream data. It reuses the existing unique synchronized HERO Project binding
and read-only GraphQL client with fresh, confirmed account configuration.
The only selected business value is `ProjectMatch.address.full_address`,
queried by the bound source ID; returned identity is checked.

This is a read-only projection, not an editable Customer/Address link and not
a second import/writeback pipeline. `Project.custom_adresse` is untouched.
No Project record is modified. Unbound, missing and unavailable source states
are explicit and never fall back to a customer's address or parsed title.
The request runs independently of native form loading, is capped at 15 seconds
with no retry and a 256 KiB response limit, and caches at most five minutes.
Cache identity includes site, binding, account endpoint and confirmed binding
revision; permission is checked on every access, including cache hits.

This extension needs scoped synchronization of the new DocType, the File
Custom Field and the one-time category patch. Unlike the initial five-file
overlay below, it changes schema additively. Take a fresh protected backup;
do not run an unrelated full-app migration or drop the new schema on automatic
code rollback.

### Category extension activation — 2026-11-17

Functional revision `9e8e2d4dd974b29b599e2627c9ec80d738086bfd` is active on
dev in `hero-project-categories-dev:9e8e2d4dd974` (image ID
`sha256:fa2af84cb3a44d5923bebef098482f1f4263b43349a9f5c0311ec4b656d221d3`).
The new DocType and File Link were synchronized through only the scoped native
patch; no full migration was run. Both default categories are enabled.

83 offline tests passed. Native category assignment/filtering was exercised
inside a rolled-back transaction, confirming existing image classification and
timestamp were restored. Eleven existing non-manager users were denied
configuration writes. All six app services, served asset hashes, German labels,
Guest HTTP denial and unchanged Project revisions were verified. Fresh
database/configuration/public/private file backups precede the schema change.

**Site-address data remains blocked, not verified as fetched from HERO.**
All 49 current dev Projects lack both HERO source IDs and Project integration
bindings. The read endpoint correctly returns `not_linked`; it does not infer
identity from a name or customer address. Confirmed Project-to-HERO identities
must be established through the existing integration before real addresses
can be shown.

Protected deployment evidence and the schema-compatible code rollback are in
`/opt/hero-auto-deploy/patches/project-workspace-categories-20261117/dev/`.
The old UI fallback retains the additive category schema/controller/validation;
automatic rollback never drops the new table, column or administrator data.

## Initial dev activation — 2026-11-17

- Functional source: `b9c300a589ebce549b9afde864bcebb236afe2d6`.
- Image: `hero-project-workspace-dev:b9c300a589eb`, immutable ID
  `sha256:de078beac1df5e2b1140493d4c041708c199a15eb18bbfcce61c42b4b7988a7d`.
- Five runtime files were overlaid on the actual active image; prior patches
  and the historical full-app revision marker were retained. The feature's
  source/hash manifest is `/opt/stack/project-workspace.json`.
- Backend, frontend, websocket, both queues and scheduler use the new image.
  Backend/websocket health and public HTTP checks passed; served JS/CSS hashes
  match the manifest. Database/Redis containers were not recreated.
- All six read sections passed against real dev data in read-only database
  sessions. Existing non-manager users with and without Project access were
  checked; unauthenticated HTTP access was denied with 403.
- Native Project form metadata includes both the new script and the existing
  HERO confirmation. Required/read-only fields, attachment limit and the new
  German translations were verified.
- The 49 Project name/modified/docstatus rows have the same fingerprint before
  and after activation. Scheduler pause state is unchanged; maintenance is off.
- Database, configuration and public/private file backups were taken and
  checked under the protected site backup directory
  `private/backups/project-workspace-20261117/`.

The first attempt blocked on the existing backend HTTP health check because
maintenance mode intentionally returns 503. The old version was restored and
HTTP-verified before retrying. The successful retry gated public traffic by
stopping the frontend, left maintenance off, verified the backend and caches,
then started the matching frontend. Health checks were not weakened.
Protected operation records, including both attempts and the rollback
environment, are under
`/opt/hero-auto-deploy/patches/project-workspace-20261117/dev/`.

## Before any separately authorized dev rollout

Recheck routing, worktree and actual runtime fingerprints. Deliver matching
Python, hook, JS, CSS and translation sources to backend/frontend together
through the established deployment mechanism; the source checkout is not a
runtime bind mount. Do not invoke the full migration/deployment script merely
to serve these assets. Determine required metadata/translation cache
invalidation and a bounded backup/recovery path before execution.
Do not wait for the normal HTTP health check while maintenance mode is on;
use the frontend-gating sequence above for a schema-free overlay.

Authenticated acceptance must cover native saves/failures and read-only
fields, lower-privilege and masked-field reads, Event join/paging semantics,
private-file preview/download/upload limits, keyboard/narrow-layout behavior,
English/German text, retained native dashboard/timeline actions and unchanged
HERO confirmation. Scheduling/availability and authenticated visual acceptance
remain explicit limitations, not accepted completed features.

## Release import category policy

Approved mapping evaluation remains mandatory. For imported Project images, a
valid enabled mapped category wins. Missing or invalid categories use an active
`HERO-Import` category created idempotently through its native controller;
disabled names remain disabled and the importer uses an active numbered successor.
Classification occurs before File validation. Existing unmapped, uncategorized
images are retained; relinking an image to a Project applies the new-image rule.
File updates and relinks use native saves inside the existing guarded transaction.
Manual image uploads keep their explicit category selection.

Migration starts use only `POST hero_integration.api.sync_area_start`.
The unused `services.file_transfer.start` entry point has been removed.
Run creation and enqueue-after-commit retain the normal Frappe POST transaction.
Administrative integration output adds `outcome` alongside unchanged detailed
result codes: SUCCESS, FAILED, SKIPPED_DEPENDENCY or CONFLICT. Review/dependency
holds remain distinguishable through the original result and action fields.
