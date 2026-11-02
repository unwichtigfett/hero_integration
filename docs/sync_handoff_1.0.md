# Synchronization handoff: Customer, Project and Task

Mapping drafts, approved read contracts and execution authority
are separate. This delivery does not authorize synchronization or external
mutations. Before execution, verify the active mapping revision, the saved read
service revision, the source instance and the schema fingerprint.

Customer intake uses the HERO source identity and the existing integration link.
Customer, primary Contact and Address are native records. The existing adapters
link them. Updates to shared Contact or Address records require proven
authority. The current paths preserve shared records and report a conflict.
Names and email addresses are not source identities.

Project intake uses four inputs: the HERO Project identity, a resolved Customer
link, a separately confirmed local Company and the approved server-side title.
Partner notes stay separate from native Project notes. The app never guesses an
unresolved Customer or Company from another source identifier.

Native Task intake is scoped to source instance and Task type. Its integration
link identifies the source by system, instance, type and source ID. Creation
requires a subject. The description is optional; the app writes it only when its
rule is selected. Field baselines detect concurrent local and source changes.
Replay keeps the same identity. Conflicting changes require review. Omitted
values stay unchanged. Empty or NULL selected text requires review and does not
clear a field. Source deletion does not delete or close the Task. A mapping
revision change requires review of the existing binding.

An owned saved Task draft can generate a read-only service draft. Publication
binds the generated document to the selected active Task rule contents, source
instance and snapshot. A validated generated service permits publication. It
does not grant a read: a System Manager must separately approve the resulting
active mapping and service revisions. Before each generated page, the executor
revalidates that approval and the locked rule contents. It then uses the
existing HERO client and result decoder. Legacy reviewed documents keep their
existing contract. This source implementation and its offline tests do not
establish Frappe database, assignment-boundary or external HERO runtime
acceptance.

The Query and Mutation exchange files have separate directions. Each has its own
owner, snapshot and revision boundaries. Mutation drafts are technical
metadata. They neither demonstrate nor authorize an external mutation. Before
you import, export and preview the administrator's complete current context.
The preview must show no unintended removal or replacement. Schema provisioning
has its own guard. Include it in a package only when the target needs it.

The personal Draft exchange validates selected field-rule identities against
the target. It keeps target-managed provenance. An authenticated preview binds
the package and observed references to the site and owner. It is not a runtime
grant. The import skips registry instances, acknowledgements and execution
state. The bounded exchange locks existing rule identities and the referenced
metadata. Supported GraphQL Mapping writers share a
site/source/snapshot/direction lock. The preview binds the complete active
context. Raw database writes and arbitrary administrator DDL stay outside that
boundary. The existing reviewed writeback contract checks primary Customer and
Project field-action parameters. The target-side integration link remains the
identity authority.

Calendar uses the existing native Event projection and the source-instance /
CalendarEvent identity. A non-null `project_match_id` resolves only through the
existing Project integration link and Project read permission. A missing or
NULL value does not remove an existing relationship. Nullable omitted fields do
not clear values or their acknowledged baselines. Local edits stay visible to
later conflict checks, even after an independent source-field update. The app
blocks an unknown recurrence flag. It also holds explicit recurrence until a
reviewed rule exists. For an existing bound Event, the app holds the whole time
group and records a review conflict. Independent field changes still proceed. A
new Event still requires the necessary validated inputs. A validated schema
alone does not confirm the source timezone, all-day boundary or participant
identity. The private editor Draft carries unconfirmed time options and does
not authorize execution. Raw partner/resource observations are not native
participant relationships. The app infers no Customer association from a
Project or Partner identifier.

Missing or unverified:

- Task assignment and ToDo transfer
- external deadlines and their source NULL/time-zone semantics
- calendar source time and recurrence
- calendar customer and participant/resource relationships
- binding and action import
- external execution

Articles, versions, prices, inventory and accounting are outside the current
mapping scope. Unproven NULL semantics never trigger automatic unassignment,
completion, deletion or clearing. Full mapping acceptance requires evidence for
every agreed mandatory area and the actual target execution and recovery gates.
