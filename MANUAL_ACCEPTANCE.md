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
   Change one mapped HERO field only and confirm that the approved target field
   updates through native Frappe validation. Then change that same mapped field
   on both sides since the common comparison state. Confirm that the Link is a
   conflict, the target is not overwritten and one configured System Manager
   receives an internal notification. Repeat an unchanged poll and confirm no
   notification flood. Run the equivalent Preview and confirm that target, Link
   status and observed hashes remain unchanged. Verify that a shared Contact is
   not changed by a home-phone import.
8. Enable scheduled sync. Trigger its scheduler entry once, then repeat while a
   run is active. Confirm that only one run is queued and the token never
   appears in job arguments, run fields, or logs.
9. Verify that attachment migration, ticket migration, and reverse sync are
   reported as unavailable. Keep **Allow deletion** OFF and attempt a normal
   HERO-owned service or mapping delete: the server must refuse it. In a
   synthetic isolated test only, enable the gate for one owned record, then
   return it to OFF. This never authorizes ERPNext record deletion.

Record the exact app Git revision, Frappe and ERPNext revisions, test-site name,
and test results. This repository preparation does not include native VPS
acceptance; that acceptance remains pending.

## RC 15 ChatGPT operating guide

Use this guide in the listed order. It describes the released UI; it does not
grant a live HERO action. Credential entry is only permitted in **Settings**
for the separately approved read-only step.

| UI path and function | Prerequisite and input | Effect and boundary | Error state and source evidence |
| --- | --- | --- | --- |
| **Mapping** → **Map fields** | Select an explicit, validated snapshot and a source operation; choose only an allowed target field. | Creates or edits the classic `HERO Frappe Mapping` draft. Save, activation, import/export, preview and data transfer remain separate actions. Its exact `hero-frappe-mapping-package-v2` format is in [MAPPING_MATRIX.md](MAPPING_MATRIX.md). | Missing, stale, protected or incompatible fields remain blocked with a reason. See `test_direct_frappe_adapter.py`, `test_mapping_exchange.py` and `test_frappe_mapping_catalog.py`. |
| **Mapping** → **Query Mapping** or **Mutation Mapping** | Pick the same explicit snapshot, then one query or mutation operation and typed paths. | Persists the dynamic `HERO GraphQL Mapping.mapping_json` object and fingerprint. Query mappings never become the classic package; mutation mappings also need a separate `HERO Mutation Contract`. | An absent/changed snapshot or invalid path stays Draft/blocked. See `test_schema_operations.py`, `test_workspace_snapshot_binding.py` and `test_workspace_mutation_draft_contract.py`. |
| **Overview** | No endpoint or credential is needed. | Shows only stored, redacted connection, snapshot, service, mapping and run status. It never contacts HERO. | A missing local value renders as an empty/not-started state, not an inferred remote status. See `api.py:integration_overview` and `test_workspace_overview_contract.py`. |
| **API & Schema** → **API Analyzer**, **Graph Viewer**, **GraphQL Services** | Analyzer/viewer require a saved snapshot; Services additionally receives Query or Mutation from its canonical route. | Analyzer/viewer remain local projections. A Service draft is bound to source identity and fingerprint; switching Query/Mutation, deep links and browser history must retain that route context. | No implicit "latest" snapshot and no route fallback to Overview. See `hero_workspace.js`, `test_hero_desk_ui_contract.py` and `test_graphql_graph_contract.py`. |
| **Synchronization** | Keep polling OFF for local review. MONITOR/AUTO_SYNC need a separately approved source, active mappings and confirmed Company. | OFF makes no automatic request; MONITOR records observation only; AUTO_SYNC permits approved HERO-to-Frappe writes only. No mode executes a HERO mutation and no HERO exactly-once claim is made. | Invalid windows, sources, paging, limits or stale mappings fail closed. See `polling.py`, `tasks.py`, `migration.py`, `test_polling_contract.py` and `test_scheduler_contract.py`. |
| **Settings** | System Manager; credentials only for the explicit live read-only gate. | Saves allowed endpoint/host, Company, polling, conflict recipients and default-OFF **Allow deletion**. The native **Polling Settings** accordion is initially closed and opening it changes nothing. Mutation activation is a separate global request, still insufficient without a per-operation contract, preview fingerprint and confirmation. | Server permission and validation failures are displayed without secret values. Asset failures show an English error boundary. See `hero_settings.js`, `hero_workspace.js`, `test_workspace_settings_contract.py` and `test_hero_workspace_loader.mjs`. |

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
6. **Controlled AUTO_SYNC — separate approval.** Use a designated reversible Frappe target with approved active mappings. Verify Customer-first, local repeat protection, conflicts, pagination/resume and no automatic deletions. Do not use this step for any HERO mutation.
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

