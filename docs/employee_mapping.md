# Employee migration and field mapping

**Synchronization → Employees → Start/Update** queues a manual Employee Migration.
The HERO purge preserves Employee/User records and their source links.
Employees belong to the configured ERPNext company; HERO is their data source, not their employer.
New Full Migration runs start with employees; existing full runs retain their
original step list. Deployment requires the app's normal install/migrate hooks
before using the new run action and fields. This document is a source contract,
not evidence of installation or a live import.

All 18 supplied fields are declared in
`hero_integration/config/employee_mapping.json`. The pure
`employee_mapping.preview` exposes their proposals and blockers. The writer is
`services.employee_migration`; the mapping is currently fixed, rather than a
publishable Query-v2 employee adapter. It reads `partners`, or
`company.partners` when that is the supported root in the source's newest
validated snapshot. The exact read-only Query is validated against that snapshot.
Neither root is assumed to support first/offset; the unpaginated list is bounded
to 10,000 records and missing/null/oversized/duplicate-ID responses block the run.

## Company and joining date

The company is the ERPNext Global Defaults company, or the sole Company when
no default is set. Several companies without a default block for configuration.
Branches/sites are not converted into new companies. Imported employees use
**2026-01-01** as their joining date. The importer creates a source-scoped
employee number using the existing `employee_number` field. Existing Employee
values remain subject to adoption/conflict checks.

Gender and birth date are optional under the owner's policy. Installation and
migration express that policy through standard `reqd=0` Property Setters on
`Employee.gender` and `Employee.date_of_birth`. No mandatory validation is
bypassed and no dates, genders or placeholder records are invented.

## Field contract

| HERO source | ERPNext target | Handling |
| --- | --- | --- |
| `id` | `HERO Integration Link.source_id` | `partner_identity` |
| `user_id` | `HERO Integration Link.source_id` | `user_identity` |
| `first_name` | `Employee.first_name` | `text` |
| `last_name` | `Employee.last_name` | `text` |
| `full_name` | `Employee.employee_name` | `computed_name` |
| `email` | `Employee.company_email` | `text` |
| `phone` | `User.phone` | `text` |
| `mobile` | `Employee.cell_number` | `text` |
| `role` | `Employee.custom_hero_partner_role` | `observation` |
| `status` | `Employee.status` | `employee_status` |
| `account_type` | `Employee.custom_hero_account_type` | `observation` |
| `user.id` | `HERO Integration Link.source_id` | `user_identity_check` |
| `user.email` | `User.email` | `user_login_identity` |
| `user.role` | `User.custom_hero_role` | `observation` |
| `user.locale` | `User.language` | `locale` |
| `user.last_login` | `User.custom_hero_last_login` | `observation` |
| `user.created` | `User.custom_hero_created` | `observation` |
| `user.modified` | `User.custom_hero_modified` | `observation` |


`id` binds the Employee source link under type `partner`. `user_id` and
`user.id` must agree and share one User source link under type `User`.
These integers are never written into `Employee.user_id`, which links the
existing local User. No duplicate name, email, mobile or language fields are
created. Visible field labels have no HERO prefix; internal `custom_hero_*` keys remain
stable. Six app-owned observations retain source-specific roles, account type
and remote audit times; the installation checks existing definitions for
conflicts before creating missing fields. Remote audit values preserve their
ISO offset strings instead of overwriting local system timestamps.

Contact email and login email remain distinct. On first adoption the exact
`user.email` identifies an existing User or the login for a new active employee User,
with no conflicting HERO binding.
Subsequent runs follow that source link and block if the login email differs;
accounts are never renamed or merged by email changes. Employee adoption uses
its existing `user_id` relation, not a name match, and requires compatible
existing values. Different middle/full names require review.

## Local account and conflict protection

Missing Users are created **active** with only the **Employee** role and no
welcome email. HERO partner roles never become permissions. Existing User
roles are retained. Records already imported stay committed.
The Employee status must agree with the local User's enabled state before
saving, so ERPNext's native lifecycle cannot enable/disable a login as an
import side effect. HERO role/account type observations are never converted
into Frappe roles or account types. Normal Employee controller behavior still
applies, including its standard Employee role; no HERO assignment policy or
Object Binding authorization is created automatically.

Missing source paths are omitted and explicit nulls remain explicit proposals.
Required name/status, language availability, identities, source binding and
site validation are enforced. Changed local mapped values or ambiguous links
block a record before writes. Employee, User, their source links and checkpoint
commit together; failures roll back that entire record. Resume retries failed
identities without reprocessing successful ones or double-counting them.
Company/schema/mapping or inventory changes require a new run. Cancellation
is checked per record and time slices are bounded.

The tests use synthetic records and fake persistence, plus the offline suite.
They do not establish a deployed schema, a real Employee/User controller
transaction or a live HERO import.
