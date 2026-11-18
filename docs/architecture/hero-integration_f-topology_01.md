# HERO Integration — File Topology Convention

## Identity and decision

| Field | Value |
| --- | --- |
| Project / slug | HERO Integration / `hero-integration` |
| Scope | Existing `hero_integration` app; no parallel app or framework fork |
| Document | `docs/architecture/hero-integration_f-topology_01.md` |
| Revision / predecessor | 01 / [original layout record](hero-integration_f-topology_00.md) |
| Decision | ACCEPTED |
| Agreement | User answered “ja” and “1” to the current chat's concrete code, test and file-placement proposal; session date 2026-11-16 |
| Source baseline | `hero_int_dev`, `6f802e88f6bdc6f8de2e35ca9fce089402fd0b31` |
| Implementation / testing | Authorized next step; not proof of implementation, testing or deployment |

## Constraints and sources

Frappe 16.54.0 and ERPNext 16.53.0 were read from the running dev image.
The official [Hooks documentation](https://docs.frappe.io/framework/user/en/python-api/hooks),
sections “Override Form Scripts” and “Include JS / CSS”, was consulted on the
session date above. Its unversioned guidance was checked against the installed
Frappe `frappe/desk/form/meta.py`, which loads `doctype_js` through
`add_code_via_hook`. App-relative asset paths and hook registration are framework
constraints; the service and test subdivisions below are existing local choices.
`pyproject.toml` remains the packaging authority.

## Path and ownership rules

| Path | Responsibility and rules |
| --- | --- |
| `hero_integration/hooks.py` | Native hooks; extend existing registrations without replacing unrelated behavior |
| `hero_integration/services/` | App-owned Python services; permission and field-access checks stay server-side |
| `hero_integration/integrations/` | Existing external-system adapters; UI code must not introduce another writeback path |
| `hero_integration/hero_integration/doctype/` | Framework-discovered DocTypes and controllers; no new schema is authorized by this step |
| `hero_integration/public/js/` | Native Desk extensions registered by hooks; use the current form and native actions |
| `hero_integration/public/css/` | App assets; Project-specific rules must be scoped to the Project extension |
| `hero_integration/tests/` | Existing Python `test_*.py` and Node `test_*.mjs` conventions; synthetic fixtures under `fixtures/` |
| `hero_integration/templates/`, `www/` | Existing Frappe template and website discovery rules, when applicable |
| `hero_integration/patches/` | Existing migration history; do not repurpose or rewrite historical patches |
| `docs/` | English technical and operator documentation; existing root guides remain in place |
| `docs/architecture/hero-integration_f-topology_NN.md` | Immutable complete topology snapshots; monotonically increasing integer suffix, at least two digits |
| `.github/workflows/`, `ci/`, `.woodpecker/` | Existing CI; no second pipeline for this feature |
| Root manifests | Python packaging and Frappe metadata remain at their existing canonical paths |

Generated `public/dist/`, caches, egg metadata, dependencies and runtime
sites/files are not hand-edited source. Ignored documents and unrelated
worktrees remain user-owned; this agreement does not authorize their cleanup.
No credentials or live business data belong in fixtures or documentation.

## Changes and implementation boundaries

This adopts and completes the existing layout without moving files or changing
dependency direction. Revision 00 is retained unchanged. There are no newly
approved layout exceptions.

The separately approved implementation is a native Project UI extension,
permission-checked reads and offline tests. It must preserve native fields,
save/validation behavior, file privacy and existing HERO confirmations.
Database changes, deployments, service control, new stored relationship/category
models, external writes and unrelated UI changes are excluded.

Validate hook registration, package inclusion, import paths and the affected
offline tests when implementation is complete. This agreement is not runtime
acceptance. The README “File topology” link is the current entry point.

## Revision policy

Preserve this snapshot and all predecessors byte-for-byte. Ordinary new files
that fit these rules need no revision. A rule change needs a reviewed successor,
using the next unused integer after the greatest allocated suffix; never fill
gaps or silently overwrite history.
