# Portability report

## Checked production surfaces

The app uses Frappe APIs for document storage, site context, queueing, scheduler
registration, and encrypted password access.

## Result

- No runtime references to development home directories, fixed sites, private
  addresses, Docker container or volume names, database credentials, or SSH key
  paths were found.
- ERPNext is a mandatory preinstalled application: a pre-install
  guard checks its v16 import and required native DocTypes without using the
  auto-installing `required_apps` hook. Helpdesk is detected only for its
  optional profile.
- The Frappe-standard nested module directory is intentional: it contains the
  app's exported DocTypes and Desk Page.
- Mapping packages and the **HERO Mapping Adoption** DocType use Frappe
  Document APIs only. They carry no host path, endpoint, token, business data,
  executable code, or permission definitions. A package upload is read-only.
- `patches.txt` records, that no data patch is required.

## Deliberate constraints

Endpoint hostnames and tokens are site configuration, never release metadata.
HERO source mappings are fixed and versioned.
