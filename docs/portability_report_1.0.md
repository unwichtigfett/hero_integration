# Portability report

## Checked production surfaces

The app uses Frappe APIs for document storage, site context, queueing, scheduler
registration, and encrypted password access.

## Result

- The runtime code contains no references to development home directories, fixed
  sites, private addresses, Docker container or volume names, database
  credentials, or SSH key paths.
- ERPNext must be installed first. A pre-install guard checks its v16 import and
  the required native DocTypes. It does not use the auto-installing
  `required_apps` hook. The app detects Helpdesk only for its optional profile.
- The Frappe-standard nested module directory is intentional. It holds the app's
  exported DocTypes and Desk Page.
- Mapping packages and the **HERO Mapping Adoption** DocType use only Frappe
  Document APIs. They carry no host path, endpoint, token, business data,
  executable code, or permission definitions. A package upload is read-only.
- `patches.txt` records that no data patch is required.

## Deliberate constraints

Endpoint hostnames and tokens belong to site configuration, never to release
metadata. HERO source mappings are fixed and versioned.
