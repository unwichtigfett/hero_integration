# HERO Integration

HERO Integration is a Frappe v16 app for managing HERO-GraphQL data. Requires installed ERPNext v16 site. Creates native Customer and Project records.
Installation does not contact HERO.
The pre-install guard verifies erpNext Customer, Contact, Contact Email,
Contact Phone and Project DocTypes before adding the HERO schema.
Helpdesk is optional.

## Supported scope

- HERO Customer, Project Match, Calendar Events and Task operations with fixed,
  versioned selection sets.
- Preview and execute mode, result records, source-to-target bindings,
  idempotent repeats, conflict detection, retry policy, and scheduled sync.
- Dataflow after an explicit Company confirmation.
- Data transmission is defined by linear variable-mappings.

## Documentation

- [Installation](INSTALLATION.md)
- [Configuration](docs/CONFIGURATION.md)
- [Manual acceptance](docs/MANUAL_ACCEPTANCE.md)
- [Feature matrix](docs/FEATURE_MATRIX.md)
- [Portability report](docs/PORTABILITY_REPORT.md)
- [DocType extensions](docs/DOCTYPE_EXTENSIONS.md)
- [Mapping matrix](docs/MAPPING_MATRIX.md)
- [Desk workspace](docs/UI.md)

## Adapter tools

App stores GraphQL schema snapshots, versioned services and cyclic polling. Deterministic API Analyzer graph from saved snapshot without contacting HERO.
**Mapping** tab in **HERO Integration** Desk workspace requires an explicit saved, validated schema snapshot before loading source fields, opens the graph, or supplies searchable source and target pickers.
Schema-only fields are visible but disabled.
Target picker reads the installed site's Frappe metadata, including Custom Fields, identity, Company, status, standard-mapping, standard-fields, and unresolved child-record fields.
Suggestions are bidirectional, non-binding and can only create a Draft mapping. An administrator must explicitly validate and activate it.
Custom direct mappings only target writable fields on approved DocTypeS.
Frappe validates metadata, types, relations, permissions, snapshot fingerprints, and mapping revision pre-activation.

Fresh installations contain no mapping definitions. The Mapping Editor exports
deterministic, versioned packages. The editor shows a checklist for HERO-owned Custom DocTypes, Custom Fields, relations, and mappings after import.
A System Manager selects entries and confirms adoption action, once.
Action provisions schema first and activates selected mappings only after
checks pass. It never triggers a migration by itself.

## Security model

 - Endpoint must be HTTPS.
 - Exact hostname must appear in **Allowed HERO Hosts**.
 - API token is a Frappe `Password` value, read only by a worker, it
   is not returned to Desk, written to a log or passed as queue argument.
