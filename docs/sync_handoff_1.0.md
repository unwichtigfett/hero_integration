# Synchronization Handoff — HERO Integration

## 1. Confirmed — Source Behavior
### Customer
- HERO identity via existing Integration Link.
- Native Customer, Contact and Address records.
- Shared records protected against unauthorized updates.
- Conflicts reported; no identity inference from names or emails.

### Project
- Requires HERO identity, resolved Customer, confirmed Company and server-side title.
- Partner notes separated from native Project notes.
- No inferred Customer or Company assignment.

### Task
- Stable identity: `system/instance/type/source_id`.
- Subject required; description optional.
- Field baselines detect concurrent changes.
- Conflicts and NULL/empty values require review.
- Omitted fields remain unchanged.
- No automatic deletion, closure or binding replacement.
- Replay preserves identity.

### Task Read Service
- Saved Task draft generates read-only service draft.
- Publication binds rules, source instance and schema snapshot.
- Separate System Manager approval required for execution.
- Approval and locked rules revalidated before each generated page.
- Existing HERO client/decoder reused.

### Mapping Exchange
- Query and Mutation strictly separated.
- Independent revisions, owners and snapshots.
- Target identities and provenance protected.
- Authenticated preview binds complete active context.
- Shared `site/source/snapshot/direction` writer lock.
- Mutation metadata grants no execution authority.

### Calendar
- Native Event projection with stable CalendarEvent identity.
- Project resolution exclusively via existing Integration Link.
- Missing/NULL relationships preserved.
- Local edits remain conflict-detectable.
- Unknown recurrence blocked; explicit recurrence held.
- Existing Event time-group changes require review.
- Independent field updates remain possible.

## 2. Pending — Functional Coverage

- Task assignment and ToDo synchronization
- External deadlines, NULL and timezone semantics
- Calendar timezone, all-day and recurrence handling
- Binding and action import
- External synchronization execution

## 4. Safety Constraints

- Drafts and previews grant **no execution authority**.
- Unverified NULL values never automatically clear, delete, complete or unassign.
- Existing Integration Links remain identity authority.
- Raw DB writes and arbitrary DDL are outside exchange protection.
- Source implementation and offline tests do not prove runtime acceptance.

## 5. Out of Scope

Articles, versions, prices, inventory and accounting.
