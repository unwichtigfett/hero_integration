# HERO Integration file topology — revision 00

- Scope: `unwichtigfett/hero_integration`, Frappe v16 app.

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

Use existing Python/Frappe naming and framework discovery conventions. CI scripts do not own app logic.

## Revision and validation

This is a complete snapshot. Keep approved revisions unchanged; a user-approved change to these rules requires the next unused integer, padded to at least two digits, and a new complete record with its predecessor. Ordinary files within these rules need no new revision. Validate Python imports/tests and workflow syntax when affected; a topology approval does not itself prove implementation or test success.
