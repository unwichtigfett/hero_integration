# Frappe assignment patch

- Hooks: `assignment_intent_guard`, `assignment_intent_before`, `assignment_intent_after`; version: `ASSIGNMENT_INTENT_HOOK_VERSION=2`.
- Original tested Frappe commit: `4515014c87ece6cb20ee91d2cfb121c5cbd1ff4a`.
- Additional tested commit: `2fcb9397a1f1da353b21e53d2f97ec56653acd02`; tracked Python identities: `frappe-2fcb-python.manifest`.
- Base `frappe/desk/form/assign_to.py` SHA-256: `e317417e9f7ad0a6c2a60c5238f5c393fe9302b0209c2c9fab11014d624143f1`.
- Patched file SHA-256: `d226b9de31c9f5edf1f207382534926547021ce14bfdaac1d62a334e59921b64`.
- Patch SHA-256: `1273a4d1ab9a803de4a1394c6366bc490f4dabd64f94bae392573806cf976969`.
- Container installation: `apply_checked.py` verifies the manifest, patch applicability and output hash; unknown versions are rejected.
- Manual apply: verify the base commit and hash; run `patch -p1 --dry-run < assignment-intent.patch`, then apply and verify the patched hash.
- Rollback: verify the patched hash; run `patch -p1 -R --dry-run < assignment-intent.patch`, then reverse and verify the base hash.
- Guarded calls use savepoints and discard newly registered callbacks on rollback; third-party nontransactional side effects cannot be undone.
- The app requires the hook version and registered hooks; missing boundaries hold automatic assignments.
