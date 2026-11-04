# Complete JSON mapping packages

The Mapping page remains a list with JSON import and export. A configuration file
can include all nine migration areas: customers, projects, calendar events, tasks,
products, documents, pictures, services and employees. Project histories belong to
the project reader and project migration; they do not require a tenth migration.

## Import contract

`hero-integration-config-v2` retains its existing schema, settings, mapping package,
read services and `replace_mappings` members. The optional `mutation_mappings`
member contains portable `MAPPED_UPDATE_V2` definitions for the same areas. Install
this application revision before importing a file that uses these extensions.

An explicitly confirmed replacement import provisions declared integration fields,
publishes the read mappings through their normal revision history, approves each
reader, and retires superseded read mappings. It saves mutation definitions as
personal drafts. It does not activate mutation contracts, enable execution, start
migrations, download files or issue HERO mutations. Prepared mutation assignments
appear in the existing Mutation list with Draft status. Query export includes the
current user's prepared mapped updates as well as active mapped updates.

Before writeback activation, the target must have a separately reviewed, active
mutation contract and validated service. Sending then requires native permissions,
a unique source binding, the approved Query mapping, unchanged source and local
baselines, a fresh preview fingerprint and explicit confirmation. Only updates
are supported by these definitions; creating and deleting remote entities remain
separate workflows.

## Native data and relationships

Native ERPNext fields take precedence over additional integration fields. Customer
names use an explicit company/full/first/last-name fallback. Contact phone and email
values use native child rows. Secondary addresses, customer contact persons, bank
accounts and project addresses use source-bound native records with conflict
baselines. Equal names, email addresses or account numbers never authorize merging
unbound records. Repeated imports retain one source binding and preserve local edits.

Products retain read-only stock observations; importing a mapping never posts
inventory. Explicit source sales-price-list bindings identify native Item Price
records, their currency and whether a missing Price List may be created. Unknown
source price lists block the record instead of silently dropping prices. A mapped
product identifier remains distinct from the version identifier required by HERO's
update mutation.

Employee imports use the confirmed ERPNext company and the agreed joining date,
2026-01-01. New local users are active, receive only the Employee role and no welcome
email. HERO roles, account types and permissions do not grant ERPNext privileges.
Birth date and gender remain optional.

## Project logbook

The approved project query explicitly selects the history fields, collection
identity, visibility and context paths. One project per page allows its independent
history cursor to advance using the exact approved document. Even short history
pages are followed until an empty page; repeated identities and exhausted bounds
fail visibly.

Entries map native Comment content, subject, author, email and source timestamps.
Weather, attachment identities and the other declared history context are retained
in the existing context field. Restricted entries stay outside the shared project
timeline. Existing comments are source-bound, deduplicated and reconciled; local
edits raise a conflict instead of being overwritten. Preview validates histories
without creating a Project or Comment.

## Coverage limits

Coverage reports distinguish native mappings, declared relationships, derived
aliases, technical identities and deliberately excluded operational/security data.
They are not a claim that arbitrary recursive GraphQL objects should become ERPNext
fields. Authentication settings, external ACLs, transient capabilities and calculated
search strings are not duplicated. Warehouse ledger migration and generated sales
or purchasing documents are separate workflows.

Writable coverage follows the actual HERO input schema and the effective ERPNext
metadata. Output-only values, account identities, lifecycle operations, locale
regions without an explicit policy and relationships without a verified inverse
are not silently converted into writable assignments. Import validation is not
proof of successful live HERO writeback; no live mutations are required to import
or verify a mapping package.
