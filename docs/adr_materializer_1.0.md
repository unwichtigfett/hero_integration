# ADR-36: Internal pure GraphQL materializer

Status: APPROVED

Decision: hero_integration ships a versioned, internal, pure-Python materializer package. The package is extracted from hero_connector revision 6908a5acf7fe00726cc379c9b1d72824bc7a601d. It has no runtime dependency on hero_connector, auto_domain, Frappe, site configuration, database, or HTTP transport.

Scope: The package covers deterministic operation rendering, typed variables, stable selections, result decoding, and request fingerprints. Nothing else. Snapshot adaptation and mapping-controller integration follow in later phases.

Provenance: The source is MIT-licensed hero_connector code. hero_integration/graphql_protocol/SOURCE_PROVENANCE.json records the source revision and the hashes of the imported files.

Safety: The package changes none of these: ReadContract, writeback gates, preview, confirmation, audit, no-retry mutation transport, mappings, services, and contracts.
