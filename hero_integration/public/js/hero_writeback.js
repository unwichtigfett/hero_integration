(() => {
    "use strict";

    const method = "hero_integration.api.";

    const detailRows = (data) => {
        const table = $("<dl class='hero-mapping-detail-list'></dl>");
        data.forEach(([label, value]) => table.append($("<dt></dt>").text(label), $("<dd></dd>").text(value == null ? "—" : String(value))));
        return table;
    };

    const previewAndConfirm = (frm, action) => {
        frappe.call({method: method + "writeback_preview", args: {mapping_name: action.mapping_name, doctype: frm.doctype, docname: frm.doc.name}}).then((response) => {
            const preview = response.message;
            const dialog = new frappe.ui.Dialog({title: __("Send to HERO"), fields: [{fieldtype: "HTML", fieldname: "preview"}], primary_action_label: __("Confirm and send once")});
            dialog.fields_dict.preview.$wrapper.empty().append(
                $("<p class='text-muted'></p>").text(__(preview.effect_summary)),
                detailRows([
                    [__("HERO target"), `${preview.target.source_instance}:${preview.target.external_id}`],
                    [__("Frappe field"), `${preview.source.doctype}.${preview.source.field}`],
                    [__("Current value"), preview.source.value],
                    [__("HERO field"), `${preview.hero.operation}.${preview.hero.field}`],
                    [__("Expected value"), preview.hero.value],
                ]),
                !preview.can_execute ? $("<p class='text-warning'></p>").text(__(preview.blocked_reason || "Global HERO mutation execution is currently OFF.")) : "",
            );
            dialog.set_primary_action(__("Confirm and send once"), () => {
                frappe.call({method: method + "writeback_execute", args: {
                    mapping_name: action.mapping_name, doctype: frm.doctype, docname: frm.doc.name,
                    confirmation_fingerprint: preview.request_fingerprint, confirmed: 1,
                }}).then((result) => {
                    dialog.hide();
                    frappe.show_alert({message: __("HERO writeback completed"), indicator: "green"});
                    frm.reload_doc();
                    return result;
                });
            });
            dialog.get_primary_btn().prop("disabled", !preview.can_execute);
            dialog.show();
        });
    };

    const mount = (frm) => {
        frm.$wrapper.find(".hero-writeback-action").remove();
        if (frm.is_new() || !frm.doc.name) return;
        frappe.call({method: method + "writeback_available", args: {doctype: frm.doctype, docname: frm.doc.name}}).then((response) => {
            (response.message?.actions || []).forEach((action) => {
                const field = frm.get_field(action.field);
                if (!field?.$wrapper) return;
                const button = $("<button type='button' class='btn btn-xs btn-default hero-writeback-action'></button>").text(__("Send to HERO"));
                button.on("click", () => previewAndConfirm(frm, action));
                const label = field.$wrapper.find(".control-label").first();
                (label.length ? label : field.$wrapper).append(button);
            });
        });
    };

    ["Customer", "Project"].forEach((doctype) => frappe.ui.form.on(doctype, {refresh: mount}));
})();
