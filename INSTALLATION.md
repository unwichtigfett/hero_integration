# Installation

Install this app only from a reviewed Git repository on a fresh Frappe v16
bench with ERPNext v16 already installed on the target site. The app declares
Frappe v16 compatibility in `pyproject.toml`. It deliberately does not use
Frappe's `required_apps` hook, because Bench resolves that hook before an app
can reject an installation and could otherwise install ERPNext automatically.
No development-machine path is required.

1. Create a Frappe v16 bench and site following the official Frappe
   installation guidance for the target operating system.
2. Add and install ERPNext v16 before adding HERO Integration.

   ```bash
   bench get-app erpnext --branch version-16
   bench --site <site-name> install-app erpnext
   ```

3. Add the reviewed HERO Integration release branch from its Git URL.

   ```bash
   bench get-app https://github.com/example-owner/hero_integration --branch release/v1-rc
   ```

4. Install HERO Integration on that ERPNext site. Its pre-install check aborts
   before HERO schema writes if ERPNext is missing, is not v16, or lacks
   Customer, Contact, Contact Email, Contact Phone, or Project.

   ```bash
   bench --site <site-name> install-app hero_integration
   ```

The app installs its DocTypes, the HERO source Custom Fields on Customer and
Project, the Project Phase and Volume fields, the Task HERO Assignee field,
the role "HERO Writeback", the trade Project Types, the German salutations and
the Project form layout. `bench install-app` does not run patches, so all of
this is created by `after_install`; `bench migrate` creates missing records
again (create-only) without resetting later form changes.

The install creates no field mappings and no read services. They come from a
**configuration file** exported on an existing site:

- Mapping Editor → "Complete configuration" → *Export* (or
  `bench --site <site> execute hero_integration.api.config_export_file --kwargs '{"path": "/tmp/hero-config.json"}'`).
  The file holds the field mappings with their HERO Custom Fields/DocTypes and
  the read service of every sync area. It never contains credentials or data.
- On the new site: save the HERO connection, refresh the HERO schema once
  (or, without HERO access, `hero_integration.api.snapshot_import_file` with an
  introspection JSON file), then import the file in the Mapping Editor or with
  `hero_integration.api.config_import_file` (`"dry_run": 0` to import; the
  default is a preview). The import adopts the mapping package, then creates,
  validates and approves the read services. HERO's schema changes often, so the
  package is bound to the new site's own snapshot and checked by content: every
  field rule and every read query must fit that schema, otherwise nothing is
  imported. A second import of the same file changes nothing.

Check an install with
`bench --site <site> execute hero_integration.app_setup.verify_install`.

5. Run migrations and build assets through normal Bench operations when the
   target bench requires them.

   ```bash
   bench --site <site-name> migrate
   bench build --app hero_integration
   ```

The automated fresh-install test is `ci/bench-install-test.sh` (Woodpecker,
see `.woodpecker.yml`). No Customize Form step is required.
Do not copy this app from another bench as a release substitute.
