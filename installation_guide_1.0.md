# Installation

The installation branch is `hero_int_1.0`:
<https://github.com/unwichtigfett/hero_integration/tree/hero_int_1.0>

Install this app only from that branch. Use a fresh Frappe v16
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

3. Add HERO Integration from its installation branch `hero_int_1.0`.

   ```bash
   bench get-app https://github.com/unwichtigfett/hero_integration --branch hero_int_1.0
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

The install creates no field mappings and no read services. You move them from
an existing site (e.g. development) with **one package file**.

**What the package contains:** the HERO schema snapshot, the operational
settings (sync areas, automatic sync, polling, schema refresh), the field
mappings with their HERO Custom Fields and DocTypes, and the read service of
every sync area.

**What it never contains:** HERO URL and API token, allowed hosts, admin
notification recipients, the mutation and deletion switches, testing mode,
runtime status and any HERO or ERPNext data.

**Export** on the source site: Mapping Editor → "Complete configuration" →
*Export complete configuration*, or

```bash
bench --site <site> execute hero_integration.api.config_export_file --kwargs '{"path": "/tmp/hero-integration-config.json"}'
```

**Import** on the target site. The importing user needs the role System Manager
and write access to every target: Customer, Contact, Address, Project, Task and
Event (in ERPNext e.g. the roles Sales Manager or Sales Master Manager and Projects
User).


1. Install the app (steps 1–4 above). If you want the automatic sync to run,
   enter the HERO connection (URL, token) in the HERO settings first.
2. Mapping Editor → "Complete configuration" → choose the file. The preview
   lists the snapshot, the changed settings and the read services. Confirm once.
   Or use

   ```bash
   bench --site <site> execute hero_integration.api.config_import_file --kwargs '{"path": "/tmp/hero-integration-config.json", "dry_run": 0}'
   ```

   (without `"dry_run": 0` it only previews).

The import creates the snapshot from the file if the site has none for that
schema. It then applies the settings, adopts the fields and mappings, and
creates, validates and approves the read services. No HERO request is made.
To bind the package to a different HERO schema snapshot of the target site, pass
`"snapshot_name": "<snapshot>"` to `config_import_file`. The package is then
checked by content: every field rule and read query must fit that schema, or
nothing is activated. A second import of the
same file changes nothing. The mutation switch stays off until you switch it on.

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
