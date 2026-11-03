# Configurable Query and Mutation mappings

See [configurable Products, Documents and Pictures mappings](configurable_direct_mappings.md) for direct-operation rules and setup.

## Execution boundary

Configurable native adapters use the existing personal Mapping Draft, revision,
publication and saved-service approval flow. They reuse the existing native
writers and Integration Link identities. They do not enable the unfinished B8
writer or grant execution merely by importing a package.

Project title composition remains server code. The existing per-field writeback
contract is unchanged. Native **Send to HERO** uses a separate, grouped Mutation
mapping; it has no automatic fallback to the historical fixed sender.

Existing approved Query mappings keep their legacy behavior until explicitly
upgraded. There is no automatic activation or rewriting of installed mappings.

## Query configuration

Open a Query mapping's personal draft and choose **Configure Query adapter**
under its rule details. Select adapter version `2` and the validated saved read
service. Edit field rules through the existing source and target pickers, then
save and publish the draft. Approve the resulting read service against the new
mapping revision before starting synchronization.

The saved Query must actually select the configured fields, source identity,
relationship identities and required native guard fields. Editing a mapping
never silently adds fields to an already approved fetch. Publication seals the
exact Query document, selected rules, options, snapshot and source. Changing
any of those invalidates the old approval.

An example adapter section, with illustrative rule IDs:

```json
{
  "version": 2,
  "read_service": "HERO-GQL-EXAMPLE",
  "transform_options": {
    "selected-trade-rule": {
      "map": {"PV": "Photovoltaik", "ELEK": "Elektrohandwerk"},
      "link_doctype": "Project Type",
      "target_type": "Link"
    },
    "selected-date-rule": {
      "timezone": "Europe/Berlin",
      "target_type": "Date"
    }
  }
}
```

Use the actual selected rule identities. The draft records current target types
from site metadata when they are missing. Supplied types must match the site;
the compiler rejects incompatible scalar conversions and protected child fields.
Options are available per selected rule through **Configure transformation
options**. They participate in the mapping fingerprint and portable identity
remapping.

| Data | Configuration |
| --- | --- |
| Project phase | Select the status step's name and map it to the desired supported Project observation field. No independent status-step refresh runs for v2. |
| Trade and department | Use explicit `Link Map` rules with the target reference type and conversion table. The title builder retains its existing trade/title rules. |
| Tasks | Select only the fields to import. Date, completion and assignment adapters retain native validation and assignment policy. Unselected lifecycle fields are not derived or written. |
| Calendar | Select Event fields and explicit native time groups. Source/target timezone, all-day and recurrence rules remain mandatory; native safety/privacy defaults remain server-owned. |
| Primary Address | Map the members of the source Customer's direct `address` relation to Address fields. Country and required Address type need explicit rules; v2 assumes no country. |
| Primary Contact | Map the selected name, salutation, email and phone fields through the exclusive primary Contact adapter. |
| Salutation and customer type | Existing named transformations remain available; `Value Map` or `Link Map` allows explicit conversion tables. |

`Value Map` and `Link Map` never guess unknown values. A configured default is
explicit; an omitted response field remains missing evidence. `Local Date` and
`Local Datetime` require an IANA timezone. Choose the Frappe system timezone
when writing local Datetime fields. `Safe HTML` sanitizes rich text. `Number`
and `Integer` reject invalid/nonfinite values rather than relying on Frappe's
implicit numeric conversion.

For `Table Rows`, declare source-member to target-child-field assignments in
`child_fields`, target types in `child_types`, and needed conversions in
`child_transforms`. For example, an integer HERO ID written to a Data child
field needs `Scalar Text`. Hidden, privileged, computed and read-only children
remain protected.

### Project logbook

Logbook entries are an inline collection of the Project Query. Configure its
path, child identity, collection bound and optional heading paths:

```json
{
  "path": "histories",
  "identity_path": "id",
  "limit": 100,
  "heading_paths": ["histories.custom_title", "histories.author_name"]
}
```

Place this object in `query_adapter.logbook`. Add explicit child rules such as
`histories.custom_text` to `Comment.content` with `Safe HTML`, and optionally
`histories.created` to the registered `Comment.creation` timestamp slot with
`Local Datetime`. The saved Query must select these paths and use the declared
nested `first` bound. Hitting that bound blocks the collection for review;
it is not reported as a complete history import.

Source instance, parent Project identity and History identity jointly prevent
duplicates. Existing legacy Comments are reused only after unique source and
Project ownership proof. Their content and dates are retained. Remote history
edits or deletions do not rewrite previously imported activity entries.

### Read-only lookups

A saved lookup Query may use `target_operation: "HeroLookup"` with
`query_adapter.role: "LOOKUP"`, `version: 2`, an explicit `read_service` and
`max_records`. It has no business writer or scheduler area. Its separate
execution approval, schema/source seal, identity and result bound still apply.
Nonpaginated lookups execute once; declared offset pagination is bounded.

## Native Send to HERO

Open a Mutation mapping's personal draft and choose **Configure Mutation usage**,
then **Send to HERO**. Keep the primary assignment and additional assignments as
one group. The field-writeback usage remains separate.

A Send profile declares:

- The approved read service for baseline comparison and post-mutation readback.
- The technical identity argument/field, supplied only from the Integration Link.
- Each Frappe source field, exact mutation input path and selected transformation.
- Each assignment's resolver, readback path and missing/null policy.
- Any related server-owned Address identity, explicit lookup or local predicate.

No active profile, an ambiguous profile, stale revisions, an unconfirmed source,
or mismatched readback blocks sending. Updates also require an existing baseline. A validated Draft alone
never authorizes a mutation. The existing global execution gate and active
schema-bound technical Mutation Contract remain required.

Resolvers include direct fields, plain text, dates, explicit value tables and
supported native User/Project/phase relationships. Related records come only
from proven native links and require document permission. Phase choices are
limited to the bound Project's configured active-step lookup. User and Project
identities are not interchangeable merely because both use GraphQL `Int`.
User lookups must select the email and ID of the same schema `User` object.
Unreviewed foreign-key inputs cannot use a direct scalar or value-table resolver.

Each assignment has a declared `read_path`. If its approved Query projection
uses another scalar for the Frappe value, declare `query_path` as well. The
corresponding selected Query rule supplies canonical baseline conversion.
An unavailable or ambiguous projection blocks the Send.

A conditional assignment can declare `when` with a source-local `field`, scalar
`equals` and explicit `otherwise` policy. This can express sending a completion
date only for a completed Task, including a reviewed clear when reopening.
Condition fields receive the same source metadata and concurrency checks.

Sending performs fresh source checks, baseline comparison, one mutation and
field readback. It never retries a mutation. Local acknowledgment locks are
acquired after HTTP; they compare the reviewed values and merge current,
unrelated baseline entries. A post-reservation failure rolls back this action's
local changes and callback queues before finalizing its failed audit.

### Creating a Task

Keep separate `send_options.mode` profiles for `CREATE` and `UPDATE`. A genuinely
unbound Task selects `CREATE`; an existing binding selects `UPDATE`. Creation is
limited to Tasks and uses the schema's `update_task(task: TaskInput)` operation
without an `id`. It requires an explicit, nonempty title assignment and a
`PROJECT_LINK` assignment to an existing Project in the same confirmed source.

Creation has no previous remote baseline. After the one-shot mutation, the
approved Task Query must find the returned ID and verify every sent field.
Only then may the app create the Integration Link and canonical field baselines,
using that Query plan's mapping version. The Query must include newly created
Tasks within its approved filters and pagination bound.

An attempted creation that cannot be verified leaves a durable audit block for
that Task, site and source. Editing the Task or obtaining a new preview does
not permit another creation. Reconcile the external result manually first;
there is no automatic retry. A failure proven to precede the transport does
not latch an uncertain external creation.

## Verification boundary

Tests use synthetic records, schema fixtures and mocked HERO clients. An
optional local full HERO schema can be supplied through `HERO_REFERENCE_SCHEMA`;
it is not a credential and no network request is made by that check.

These checks do not establish a live HERO mutation or target-site migration.
Any live mutation acceptance for this change is restricted to the explicitly
identified test project. No production object is authorized as a test fixture.

### Legacy project history pagination and visibility

The legacy reader exhausts both `project_matches` and each nested `histories`
collection, including short server pages. It never substitutes the differently
filtered `project_histories` root. Repeated identities, lost parents and page
bounds fail visibly. Existing content and historical timestamps remain immutable.
Native Comment subject/author fields are filled only when empty. Source-only
scalars, visibility, weather and attachment IDs are retained in a single hidden
source-context field, without creating duplicate business fields.

Nonempty source role restrictions are not interpreted as local ERPNext roles.
Those comments are referenced from their System Manager-only Integration Link,
outside the general Project timeline. The original Project relation is retained.
An already restricted entry is never automatically made public. Attachment IDs
are source references; they do not prove a downloaded file or a migrated mail.
