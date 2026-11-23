frappe.ui.form.on("HERO Sync Change Event", {
    refresh(frm) {
        if (frm.doc.outcome !== "FIELD_CONFLICT" || !frappe.user.has_role("System Manager")) return;
        const reservation = frm.doc.resolution_json && JSON.parse(frm.doc.resolution_json);
        const pending = reservation?.choice && reservation;
        if (["PREPARED", "DISPATCHING"].includes(pending?.phase)) {
            frm.add_custom_button(__("Review dispatch slot"), async () => {
                const args = {event: frm.doc.name, expected_modified: frm.doc.modified, choice: "RELEASE_RESERVATION"};
                const {message: preview} = await frappe.call({method: "hero_integration.api.preview_sync_field",
                    args, freeze: true});
                const text = preview.phase === "PREPARED"
                    ? __("Cancel the unsent reservation so the field can be reviewed again?")
                    : __("Release this dispatch slot and keep the field open for verified readback?");
                const escape = frappe.utils.escape_html;
                frappe.confirm(escape(preview.audit) + " / " + escape(preview.operation) + "<br>" + escape(text),
                    async () => {
                        await frappe.call({method: "hero_integration.api.resolve_sync_field", freeze: true,
                            args: {...args, confirmed: 1, confirmation_fingerprint: preview.confirmation_fingerprint}});
                        await frm.reload_doc();
                    });
            });
        }
        frm.add_custom_button(__("Resolve field conflict"), () => {
            const dialog = new frappe.ui.Dialog({
                title: __("Resolve field conflict"),
                fields: [
                    {fieldname: "choice", fieldtype: "Select", label: __("Decision"), reqd: 1,
                        options: ["HERO", "ERPNEXT", "CUSTOM"], default: pending?.choice || "HERO",
                        read_only: Boolean(pending)},
                    {fieldname: "value_json", fieldtype: "Code", options: "JSON", label: __("Selected value (JSON)"),
                        depends_on: "eval:doc.choice === 'CUSTOM'", mandatory_depends_on: "eval:doc.choice === 'CUSTOM'",
                        default: pending ? JSON.stringify(pending.value) : "", read_only: Boolean(pending)},
                    {fieldname: "mutation_mapping", fieldtype: "Link", options: "HERO GraphQL Mapping",
                        label: __("Mutation Mapping"), depends_on: "eval:doc.choice !== 'HERO'",
                        default: pending?.mapping, read_only: Boolean(pending),
                        get_query: () => ({filters: {source_instance: frm.doc.source_instance,
                            status: "Active", direction: "FRAPPE_TO_MUTATION"}})}
                ],
                primary_action_label: __("Preview decision"),
                async primary_action(values) {
                    const args = {...values, event: frm.doc.name, expected_modified: frm.doc.modified};
                    const {message: preview} = await frappe.call({
                        method: "hero_integration.api.preview_sync_field", args, freeze: true
                    });
                    const lines = [
                        [__("Document"), preview.target_doctype + " / " + preview.target_name],
                        [__("Field"), preview.field],
                        [__("Current HERO value"), JSON.stringify(preview.hero_value)],
                        [__("Current ERPNext value"), JSON.stringify(preview.erpnext_value)],
                        [__("Selected value (JSON)"), JSON.stringify(preview.chosen_value)],
                        [__("HERO mutation"), preview.operation || __("None")],
                        [__("ERPNext field update"), preview.updates_local ? __("Yes") : __("No")]
                    ];
                    if (preview.remote_change) {
                        const change = preview.remote_change;
                        lines.push([__("HERO object"), change.object_type + " / " + change.id],
                            [__("HERO field"), change.field], [__("Value sent to HERO"), JSON.stringify(change.value)]);
                    }
                    const escape = frappe.utils.escape_html;
                    const message = lines.map(([label, value]) => escape(label) + ": " + escape(value)).join("<br>");
                    let prompt = __("Confirm this field decision?");
                    if (preview.cancel_unsent) {
                        prompt = __("Cancel the unsent reservation so the field can be reviewed again?");
                    } else if (preview.requires_new_review) {
                        prompt = __("Confirm the HERO readback and open a new review for the later ERPNext edit?");
                    } else if (preview.recovery) {
                        prompt = __("Verify the existing dispatch and resume this decision?");
                    }
                    frappe.confirm(message + "<br><br>" + escape(prompt), async () => {
                        let result;
                        try {
                            result = await frappe.call({method: "hero_integration.api.resolve_sync_field", freeze: true,
                                args: {...args, confirmed: 1, confirmation_fingerprint: preview.confirmation_fingerprint}});
                        } finally {
                            dialog.hide();
                            await frm.reload_doc();
                        }
                        if (result?.message?.review) {
                            await frappe.set_route("Form", "HERO Sync Change Event", result.message.review);
                        }
                    });
                }
            });
            dialog.show();
        });
    }
});
