# Manual acceptance

Run this procedure only on a new, owned, non-production Frappe v16 site with
synthetic data. Do not configure a production HERO endpoint or token.

1. Install ERPNext v16, then this app, by following
   [INSTALLATION.md](INSTALLATION.md). Confirm that the site can open **HERO
   Integration Settings** and **HERO Migration Center**.
2. Confirm that all HERO app DocTypes exist. Confirm the four Customer and five
   Project fields listed in [DOCTYPE_EXTENSIONS.md](DOCTYPE_EXTENSIONS.md).
   Repeat `bench --site <site-name> migrate` and confirm that they are unchanged.
3. Create one synthetic Company. Select and confirm it in the settings.
4. Use the deterministic mock responses from the test suite. Verify the fixed
   GraphQL variables, envelope, pagination, optional values, invalid records,
   conflict records, and timeout retry with no external network access.
5. Preview Customer and Project migration without active mappings. Confirm that
   it blocks before any Customer, Project, or **HERO Integration Link** write.
6. Upload a reviewed mapping package. Confirm that upload only displays one
   checklist. Select its required Custom DocTypes, Custom Fields, relations,
   and mappings. Confirm one **Apply selection** action. Verify that the
   selected schema exists before mappings become Active, and that no migration
   starts. Then execute the supported Customer and Project records. Verify native Customer
   values; primary Contact first/last name, Contact Email, mobile and home
   Contact Phone rows; Project customer and company links; Custom Field values;
   and one canonical **HERO Integration Link** per created target.
7. Repeat the same data. Confirm that it is skipped without duplicate targets.
   Change a source payload and then a target payload separately. Confirm that
   both become conflicts and neither target is overwritten. Verify that a
   shared Contact is not changed by a home-phone import.
8. Enable scheduled sync. Trigger its scheduler entry once, then repeat while a
   run is active. Confirm that only one run is queued and the token never
   appears in job arguments, run fields, or logs.
9. Verify that attachment migration, ticket migration, and reverse sync are
   reported as unavailable.

Record the exact app Git revision, Frappe and ERPNext revisions, test-site name,
and test results. This repository preparation does not include native VPS
acceptance; that acceptance remains pending.

## RC 15 manual acceptance protocol

Use `http://hero-native-cli-fresh.localhost:18010/app/hero-integration` as a
System Manager. Keep Polling **OFF** and mutation execution **OFF** for the UI
and local-function pass. Do not enter a credential until the separately
authorized read-only HERO step.

1. **Mapping first.** Open **Mapping**, select no snapshot, and verify the explanatory blocked state. Select a saved validated snapshot only when one exists; choose a query and a mutation operation, save a Draft, reload it, and verify that Draft, activation, service saving, preview and execution are separate actions. Check the three mapper boundaries in [MAPPING_MATRIX.md](MAPPING_MATRIX.md). Expected: no request to HERO and no silent selection of a latest snapshot.
2. **UI and local functions.** Verify exactly one sidebar entry and the five tabs **Overview**, **API & Schema**, **Mapping**, **Synchronization** and **Settings**. Under API & Schema verify **API Analyzer**, **Graph Viewer** and **GraphQL Services**. Toggle Query → Mutation → Query, reload a deep link, use browser Back/Forward and cancel one dirty-draft replacement. Expected: no full-page reset to Overview and no lost draft.
3. **Settings and visual states.** In light/dark and a narrow window, check neutral grey cards, visible field borders/labels, readonly/disabled/error states, and status colours. Polling starts closed and its summary shows only **Polling Settings** and the native chevron; opening/closing changes no setting. A failed asset load must show an English error rather than a blank page or endless loading state.
4. **Live read-only HERO introspection — separate approval.** Enter the credential only in Settings, confirm the exact HTTPS host, run one explicit schema refresh, and review its source identity, fingerprint, diff and operation counts. Do not treat a synthetic test snapshot as this proof.
5. **MONITOR — separate approval.** Select permitted sources and enable MONITOR. Verify stored observation events only: no Customer, Project, Link or other business writes. Return to OFF and confirm new automatic ingress is blocked.
6. **Controlled AUTO_SYNC — separate approval.** Use a designated reversible Frappe target with approved active mappings. Verify Customer-first, idempotence, conflicts, pagination/resume and no automatic deletions. Do not use this step for any HERO mutation.
7. **Helpdesk — separate approval.** Verify the actually installed compatible Helpdesk version and the frozen contract; otherwise record it as blocked, not supported by implication.
8. **Mutations — later, per operation.** For one operation, verify the exact active contract, type-valid input, preview fingerprint and explicit confirmation. There is no retry after an ambiguous remote outcome. The 15 real ProjectMatches remain three separately authorized Customer-first batches of five.
## Mapping import and export

1. On a fresh site, confirm that the HERO Frappe Mapping list is empty.
2. Run `bench --site <site-name> migrate` again and confirm it stays empty.
3. Create a synthetic reviewed GraphQL schema snapshot.
4. Open HERO Mapping Editor and confirm source and target pickers work.
5. Export the empty mapping set. Confirm that it contains no token, endpoint,
   customer data, or migration log.
6. Upload a reviewed mapping package and inspect one shared checklist.
7. Select one Custom Field and its dependent mapping. Confirm that selecting
   the mapping selects its required schema entry.
8. Apply the selection once. Confirm that the Custom Field exists, the mapping
   is Active, and no migration started.
9. Upload the same package again. Confirm that schema and mappings are marked
   Unchanged and no duplicate exists.
10. Upload a package with a foreign field collision or a missing relation.
    Confirm that it is blocked and leaves mappings inactive.

