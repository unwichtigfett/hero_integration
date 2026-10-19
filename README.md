# HERO Integration

HERO Integration is a portable Frappe v16 app for approved, read-only HERO
GraphQL data. It requires an already installed ERPNext v16 site and creates
native Customer and Project records. It has no runtime dependency on
`auto_domain`, `hero_connector`, DTS, Helpdesk, a daemon, or a local filesystem
layout.

Installation does not contact HERO. ERPNext v16 must already be installed; the
pre-install guard verifies its Customer, Contact, Contact Email, Contact Phone,
and Project DocTypes before adding the HERO schema. Helpdesk remains optional.

## Supported scope

- Read-only HERO Customer and Project Match operations with fixed, versioned
  selection sets.
- Preview and execute modes, result records, source-to-target bindings,
  idempotent repeats, conflict detection, retry, and scheduled one-way sync.
- Customer and Project creation after an explicit Company confirmation.
- Create-only, conflict-safe Customer and Project Custom Fields for source IDs,
  display values, raw source attributes, and external timestamps; native Contact
  Email and Phone persistence where ERPNext provides a matching field.

The app does not migrate attachments or Helpdesk tickets and has no reverse
sync. Those functions are visibly reported as unavailable rather than claimed
as part of Cheap or Full migration.

## Documentation

- [Installation](INSTALLATION.md)
- [Configuration](CONFIGURATION.md)
- [Manual acceptance](MANUAL_ACCEPTANCE.md)
- [Feature matrix](FEATURE_MATRIX.md)
- [Portability report](PORTABILITY_REPORT.md)
- [DocType extensions](DOCTYPE_EXTENSIONS.md)
- [Mapping matrix](MAPPING_MATRIX.md)

## Adapter tools

The app stores read-only GraphQL schema snapshots and services. It builds a
deterministic API Analyzer graph from a saved snapshot without contacting HERO.
The **ERPNext Mapping** tab in the canonical **HERO Integration** Desk
workspace opens the graph and supplies searchable source and target pickers. A
source picker exposes only fields selected by the saved read-only operation;
schema-only fields are visible but disabled. The target picker reads the
installed site's Frappe metadata, including Custom Fields, and disables system,
identity, Company, status, standard-mapping, standard-field, and unresolved
child-record fields. Site-specific direct mappings can only write a writable
Custom Field. Suggestions are bidirectional and non-binding.
They can create a Draft mapping only; an administrator must explicitly validate
and activate it. Custom direct mappings only target writable fields on the
approved Customer or Project DocType. Frappe validates metadata, types,
relations, permissions, snapshot fingerprints, and mapping revision before
activation.

Fresh installations contain no mapping definitions. The Mapping Editor exports
deterministic, versioned packages and previews an uploaded package against the
selected schema fingerprint. An upload changes nothing. The editor shows one
checklist for HERO-owned Custom DocTypes, Custom Fields, relations, and
mappings. A System Manager selects entries and confirms one adoption action.
The action provisions schema first and activates selected mappings only after
the target checks pass. It never starts a migration.

## Security model

The endpoint must be HTTPS and its exact hostname must appear in **Allowed HERO
Hosts**. Redirects, endpoint credentials, GraphQL mutations, and subscriptions
are rejected. The token is a Frappe `Password` value, read only by a worker; it
is not returned to Desk, written to a log, or passed as a queue argument.
