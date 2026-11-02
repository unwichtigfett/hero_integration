# HERO-MAPPING-ADR-041: Object- and relationship-oriented mapping

Status: ACCEPTED

## Decision

The mapping editor presents business objects, fields and relationships as the
primary selection. Record mapping identifies a HERO record. It controls reuse
or creation of the Frappe counterpart. Field mapping assigns information to a
business-appropriate target field, with its transformation and write rules.
Relationship mapping connects records through verified identities, roles and
cardinalities. These responsibilities reuse the existing mapping, Draft,
identity and resolver contracts. They need no separate registries or engines.

The validated snapshot supplies the actual HERO types and paths. Query
selection stays part of the technical fetch contract, independent of the
business mapping. Compatible fetch configurations may share a mapping only when
their identity, context and meaning agree. Choosing a field does not extend
read or execution authority. A missing fetch contract stays visible and cannot
implicitly activate a Draft. Field arguments that affect meaning stay part of
the relationship context.

Mutation direction stays separate: Frappe source, HERO action, argument, input
type and input field. Imports never execute actions. They never import
authority, verified identities or acknowledged runtime state.

## Boundaries

Customer, Contact and Address remain canonical linked records. Project uses a
resolved Customer, an independently confirmed Company and a protected title.
Task, ToDo and external deadline use the existing native and authorization
contracts. Event project, participant and resource relationships require
evidence for each identity and source relationship. The existing protection
for local changes, omission, unconfirmed NULL semantics and conflicts stays in
force. Articles, versions, prices, inventory and accounting are outside this
decision.

## Compatibility and consequences

Existing definitions, personal Draft ownership, revisions, publication gates,
Query/Mutation exchange and execution bindings remain authoritative. New
object and relationship context is a backward-compatible Draft extension. It
does not rewrite existing data. You may save schema-only paths for review. The
app never silently promotes them to a supported resolver or approved fetch.
Existing supported resolvers stay usable. This ADR adds no mirror fields,
implicit deletions, completions, unassignments or external calls.

Private acceptance and target deployment are separate. Source time, identity
and authority contracts stay open wherever the saved evidence does not
establish them. Reconsider this decision only if a confirmed source contract
or the existing implementation makes these boundaries incompatible.

## Amendment 2026-10-29: canonical operation generation

For new mapping configurations, the existing GraphQL materializer is the
canonical operation generator. An explicit schema-validated root operation,
mapped paths, identity and typed arguments determine the generated operation.
Operators do not author a GraphQL document. Ambiguous roots require an explicit
binding. Generation is discovery, not permission to fetch or mutate. The
existing service approval, mapping revision, schema, identity, pagination,
writeback and execution gates remain authoritative.

Previously approved stored Query services keep their reviewed-document
contracts. Each one changes only when migrated individually through those
gates. This amendment does not make generated requests executable. It does not
mean that all existing execution paths are migrated.
