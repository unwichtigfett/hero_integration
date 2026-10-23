frappe.ui.form.on("HERO GraphQL Service", {
    refresh(frm) {
        if (frm.is_new() || frm.is_dirty() || frm.doc.status !== "Validated" || !frm.doc.read_mapping) {
            return;
        }
        frm.add_custom_button(__("Approve Read Contract"), () => {
            frappe.db.get_doc("HERO GraphQL Mapping", frm.doc.read_mapping).then((mapping) => {
                frappe.confirm(
                    __("Approve this saved service and mapping revision? This does not start a data transfer."),
                    () => {
                        frappe.call({
                            method: "hero_integration.api.approve_read_service",
                            args: {
                                query_id: frm.doc.name,
                                mapping_name: mapping.name,
                                expected_service_revision: frm.doc.service_revision,
                                expected_mapping_revision: mapping.mapping_revision,
                            },
                        }).then(() => frm.reload_doc());
                    },
                );
            });
        });
    },
});
