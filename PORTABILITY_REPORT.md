# Portability report

## Checked production surfaces

The release source was reviewed for Python, JavaScript, JSON, hooks, scheduler
entries, DocTypes, package metadata, installer inputs, patches, and templates.
The app uses Frappe APIs for document storage, site context, queueing, scheduler
registration, and encrypted password access.

## Result

- No runtime references to development home directories, fixed sites, private
  addresses, Docker container or volume names, database credentials, or SSH key
  paths were found.
- No runtime imports from `auto_domain`, `hero_connector`, DTS, or Helpdesk
  were found. ERPNext is a mandatory preinstalled application: a pre-install
  guard checks its v16 import and required native DocTypes without using the
  auto-installing `required_apps` hook. Helpdesk is detected only for its
  optional profile.
- The Frappe-standard nested module directory is intentional: it contains the
  app's exported DocTypes and Desk Page.
- The Page JavaScript is package data. The installer creates nine create-only,
  conflict-safe ERPNext Custom Fields. There are no separate public assets,
  templates, fixtures, or data patches.
- Mapping packages and the **HERO Mapping Adoption** DocType use Frappe
  Document APIs only. They carry no host path, endpoint, token, business data,
  executable code, or permission definitions. A package upload is read-only.
- `patches.txt` explicitly records that no data patch is required.

## Deliberate constraints

Endpoint hostnames and tokens are site configuration, never release metadata.
HERO source mappings are fixed and versioned. Native ERPNext persistence has
not yet been accepted on a fresh VPS, so it is not represented as completed.
