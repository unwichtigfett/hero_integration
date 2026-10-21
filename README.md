# HERO Integration

HERO Integration is a portable Frappe v16 app for approved HERO GraphQL data.
Queries and polling stay read-only toward HERO; controlled mutations use a
separate, default-OFF, per-operation path. It requires an already installed
ERPNext v16 site and creates native Customer and Project records. It has no runtime dependency on
`auto_domain`, `hero_connector`, DTS, Helpdesk, a daemon, or a local filesystem
layout.

Installation does not contact HERO. ERPNext v16 must already be installed; the
pre-install guard verifies its Customer, Contact, Contact Email, Contact Phone,
and Project DocTypes before adding the HERO schema. Helpdesk remains optional.

## Supported scope

- Explicit saved-schema query and mutation operation catalogs, with dynamic
  mapping drafts bound to a source identity and fingerprint.
- Controlled HERO mutation execution only after the global site gate,
  active contract, validated preview, exact confirmation and server-side
  System Manager recheck. Polling never invokes a HERO mutation.
- Preview and execute modes, durable versioned result records, source-to-target
  bindings, local duplicate/concurrency protection, field-level conflict
  detection, and scheduled one-way sync. HERO does not provide an
  idempotency/exactly-once guarantee; an ambiguous remote mutation is audited
  as unknown and is never retried automatically.
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
- [RC16 technical evidence](RC16_CONTEXT_AND_SAFETY_AUDIT.md)

## Adapter tools

The app stores read-only GraphQL schema snapshots and services. It builds a
deterministic API Analyzer graph from a saved snapshot without contacting HERO.
The **Mapping** tab in the canonical **HERO Integration** Desk workspace
requires an explicit saved, validated schema snapshot before it loads HERO
source fields, opens the graph, or supplies searchable source and target pickers. A
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
Hosts**. Redirects and endpoint credentials are rejected. A GraphQL mutation
is rejected unless its separate gated contract is valid. The token is a Frappe
`Password` value; it is not returned to Desk, written to a log, or passed as a
queue argument.

`Allow deletion` is a separate default-OFF server-side gate for all
HERO-owned-document deletion routes. It does not grant deletion of ERPNext or
other application data. The durable, paginated `integration_events.list_outputs`
projection exposes only technical result metadata, source identity, target
reference, correlation/origin, action and changed field names; it never exposes
credentials or source payload values.

## RC16 operational boundary

The reviewed RC16 candidate keeps polling, GraphQL mutation execution and
deletion disabled by default. A read run requires a selected validated GraphQL
service, its selected snapshot, an active read mapping, explicit approval of
both exact revisions, and active canonical mapping definitions. The read
mapping records the response list, external identity, allowed variables and
bounded OFFSET page size; it reuses the existing Customer or Project adapter
and never enables an arbitrary database write. Changing either approved
revision makes the contract ineligible until it is explicitly approved again.
MONITOR records mapped field observations and bilateral conflicts, but never
prepares a reverse HERO write. Browser acceptance and any live HERO step remain
separate, explicitly authorized checks.
