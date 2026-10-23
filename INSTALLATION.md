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
   bench get-app https://github.com/unwichtigfett/hero_integration --branch release/v1-rc
   ```

4. Install HERO Integration on that ERPNext site. Its pre-install check aborts
   before HERO schema writes if ERPNext is missing, is not v16, or lacks
   Customer, Contact, Contact Email, Contact Phone, or Project.

   ```bash
   bench --site <site-name> install-app hero_integration
   ```

The app installs its DocTypes and HERO Custom Fields, but it installs exactly
zero `HERO-Frappe Mapping` documents. Repeated `bench --site <site-name>
migrate` calls keep the mapping count at zero until a System Manager imports
or creates mapping definitions.

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
Do not copy this app from another bench as a release substitute.
