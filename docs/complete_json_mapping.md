# JSON mapping packages

- Query exchange: `hero-integration-mappings-v1`; Mutation exchange: `hero-personal-mapping-portable-v2`.
- Complete configuration: `hero-integration-config-v2`; includes schema snapshot, approved operational settings, mappings and readers, with optional mutation definitions and contracts.
- Imports require a validated compatible schema, preview and confirmation; schema provisioning and mapping activation are separate from HERO execution.
- Credentials, hosts, notification recipients, execution/deletion switches, runtime history and business records are excluded.
- Nine migration areas share the mapping system; project histories use the project reader.
- Native ERPNext records and source-bound Integration Links preserve identity and local edits; ambiguous relationships require reconciliation.
- Mapping and service revisions, source instance and read approval are revalidated at runtime.
- Schema compatibility checks cover selected fields, arguments, used inputs, identities and readback; unrelated additions alone need not invalidate mappings.
- Test mode limits each area to 1–100 source records; processed and received counts are separate.
- Products: `HeroProducts`, `supply_product_versions`, `Source Identity`, `Record Name`, `target_operation` and `read_service`.
- Price mappings: `relation`, `Standard Buying`, `Standard Selling`, `sales_prices.supply_sales_price.is_default` and `list_price`; stock observations do not post inventory.
- Files: `HeroDocuments`, `HeroPictures`, `FileUpload_FileUploads.edges.node`, `first`, `offset` and the `images` category.
- Existing built-in mappings remain available; generated readers require explicit approval and approved mappings.
- Project logbook: selected histories are paginated, source-bound and deduplicated; restricted entries stay outside the shared timeline.
- Outbound updates and logbook creation require separate contracts, permission checks, fresh previews, confirmation and verified readback; uncertain creates are not blindly retried.
