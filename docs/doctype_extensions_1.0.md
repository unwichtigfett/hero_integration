# DocType extensions

- `HERO Integration Link`: canonical source identity, target reference, mapping version, synchronization status and conflict baselines.
- Installer-owned `custom_hero_*` fields and package-provisioned schema have separate ownership; inspect the active site metadata.
- Customer, Contact and Address use native records and Dynamic Links; secondary records require source identity and ownership checks.
- Project uses native customer, company, notes and title fields; external status observations do not automatically change workflow state.
- Event uses native calendar fields; approved mappings govern project references and observational child records.
- Native Task intake and legacy `HERO External Task` observations are distinct; assignment and deadline behavior require their respective contracts.
- `HERO GraphQL Schema Snapshot`, `HERO GraphQL Service` and `HERO Frappe Mapping` store reviewed schema, service and mapping metadata.
- Schema adoption validates target fields and permissions before provisioning; conflicting or foreign definitions remain untouched.
- Frappe schema DDL can commit independently of later mapping changes; review partial results before retrying.
- Installer definitions: [`install.py`](../hero_integration/install.py); upgrade patches: [`patches.txt`](../hero_integration/patches.txt).
