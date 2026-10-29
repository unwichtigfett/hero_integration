# Frappe patch compatibility and rollback

Base commit: `4515014c87ece6cb20ee91d2cfb121c5cbd1ff4a` (Frappe version-16, actual installed base).
Base `frappe/desk/form/assign_to.py` SHA-256: `e317417e9f7ad0a6c2a60c5238f5c393fe9302b0209c2c9fab11014d624143f1`.
Patched file SHA-256: `d226b9de31c9f5edf1f207382534926547021ce14bfdaac1d62a334e59921b64`.
Patch SHA-256: `1273a4d1ab9a803de4a1394c6366bc490f4dabd64f94bae392573806cf976969`.

Compatibility requires the exact base commit and base-file digest, an unchanged working tree for that file, and a successful `patch -p1 --dry-run < frappe-assignment-intent.patch` from the private Frappe root. Apply with `patch -p1 < frappe-assignment-intent.patch`, verify the patched-file digest above, and run the relevant isolated acceptance before use. Roll back by checking the patched-file digest and running `patch -p1 -R --dry-run < frappe-assignment-intent.patch` followed by `patch -p1 -R < frappe-assignment-intent.patch`; verify the base-file digest. These are documented procedures, not deployment approval. No foreign framework checkout was modified. Application checks the declared hook version and all registered hooks at runtime and holds automatic transfer when absent.

The patch introduces app-neutral hooks `assignment_intent_guard`, `assignment_intent_before`, and `assignment_intent_after` and `ASSIGNMENT_INTENT_HOOK_VERSION=2`. A guard may request transactional protection for its own objects. The before hook runs only after the native permission check, before native duplicate lookup; after hooks run only on successful native result creation. Savepoint rollback is limited to guarded calls and discards callbacks registered after entry; it cannot undo nontransactional side effects from other third-party hooks. Compatibility with a different Frappe revision or additional third-party hooks is not established.
