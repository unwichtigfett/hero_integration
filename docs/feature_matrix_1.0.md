# Feature matrix — implementation and acceptance

**Reviewed:** 2 November 2026 · **Source:** `hero_int_dev`  
**This is not a target-site approval inventory.** It separates code-supported behavior from the evidence still needed to enable it.

| Area | Source status | Current boundary |
| --- | --- | --- |
| ERPNext v16 installation | Implemented and offline checked | ERPNext is installed first; repeat install/migrate acceptance on the actual target site. |
| App DocTypes and schema | Implemented | Nine installer-owned fields and additional package-provisioned schema have different owners. Verify installed metadata. |
| HERO schema snapshots | Implemented | Source instance, HTTPS endpoint and fingerprint must match the confirmed binding. |
| Saved GraphQL Query | Implemented | Explicit read-only workspace execution is available for validated, revision-matched Query services. |
| GraphQL Mutation | Draft, guarded action and writeback code present | Metadata does not authorize external mutations. Active contract, preview, role, switch, audit and no-blind-retry gates apply. |
| Customer | Native Customer/primary Contact/Address implemented | Shared records require proven ownership. Source supports V1–V4 read contracts, but fresh sites start without active mappings. |
| Project | Native Project/Customer link/title rules implemented | Confirm Company separately; `-H` is the HERO document-name suffix; server-generated titles omit the salutation. |
| Native Task | Scoped native Task intake implemented | Stable identity, required subject, optional selected fields and conflict baselines; target-site execution remains unverified. |
| External Task | Historical four-domain observation projection retained | `HERO External Task` is not a native ERPNext Task or ToDo. |
| Task lifecycle and assignment | Source paths, policy/state hooks and ToDo code exist | Validate external NULL/timezone semantics, permission and Company binding, framework patch, negative cases and transfers. |
| Calendar | Native Event projection and conflict protection implemented | Source timezone, all-day boundary, recurrence and participant/resource/Customer identity remain approval-bound. |
| Mapping Editor | Drafts, personal revisions and publish gates implemented | Schema pickers, proposals or generated operations do not silently enable a read or write. |
| Configurable native Query adapters | Opt-in v2 code and offline tests | Phase, trade, Tasks, Calendar and primary Contact/Address fields use selected rules; exact saved Query and service reapproval required. Legacy mappings retain their existing behavior. |
| Project logbook | Configurable inline Project collection and legacy deduplication | Explicit child fields, timestamp conversion and collection bound; reaching the bound blocks incomplete history. Target-site acceptance pending. |
| Native Send to HERO | Grouped Mutation profiles and verified readback | Separate from fixed field writeback. Explicit source, relationship resolvers and approved read contract required; no automatic legacy fallback. |
| Mapping exchange | Legacy complete package and separate directional/personal boundaries implemented | Full target preview, CAS/provenance and guarded schema provisioning required. |
| Generated Task reads | Read-only service drafts and seals implemented | Separate mapping/service approval and per-page rule checks required; legacy reviewed documents remain valid. |
| Manual runs | Customer/Project phases and Task/Event projection runner implemented | Active approved service/mapping, source binding and site permissions required. |
| Polling | Scheduler and monitor guards implemented | Automatic source enumeration currently covers Customer and Project, not every visible sync area. |
| Checkpoints/recovery | Source contract and recorded focused tests | Target schema, queued-worker and live HERO recovery acceptance pending. |
| Helpdesk/CRM links | Reference and mirror-prevention hooks implemented | App-specific permissions and installed-version behavior require runtime checks. |
| Articles, versions, prices, inventory, accounting | **Out of current mapping scope** | Internal adapter code does not make these approved migrations. |
| Live external execution | **EXECUTION_UNVERIFIED** | CI and source tests cannot substitute for target-site and HERO acceptance. |

## How to read these states

**Implemented** means a path exists in source, not that it is activated. **Tested** refers to a named test, revision and environment. **Approved** means an actor authorized specific mapping/service/action revisions. **Verified** means the intended behavior was observed on the actual target runtime.

The [mapping matrix](mapping_matrix_1.0.md) preserves a historical four-domain baseline; its counts are not a live registry query. See [synchronization handoff](sync_handoff_1.0.md) for execution prerequisites.
