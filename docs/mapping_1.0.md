# Object- and relationship-oriented mapping

See [configurable Products, Documents and Pictures mappings](configurable_direct_mappings.md) for direct-operation rules and setup.

> **Architecture decision, not deployment evidence.** This records how mapping ownership and generated GraphQL operations should work. The [Feature matrix](feature_matrix_1.0.md) describes the current source implementation, and [Synchronization handoff](sync_handoff_1.0.md) tracks execution gates. Schema-derived operations remain drafts until their exact mapping/read/action approvals are satisfied.

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
Existing supported resolvers stay usable. This decision adds no mirror fields,
implicit deletions, completions, unassignments or external calls.

Private acceptance and target deployment are separate. Source time, identity
and authority contracts stay open wherever the saved evidence does not
establish them. Reconsider this decision only if a confirmed source contract
or the existing implementation makes these boundaries incompatible.

## Amendment canonical operation generation

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

## Creating and exchanging mappings

Query and Mutation provide the same visible entry points for creating a mapping
and importing JSON drafts. A new mapping starts with a saved, validated schema
and an operation. Source and target fields are selected in the editor. Before
the first save the configuration is local; saving creates the current user's
draft without requiring an existing active definition. The field editor labels
the assignment simply **Mapping**. Draft status is shown separately. Source,
target, transformation and save/activation controls are visible in the main
workspace. Technical controls are grouped in one flat **Advanced** section.

For queries, HERO is the source and ERPNext is the target. For mutations, ERPNext
is the source and an actual HERO mutation input is the target. Query fields
cannot simply be reversed when inputs, transformations or identity semantics
differ. Mutation publication and execution retain their existing independent
contracts.

Directional JSON import first previews the file for the selected source,
snapshot and direction. Confirmation saves drafts using the preview fingerprint
and expected revisions. It does not publish mappings or execute HERO operations.
Export contains the current user's open drafts in that context. The complete
nine-area configuration package remains a separate Settings import; that file
is not a directional draft file.

## Simple Mapping editor (2026-11-04)

**Mapping** opens the simple editor, laid out after the owner's sketch:
- **Query / Mutation** tabs and the schema snapshot at the top.
- **Map Fields**: Source (Operation, Object, Field) and Target (App, DocType, Field). Each box shows ID, Name, Type, Node, Revision and Status of the selection.
- **Create** adds the rule and applies it immediately.
- **Active Mappings** lists every rule with Status, Operation, HERO path, Target App, Target and Revision. You can filter by operation, search text and a type/value pair. **Activate** re-links inactive field rules; **Delete** unlinks field rules.
- **Mapping Import** takes a configuration file (`config_import`).

Each change publishes a new revision of the area's read mapping through the personal-draft publisher. It then approves the area's read service again. The editor chooses the transformation from the field types: Link → Existing Record, formatted text → Plain Text To HTML, text from text → Trim, numbers to text → Positive Int To Text / Scalar Text, otherwise Identity. The server validates each rule as usual.

Field rules of Customers and Projects can be edited here. Events, Tasks, Products, Documents and Pictures have fixed or code-owned bindings; they are listed but not editable. Identity, name and relation rules cannot be removed. Mutation (send) mappings are listed; you create them under **Advanced**, which opens the previous full editor.
