# HERO Integration file topology — revision 00

- Scope: `unwichtigfett/hero_integration`, Frappe v16 app.
- Decision: adopted by the user on 2026-11-02 after review of the proposed locations.
- Predecessor: none. Source revision at save: `0636dd1af0448a0cf492dc7f0e5d87860ae9bb1d`.

## Ownership and placement

| Path | Responsibility |
| --- | --- |
| `hero_integration/` | Python app, hooks, integrations, modules and DocTypes. |
| `hero_integration/tests/` | App-level automated tests; keep framework-generated DocType tests beside their DocTypes. |
| `hero_integration/public/`, `templates/`, `www/` | Frappe assets and templates in their framework discovery locations. |
| `.github/workflows/` | GitHub Actions workflow YAML. |
| `ci/` and existing `.woodpecker.yml` | CI scripts and Woodpecker workflow. |
| `docs/` | Technical and operator documentation; retain existing root guides. |
| `docs/architecture/hero-integration_f-topology_NN.md` | Immutable numbered topology snapshots. |
| Root package manifests | Existing packaging and framework metadata. |

Use existing Python/Frappe naming and framework discovery conventions. CI scripts do not own app logic. Generated caches, site data, credentials, backups, dependencies and build outputs do not belong in source control. Preserve existing framework metadata and legacy documentation locations; this agreement does not authorize moving them.

## Revision and validation

This is a complete snapshot. Keep approved revisions unchanged; a user-approved change to these rules requires the next unused integer, padded to at least two digits, and a new complete record with its predecessor. Ordinary files within these rules need no new revision. Validate Python imports/tests and workflow syntax when affected; a topology approval does not itself prove implementation or test success.

## Sources and constraints

- [Frappe Apps](https://docs.frappe.io/framework/user/en/basics/apps), accessed 2026-11-02: app package, hooks, modules and discovery locations. The project targets Frappe v16; the documentation is not version-pinned.
- [GitHub Actions workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax), accessed 2026-11-02: workflow YAML belongs in `.github/workflows/`.
- Other locations above are project conventions agreed by the user, not framework requirements.
