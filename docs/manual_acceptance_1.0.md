# Manual acceptance

Run this procedure only on a new, owned, non-production Frappe v16 site with
synthetic data. Do not configure a production HERO endpoint or token.

1. Install ERPNext v16, then this app, as described in
   [installation_guide_1.0.md](../installation_guide_1.0.md). Confirm that the site opens **HERO
   Integration Settings** and **HERO Migration Center**.
2. Confirm that all HERO app DocTypes exist.
   Repeat `bench --site <site-name> migrate` and confirm that they stay unchanged.
3. Create one synthetic Company. Select and confirm it in the settings.
4. Use the deterministic mock responses from the test suite. With no external
   network access, verify the fixed GraphQL variables, envelope, pagination,
   optional values, invalid records, conflict records and timeout retry.
5. Preview a migration without active mappings. Confirm that it blocks before
   any Customer, Project or **HERO Integration Link** write.
6. Upload a reviewed mapping package. Select its required Custom DocTypes,
   Custom Fields, relations and mappings.
   Confirm with the **Apply selection** action. Verify that the selected schema
   exists before you arm or activate a mapping, and that no migration starts.
   Verify native Customer values, primary Contact first/last name, Contact
   Email, and mobile and home Contact Phone rows. Verify the Project customer
   and company links and its Custom Field values.
7. Repeat the same data. Confirm that the app skips it without duplicate
   targets. Change a source payload, then a target payload, separately. Confirm
   that both become conflicts and that neither target is overwritten.
8. Enable scheduled sync. Trigger its scheduler entry once, then again while a
   run is active. Confirm that only one run is queued. Confirm that the token
   never appears in job arguments, run fields or logs.

Record the exact app Git revision, Frappe and ERPNext revisions, test-site name
and test results.

## Mapping import and export

1. On a fresh site, confirm that the HERO Frappe Mapping list is empty.
2. Run `bench --site <site-name> migrate` again and confirm that it stays empty.
3. Create a synthetic reviewed GraphQL schema snapshot.
4. Open HERO Mapping Editor and confirm that the source and target pickers work.
5. Export the empty mapping set. Confirm that it contains no token, endpoint,
   customer data or migration log.
6. Upload a reviewed mapping package and inspect the shared checklist.
7. Select one Custom Field and its dependent mapping. Confirm that selecting
   the mapping selects its required schema entry.
8. Upload a package with a foreign field collision or a missing relation.
   Confirm that the app blocks it and leaves the mappings inactive.
