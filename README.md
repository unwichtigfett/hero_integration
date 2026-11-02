# HERO Integration

HERO Integration is a Frappe v16 app that manages HERO GraphQL data. It requires a site with ERPNext v16 installed. It creates native Customer and Project records.
Installation does not contact HERO.
Before it adds the HERO schema, the pre-install guard verifies the ERPNext
Customer, Contact, Contact Email, Contact Phone and Project DocTypes.
Helpdesk is optional.

Install from the branch `hero_int_1.0`:
<https://github.com/unwichtigfett/hero_integration/tree/hero_int_1.0>. See `installation_guide_1.0.md`.

## Supported scope

- HERO Customer, Project Match, Calendar Events and Task operations with fixed,
  versioned selection sets.
- Preview and execute mode, result records, source-to-target bindings,
  idempotent repeats, conflict detection, retry policy and scheduled sync.
- Data flow only after an explicit Company confirmation.
- Linear variable mappings define the data transmission.

## Documentation

- [Installation](installation_guide_1.0.md)
- [Configuration](docs/configuration_guide_1.0.md)
- [Manual acceptance](docs/manual_acceptance_1.0.md)
- [Feature matrix](docs/feature_matrix_1.0.md)
- [Portability report](docs/portability_report_1.0.md)
- [DocType extensions](docs/doctype_extensions_1.0.md)
- [Mapping matrix](docs/mapping_matrix_1.0.md)
- [Desk workspace](docs/ui_overview_1.0.md)

## CI tests

Woodpecker runs database-independent tests and then the complete Frappe site
suite on a disposable site. Tests that require a Frappe database run in the
site suite only. Both test commands must succeed; no failure baseline is
accepted. Optional schema and configuration imports use a separate CI site.
GitHub Actions also provides an isolated selectable-app installation test.

## Adapter tools

The app stores GraphQL schema snapshots, versioned services and cyclic polling. The API Analyzer builds a deterministic graph from a saved snapshot without contacting HERO.
The **Mapping** tab in the **HERO Integration** Desk workspace requires an explicit saved, validated schema snapshot. Only then does it load source fields, open the graph or supply searchable source and target pickers.
Schema-only fields are visible but disabled.
The target picker reads the installed site's Frappe metadata. This covers Custom Fields, identity, Company, status, standard-mapping, standard fields and unresolved child-record fields.
Suggestions are bidirectional and non-binding. They can only create a Draft mapping. An administrator must explicitly validate and activate it.
Custom direct mappings target only writable fields on approved DocTypes.
Before activation, Frappe validates metadata, types, relations, permissions, snapshot fingerprints and the mapping revision.

Fresh installations contain no mapping definitions. The Mapping Editor exports
deterministic, versioned packages. After an import, the editor shows a checklist for HERO-owned Custom DocTypes, Custom Fields, relations and mappings.
A System Manager selects entries and confirms the adoption action once.
The action provisions the schema first. It activates the selected mappings only
after the checks pass. It never triggers a migration by itself.

## Security model

 - The endpoint must use HTTPS.
 - The exact hostname must appear in **Allowed HERO Hosts**.
 - The API token is a Frappe `Password` value. Only a worker reads it. The app
   never returns it to Desk, writes it to a log or passes it as a queue argument.
