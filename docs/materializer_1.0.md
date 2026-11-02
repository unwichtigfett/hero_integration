# Internal GraphQL materializer

HERO Integration ships an internal, versioned pure-Python GraphQL materializer. It originated in `hero_connector` revision `6908a5acf7fe00726cc379c9b1d72824bc7a601d`. The original source revision and file hashes are recorded in `hero_integration/graphql_protocol/SOURCE_PROVENANCE.json`.

The package renders deterministic operations, handles typed values and selections, decodes results and generates request fingerprints. It does not depend on `hero_connector`, Frappe, a site database or HTTP transport.

## How it is used now

The current source includes `services/materializer_routing.py`, mapping-aware operation generation and generated read-only Task service drafts. These callers provide validated snapshots, selected rules and identity context; they **do not** obtain execution permission just by producing a valid GraphQL document.

Legacy reviewed Query services keep their stored contracts until each is explicitly migrated and approved. Generated Task reads require matching active mapping and service approvals and revalidation before every page. A Mutation proposal is technical metadata, not a grant to write to HERO.

The materializer does not bypass permission checks, personal Draft ownership, CAS, source binding, request budgets, read-service approval, native field protection, mutation audits or no-blind-retry behavior.

**Evidence:** Implemented source behavior, not proof that every legacy executor has migrated or that external HERO runtime execution has been accepted.
