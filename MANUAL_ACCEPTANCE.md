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

