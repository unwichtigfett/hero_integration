# Native query field contract

- Server-owned bindings: `direct_mapping.SOURCE_CONTRACTS`; each tuple specifies operation, source path/type, target DocType/field, fieldtype and transformation.
- Generic native Field bindings require explicit business approval; `Customer.alias` is a technical test fixture only.
- Validators check purpose, transformation, write permission, protected fields and actual writable metadata.
- Native links require their approved resolver; raw foreign IDs do not prove a target binding.
- Active plans recheck the server contract, snapshot, mapping revision and read-service approval at execution.
- Drafts, imported packages and generated operations cannot add server-owned bindings or authorize execution.
- New business bindings require semantic review and targeted tests.
