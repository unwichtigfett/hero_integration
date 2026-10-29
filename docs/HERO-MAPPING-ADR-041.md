# HERO-MAPPING-ADR-041: Object- and relationship-oriented mapping

Status: ACCEPTED

## Decision

The mapping editor presents business objects, fields and relationships as the
primary selection. Record mapping identifies a HERO record and controls reuse
or creation of its Frappe counterpart. Field mapping assigns information to a
business-appropriate target field with its transformation and write rules.
Relationship mapping connects records through verified identities, roles and
cardinalities. These responsibilities reuse existing mapping, Draft, identity
and resolver contracts; they do not require separate registries or engines.

The validated snapshot supplies actual HERO types and paths. Query selection
remains part of the technical fetch contract, independently of the business
mapping. Compatible fetch configurations may share a mapping only where their
identity, context and meaning agree. Choosing a field does not extend read or
execution authority. A missing fetch contract remains visible and cannot
implicitly activate a Draft. Field arguments that affect meaning remain part
of the relationship context.

Mutation direction remains separate: Frappe source, HERO action, argument,
input type and input field. Imports never execute actions or import authority,
verified identities or acknowledged runtime state.

## Boundaries

Customer, Contact and Address remain canonical linked records. Project uses a
resolved Customer, an independently confirmed Company and protected title.
Task, ToDo and external deadline use the existing native and authorization
contracts. Event project, participant and resource relationships require
evidence for each identity and source relationship. Local changes, omission,
unconfirmed NULL semantics and the existing conflict rules remain protected.
Articles, versions, prices, inventory and accounting are outside this decision.

## Compatibility and consequences

Existing definitions, personal Draft ownership, revisions, publication gates,
Query/Mutation exchange and execution bindings remain authoritative. New
object and relationship context is a backward-compatible Draft extension;
existing data is not rewritten. Schema-only paths may be saved for review but
are not silently promoted to a supported resolver or approved fetch. Existing
supported resolvers must remain usable. No new mirror fields, implicit
deletions, completions, unassignments or external calls follow from this ADR.

Private acceptance and target deployment are separate. Source time, identity
and authority contracts remain open wherever the saved evidence does not
establish them. Reconsider this decision only if a confirmed source contract
or existing implementation makes these boundaries incompatible.

## Amendment 2026-10-29: canonical operation generation

For new mapping configurations, the existing GraphQL materializer is the
canonical operation generator. An explicit schema-validated root operation,
mapped paths, identity and typed arguments determine the generated operation;
operators do not need to author a GraphQL document. Ambiguous roots require an
explicit binding. Generation is discovery, not permission to fetch or mutate.
The existing service approval, mapping revision, schema, identity, pagination,
writeback and execution gates remain authoritative.

Previously approved stored Query services retain their reviewed-document
contracts until individually migrated through those gates. This amendment does
not make generated requests executable or imply that all existing execution
paths have already been migrated.
