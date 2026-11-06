# HERO Integration file topology

- `hero_integration/`: app, hooks, services, integrations, modules and DocTypes.
- `hero_integration/tests/`: app tests; framework-generated DocType tests remain beside their DocTypes.
- `hero_integration/public/`, `templates/`, `www/`: Frappe assets and templates.
- `.github/workflows/`, `ci/`, `.woodpecker.yml`: CI workflows and scripts.
- `docs/`: technical and operator documentation; root guides remain at the repository root.
- `docs/architecture/hero-integration_f-topology_NN.md`: numbered topology records.
- Root manifests: Python packaging and Frappe app metadata.
- Follow existing Frappe discovery and naming conventions; CI scripts do not own app logic.
