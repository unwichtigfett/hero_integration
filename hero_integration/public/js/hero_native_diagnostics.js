/* Translate known diagnostics in read-only native fields without editing data. */
(() => {
    "use strict";
    if (window.hero_native_diagnostics_registered) return;
    window.hero_native_diagnostics_registered = true;
    const fields = {
        "HERO GraphQL Mapping": ["suggestion_reason"],
        "HERO GraphQL Schema Snapshot": ["analysis_detail"],
        "HERO Snapshot Mapping Inheritance": ["reason"],
        "HERO Mutation Audit": ["detail"],
        "HERO Integration Run": ["error_message"],
        "HERO Mapping Adoption": ["error_message"],
        "HERO Integration Settings": ["schema_check_message", "sync_error"],
        "HERO Migration Progress Ledger": ["detail"],
    };
    for (const [doctype, names] of Object.entries(fields)) {
        frappe.ui.form.on(doctype, {
            refresh(frm) {
                for (const name of names) window.hero_integration_diagnostics?.bindField?.(frm, name);
            },
        });
    }
})();
