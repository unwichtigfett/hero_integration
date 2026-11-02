# Installation

Install this app only from a reviewed Git repository. Use a fresh Frappe v16
bench with ERPNext v16 already installed on the target site. The app declares
Frappe v16 compatibility in `pyproject.toml`. It does not use Frappe's
`required_apps` hook. Bench resolves that hook before an app can reject an
installation, so it could install ERPNext automatically. You need no
development-machine path.

1. Create a Frappe v16 bench and site. Follow the official Frappe installation
   guide for your operating system.
2. Add and install ERPNext v16 before you add HERO Integration.

   ```bash
   bench get-app erpnext --branch version-16
   bench --site <site-name> install-app erpnext
   ```

3. Add the reviewed HERO Integration release branch from its Git URL.

   ```bash
   bench get-app <repository-url> --branch <release-branch>
   ```

4. Install HERO Integration on that ERPNext site. Its pre-install check aborts
   before any HERO schema write if ERPNext is missing or is not v16. It also
   aborts if Customer, Contact, Contact Email, Contact Phone or Project is
   missing.

   ```bash
   bench --site <site-name> install-app hero_integration
   ```

The app installs:

- its DocTypes
- the HERO source Custom Fields on Customer and Project
- the Project Phase and Volume fields
- the Task HERO Assignee field
- the role "HERO Writeback"
- the trade Project Types
- the German salutations
- the Project form layout

`bench install-app` does not run patches, so `after_install` creates all of
this. `bench migrate` creates missing records again (create-only). It does not
reset later form changes.

The install creates no field mappings and no read services. You get them from a
**configuration file** exported on an existing site:

- Mapping Editor → "Complete configuration" → *Export* (or
  `bench --site <site> execute hero_integration.api.config_export_file --kwargs '{"path": "/tmp/hero-config.json"}'`).
  The file holds the field mappings with their HERO Custom Fields/DocTypes and
  the read service of every sync area. It never contains credentials or data.
- On the new site, save the HERO connection and refresh the HERO schema once.
  Without HERO access, use `hero_integration.api.snapshot_import_file` with an
  introspection JSON file instead. Then import the file in the Mapping Editor
  or with `hero_integration.api.config_import_file`. The default is a preview;
  pass `"dry_run": 0` to import. The import adopts the mapping package. It then
  creates, validates and approves the read services. HERO's schema changes
  often. The import therefore binds the package to the new site's own snapshot
  and checks it by content. Every field rule and every read query must fit that
  schema, or nothing is imported. A second import of the same file changes
  nothing.

Check an install with
`bench --site <site> execute hero_integration.app_setup.verify_install`.

5. If the target bench requires it, run migrations and build assets through
   normal Bench operations.

   ```bash
   bench --site <site-name> migrate
   bench build --app hero_integration
   ```

The automated fresh-install test is `ci/bench-install-test.sh` (Woodpecker,
see `.woodpecker.yml`). You need no Customize Form step.
Do not copy this app from another bench as a release substitute.
