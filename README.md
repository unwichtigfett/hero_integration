# HERO Integration

- Connects HERO GraphQL to Frappe/ERPNext v16 through approved mappings, read services and guarded synchronization.
- Installation does not contact HERO or activate mappings.
- Migration areas: employees, contacts, projects, events, tasks, documents, pictures, articles and services.
- Identity: source instance and HERO ID, recorded in `HERO Integration Link`; missing relationships can remain pending.
- External writes require a separately approved mutation contract, permissions, preview and confirmation.
- HERO credentials remain in Frappe Password fields; configure an allowlisted HTTPS host.
- UI language follows the User language or System Settings; Navy is available in the native appearance selector.

## Documentation

- [Installation](installation_guide_1.0.md)
- [Feature matrix](docs/feature_matrix_1.0.md)
- [Mapping packages](docs/complete_json_mapping.md)
- [Employee mapping](docs/employee_mapping.md)
- [DocType extensions](docs/doctype_extensions_1.0.md)
- [Query field contract](docs/query_fieldcontract_1.0.md)
- [App integration](docs/app_integration_1.0.md)
- [Desk workspace](docs/ui_overview_1.0.md)
- [Portability](docs/portability_report_1.0.md)
- [GraphQL materializer](docs/materializer_1.0.md)
- [File topology](docs/architecture/hero-integration_f-topology_00.md)
