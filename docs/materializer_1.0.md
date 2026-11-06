# GraphQL materializer

- Pure-Python package: `hero_integration/graphql_protocol`; provenance: `SOURCE_PROVENANCE.json`.
- Functions: deterministic operation rendering, typed values, selections, response decoding and request fingerprints.
- Dependencies: no Frappe site, database, HTTP transport or `hero_connector` runtime.
- Generated reads remain drafts until their mapping and service are approved and revalidated.
- Generated mutations do not grant write permissions or bypass source, audit and confirmation controls.
