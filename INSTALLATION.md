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

3. Add the reviewed HERO Integration candidate from the published development
   branch. RC16 is published only on `release/v1-rc`; do not substitute the
   default branch. Record the exact resolved commit after `bench get-app`.

   ```bash
   bench get-app https://github.com/unwichtigfett/hero_integration.git --branch release/v1-rc
   ```

   Before continuing, verify that this is the Bench CLI in the target user's
   intended toolchain, not a similarly named shell alias:

   ```bash
   bench --version
   bench version
   ```

4. Install HERO Integration on that ERPNext site. Its pre-install check aborts
   before HERO schema writes if ERPNext is missing, is not v16, or lacks
   Customer, Contact, Contact Email, Contact Phone, or Project.

   ```bash
   bench --site <site-name> install-app hero_integration
   ```

The app installs its DocTypes and HERO Custom Fields, but it installs exactly
zero `HERO Frappe Mapping` documents. Repeated `bench --site <site-name>
migrate` calls keep the mapping count at zero until a System Manager imports or
creates mapping definitions.

The Mapping Editor can later preview a reviewed schema-and-mapping package.
Uploading a package creates nothing. A System Manager must select and confirm
each HERO-owned Custom DocType, Custom Field, relation, and mapping in one
checklist. This app never creates a mapping from an install hook or package
upload.

5. Run migrations and build assets through normal Bench operations when the
   target bench requires them.

   ```bash
   bench --site <site-name> migrate
   bench build --app hero_integration
   ```

`bench install-app` creates the app's DocTypes and idempotently creates nine
HERO Custom Fields on Customer and Project. A later `bench migrate` uses the
same create-only, conflict-safe path. No Customize Form step is required.
See [DOCTYPE_EXTENSIONS.md](DOCTYPE_EXTENSIONS.md) for the exact schema.
Do not copy this app from another bench as a release substitute. The future
acceptance source is the reviewed Git revision used by `bench get-app`.

## RC16 upgrade check

For an upgrade copy, first record the pre-upgrade app revision, mapping count,
and export of the approved mapping package. Fetch the published
`release/v1-rc` revision, run `bench --site <site-name> migrate` twice, then
verify that the mapping count and imported mapping revisions remain unchanged.
Do not use a local working tree as fresh-install or upgrade evidence.

The target needs a scoped, functional database-bootstrap path for `bench
new-site`; do not substitute a fallback database engine for a MariaDB/ERPNext
upgrade proof merely because local root authentication is unavailable.
