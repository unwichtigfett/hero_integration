# Portability and upgrade boundaries

**Reviewed:** 2 November 2026 · **Scope:** source configuration, packaging, Frappe hooks and upgrade registries. This is not a production portability or recovery acceptance.

## Portable by design

- The app uses Frappe document, site, scheduler, queue and encrypted password APIs.
- ERPNext v16 must be installed first. The `required_apps` hook is intentionally absent; the pre-install guard checks the prerequisite without installing ERPNext as a side effect.
- The nested Frappe module tree provides DocTypes and Desk assets in framework-discovered paths.
- The endpoint, host allowlist and API token belong to site configuration, never release metadata. They must not be shipped in mapping packages or exposed in logs.
- Schema-package preview and actual schema provisioning are separate security and mutation boundaries. Frappe DDL cannot generally be undone by rolling back an application transaction.

## Database migrations exist

An earlier version of this report incorrectly said no data patches were necessary. [`hero_integration/patches.txt`](../hero_integration/patches.txt) registers pre- and post-model-sync migrations for mapping normalization, schema and definition backfills, link indexing and other app setup. These are historical upgrade contracts and must not be deleted merely because a fresh install creates the latest schema.

The [assignment-intent framework patch](../framework_patches/frappe_assignment_intent_v2/frappe_patch_1.0.md) is separately distributed and bound to a specific Frappe revision and file hash. It is not silently installed with this app. Task assignment fails closed if its required native framework extension is absent.

## Release checks

Compare the exact Git revision, Frappe/ERPNext and Python versions, installed DocType metadata, site patches, approved source instance, schema fingerprint, mapping/service revisions, permissions and recovery tests. A historical source scan is not a new guarantee that every environment is portable.

**Status:** Source configuration reviewed; deployment and runtime acceptance remain `EXECUTION_UNVERIFIED`.
