# Portability and upgrades

- Install ERPNext v16 before HERO Integration; the app checks prerequisites without installing ERPNext automatically.
- Endpoint, host allowlist, token and site encryption key remain site configuration; do not export them in mapping packages.
- Registered upgrade migrations: [`patches.txt`](../hero_integration/patches.txt); preserve their upgrade contracts.
- Schema provisioning uses Frappe DDL, which may commit independently of mapping transactions.
- Task assignment concurrency requires the separately distributed [Frappe patch](../framework_patches/frappe_assignment_intent_v2/frappe_patch_1.0.md); unsupported framework revisions fail closed.
- Validate the exact app revision, framework versions, site metadata, mapping/service approvals, permissions and recovery before deployment.
