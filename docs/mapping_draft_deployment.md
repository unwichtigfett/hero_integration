# Consolidated mapping deployment

`pipe deploy` captures the committed app, builds the Docker image and runs
`bench migrate`. The final app migration hook loads the site-scoped package in
`hero_integration/deployment/consolidated_mappings_20261103.json`.

The package combines eight reviewed old/new draft inputs. It keeps all useful
selected field mappings and transformation options; identical rules are combined.
The sole explicitly replaced redundant field is the custom Item category, whose
source is represented by the native Item Group. Company/person naming fallback
and separate buying/selling price lists remain distinct rules.

Customer, Project and Item drafts are saved against the actual active target
mapping revisions, with freshly captured field-rule provenance. Later changes
to an input draft, extra active fields missing from the package, ambiguous
parents, or incompatible metadata stop the transaction rather than losing work.
Only after all replacement drafts are saved are superseded input generations
archived through the owning draft service. Their revision history remains.
No merged draft is automatically published.

Missing app-owned fields are created through Frappe using plain labels. Existing
fields are checked, never overwritten. Frappe DDL can persist if later draft
writes fail; draft writes share a savepoint and remain retryable. The target
schema is resolved by source instance and fingerprint, not copied document names.

The one-time reader repair validates the existing Customer, Project and Calendar
plans and approves their exact current revisions. Calendar's broken scalar
partner binding is removed and native color/status are used. A Patch Log receipt
prevents later deployments from undoing a subsequent manual approval revocation.
No HERO request or business-data import is started by deployment.

Services, Documents, Pictures and Employees receive ordinary editable mapping drafts and
validated read-query definitions when no own mapping exists. These are not
approved automatically. Direct migrations require an approved own mapping and
cannot silently execute a built-in fallback.

In Mapping, **My personal drafts** appears before the active mapping table and
includes both new-definition drafts and drafts of existing active definitions.
The deployed drafts belong to Administrator. Other System Managers cannot read
these private drafts. Draft rows display their saved field rules and draft
revision; active-rule caches cannot conceal a private edit. Company relations
explain that the confirmed ERPNext Company is used, not the HERO customer ID.

Migration logs report `HERO draft deployment` and `HERO mapping repair`, including
current baseline revisions and draft keys. Activation and execution approvals
for newly configured mappings remain separate. No database restore or manual
JSON import is needed.

The bundle includes mapping configuration only: no customer/employee records,
credentials, tokens, or schema response dump. It is a deployment format, not a
portable-v2 editor-exchange file.
