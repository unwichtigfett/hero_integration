# Deploy the consolidated mapping drafts

`pipe deploy` captures the committed app, builds the Docker image and runs
`bench migrate` in the target container. The app's final `after_migrate` hook
loads `hero_integration/deployment/consolidated_mappings_20261103.json`.

The bundle contains the consolidated Customer, Project and Item drafts from
3 November 2026: 57 source rules for 56 logical targets. The Customer name uses
an ordered company-name/person-name fallback. Buying and selling prices have
separate price-list targets. The redundant custom Item category is omitted.

The hook applies only to the declared target site. It resolves the validated
HERO schema by source instance and fingerprint, rather than copying local
snapshot names. A missing matching schema or incompatible target field stops
the import with a deployment error. No HERO request is made.

Missing declared app-owned Custom Fields are created through Frappe, using
plain labels. Existing fields are checked, never overwritten. As with other
Frappe schema migrations, field DDL can persist if a later draft import fails.
Retrying is safe. All new draft writes share a savepoint; the outer migration
owns the final commit.

Three new-definition drafts are created through the personal draft storage
service, owned by Administrator. Existing drafts with these identities are
preserved, including user edits and discarded drafts. Already-published
definitions are also left alone. Repeated migrations create no duplicates.
Other users' drafts and all active mappings remain unchanged.

Read-service references are resolved on the target only when unique within
the selected source, snapshot and query operation. Otherwise the draft remains
unbound for review. The bundled query proposals remain in the deployment JSON;
the importer never modifies or approves existing read services.

After deployment, the log includes `HERO draft deployment` and the three draft
keys. In Desk, open **HERO Mapping Draft** as Administrator to inspect them.
The operation deploys drafts only: activation, read approval and synchronization
are separate review steps. No database restore or manual JSON import is needed.

This deployment format is intentionally separate from the portable-v2 editor
exchange, which does not yet cover all native v5 contract parameters. It carries
no customer/employee records, credentials, tokens or schema response dump.
