# Synchronization handoff: Customer, Project and Task

Status: partial. Mapping drafts, approved read contracts and execution authority
are separate. The delivery does not authorize synchronization or external
mutations. Verify the active mapping revision, saved read service revision,
source instance and schema fingerprint before execution.

Customer intake uses the HERO source identity and existing integration link.
Customer, primary Contact and Address are native records linked through the
existing adapters. Updates to shared Contact or Address records require proven
authority; the current paths preserve shared records and report a conflict.
Names and email addresses are not source identities.

Project intake uses the HERO Project identity, a resolved Customer link, a
separately confirmed local Company and the approved server-side title. Partner
notes remain separate from native Project notes. An unresolved Customer or
Company must not be guessed from another source identifier.

Native Task intake is explicitly scoped to source instance and Task type. Its
integration link identifies the source by system, instance, type and source ID.
Subject is required for creation; description is optional and may be written
only when its rule is selected. Field baselines detect concurrent local and
source changes. Replay preserves the same identity; conflicting changes require
review. Omitted values are preserved, and empty or NULL selected text requires
review rather than clearing a field. Source deletion does not delete or close
the Task. A mapping revision change requires review of the existing binding.

The Query and Mutation exchange files have separate directions and owner,
snapshot and revision boundaries. Mutation drafts are technical metadata;
they do not demonstrate or authorize an external mutation. The administrator's
complete current context must be exported and previewed before importing, with
no unintended removal or replacement. Schema provisioning is separately
guarded and must be included in a package when actually needed.

The personal Draft exchange validates selected field-rule identities against
the target and retains target-managed provenance. An authenticated preview
binds the package and observed references to the site and owner; it is not a
runtime grant. Registry instances, acknowledgements and execution state are
not imported. Existing rule identities and the referenced metadata are locked
for the bounded exchange. Supported GraphQL Mapping writers use a shared
site/source/snapshot/direction lock and the preview binds the complete active
context. Raw database writes and arbitrary administrator DDL remain outside
that boundary. Primary Customer and Project
field-action parameters are checked against the existing reviewed writeback
contract. The target-side integration link remains the authority for identity.

Calendar uses the existing native Event projection and the source-instance /
CalendarEvent identity. A non-null `project_match_id` resolves exclusively via
the existing Project integration link and Project read permission; a missing or
NULL value does not remove an existing relationship. Nullable omitted fields do
not clear values or their acknowledged baselines. Local edits remain visible to
subsequent conflict checks even after an independent source-field update. An
unknown recurrence flag is blocked; explicit recurrence is also held pending a
reviewed rule. For an existing bound Event the whole time group is held and a
review conflict recorded while independent field changes are considered; a new
Event still requires the necessary validated inputs. A validated schema alone does not confirm the source timezone,
all-day boundary or participant identity. The private editor Draft carries
unconfirmed time options and does not authorize execution. Raw partner/resource
observations are not native participant relationships; no Customer association
is inferred from a Project or Partner identifier.

Still missing or unverified: Task assignment and ToDo transfer, external
deadlines and their source NULL/time-zone semantics, calendar source time,
recurrence, customer and participant/resource relationships,
binding and action import, and external execution. Articles, versions, prices,
inventory and accounting are outside the current mapping scope. No automatic unassignment,
completion, deletion or clearing follows from unproven NULL semantics. Full
mapping acceptance requires evidence for every agreed mandatory area and the
actual target execution and recovery gates.
