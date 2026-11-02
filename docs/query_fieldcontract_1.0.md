# Native query field contract

> **Reviewed source contract, not a live target approval.** The accepted bindings below are a code-level allowlist. A saved Draft, configuration import, simulated test binding or generated Task read does not activate a native field or authorize execution. See [Synchronization handoff](sync_handoff_1.0.md) for approval and runtime gates.

`direct_mapping.SOURCE_CONTRACTS` is the existing server-owned operation contract. An operation may declare `native_field_bindings` as a set of exact `(source_path, source_type, target_doctype, target_field, fieldtype, transformation)` tuples. The business-approved generic native Field set is empty. `Customer.alias` is a technical test fixture. It is not an approved mapping for a person's or company's name. Closed purpose-specific native adapters and their reviewed projections stay unchanged.

The rule validator checks:

- the current server contract, Query operation and exact source type
- purpose and transformation
- existing DocType write permission
- protected and reserved fields
- actual field metadata: writable, visible, non-fetched and non-virtual

Native links require their existing reviewed resolver, not a raw foreign id. The server checks the native binding again whenever it reads an active runtime plan. Removing a binding therefore stops a stale saved rule from executing. A native Field rule requires the explicitly selected read-mapping path. The unbound legacy global-plan path cannot execute it. The existing storage and controller boundaries enforce snapshot fingerprint, parent/field identity, personal-draft revision and CAS. Execution also needs a saved Read Service approval for the exact active mapping revision. A change to the code-owned binding is a deployable contract change. It does not auto-approve any mapping or service.

A client, personal Draft, configuration import or schema package cannot add server bindings. Shared mapping imports accept only the exact v1/v2 envelope keys and exact definition keys. They reject unknown package keys. Personal imports keep their exact direction, identity, snapshot, revision and write-permission checks. Schema preview/adoption and its DDL/DML separation stay unchanged.

Synthetic tests may inject multiple bindings into the in-memory contract in an isolated process. Those entries are neither release data nor business approval. Each additional business binding requires semantic evidence and an explicit review. After that, add it as a data-only edit of this contract, with targeted tests. Project naming, protected notes and mutation writeback are independent contracts. This mechanism does not relax them.
