# ADR-36: Internal pure GraphQL materializer

Status: APPROVED

Decision: hero_integration ships a versioned internal pure-Python materializer package extracted from hero_connector revision 6908a5acf7fe00726cc379c9b1d72824bc7a601d. It has no runtime dependency on hero_connector, auto_domain, Frappe, site configuration, database, or HTTP transport.

Scope: deterministic operation rendering, typed variables, stable selections, result decoding, and request fingerprints only. Snapshot adaptation and mapping-controller integration remain later phases.

Provenance: MIT-licensed hero_connector source; source revision and imported file hashes are recorded in hero_integration/materializer/SOURCE_PROVENANCE.json.

Safety: existing ReadContract, writeback gates, preview, confirmation, audit, no-retry mutation transport, mappings, services, and contracts remain unchanged.
