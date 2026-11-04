// One explicit confirmation for Project data and pending logbook activities.
(() => {
    "use strict";
    const forms = new WeakMap();
    const t = value => __(value);
    const allowed = () => frappe.user.has_role("System Manager") || frappe.user.has_role("HERO Writeback");
    const node = (tag, text, className) => {
        const element = document.createElement(tag);
        if (text != null) element.textContent = String(text);
        if (className) element.className = className;
        return element;
    };
    const errorText = error => {
        try {
            const messages = JSON.parse(error?._server_messages || error?.responseJSON?._server_messages || "[]");
            const first = typeof messages[0] === "string" ? JSON.parse(messages[0]) : messages[0];
            if (first?.message) return new DOMParser().parseFromString(first.message, "text/html").body.textContent;
        } catch (_) { /* Use the request error below. */ }
        return error?.message || t("The request failed. Reopen the preview to try again.");
    };
    const rpc = async (name, args) => {
        const result = await frappe.call({method: "hero_integration.api." + name, args, type: "POST"});
        if (result?.exc) throw result;
        return result?.message;
    };
    function open(frm, owner) {
        if (owner.flow) return;
        if (frm.is_dirty()) { frappe.msgprint(t("Save the project before sending it to HERO.")); return; }
        const project = frm.doc.name, modified = frm.doc.modified;
        const flow = {closed: false, attempted: false, preview: null, request: 0};
        owner.flow = flow;
        const fresh = () => {
            const route = frappe.get_route?.();
            return !flow.closed && forms.get(frm) === owner && allowed() && !frm.is_dirty()
                && frm.doc.name === project && frm.doc.modified === modified
                && (!route || route[0] === "Form" && route[1] === "Project" && route[2] === project);
        };
        const dialog = new frappe.ui.Dialog({
            title: t("Send to HERO"),
            fields: [{fieldtype: "Select", fieldname: "phase", label: t("Phase"), options: [], hidden: 1,
                onchange: () => { if (flow.preview && !flow.attempted) load(); }},
                {fieldtype: "HTML", fieldname: "summary"}],
            primary_action_label: t("Confirm and send once"), primary_action: send,
            secondary_action_label: t("Cancel"), secondary_action: () => dialog.hide(),
            on_hide: () => { flow.closed = true; flow.request++; if (owner.flow === flow) owner.flow = null; },
        });
        flow.cancel = () => dialog.hide();
        const disable = () => dialog.get_primary_btn().prop("disabled", true);
        const wrapper = () => dialog.fields_dict.summary.$wrapper.empty()[0];
        const message = (value, danger = false) => {
            const status = node("p", value, danger ? "text-danger" : "text-muted");
            status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
            wrapper().append(status);
        };
        function render(preview) {
            const content = wrapper();
            content.append(node("p", t("Project naming stays in ERPNext and is not sent to HERO."), "text-muted"));
            const table = node("table", null, "table table-bordered");
            const heading = node("tr");
            for (const title of ["Field", "In HERO", "Will be sent"]) heading.append(node("th", t(title)));
            table.append(heading);
            for (const change of preview.data_changes) {
                const row = node("tr");
                for (const value of [t(change.label), change.hero ?? "—", change.new ?? "—"]) row.append(node("td", value));
                table.append(row);
            }
            if (preview.data_changes.length) content.append(table);
            if (preview.activities.length) content.append(node("h5", t("Project logbook")));
            for (const activity of preview.activities) {
                for (const change of activity.changes) {
                    const entry = node("div", change.new, "mb-3");
                    entry.style.cssText = "white-space:pre-wrap;overflow-wrap:anywhere";
                    content.append(entry);
                }
            }
            if (!preview.changes.length) content.append(node("p", t("No changes to send.")));
            for (const conflict of preview.conflicts) content.append(node("p",
                t("A field was changed in HERO since the last import; it is not overwritten.") + " " + t(conflict.label), "text-danger"));
            if (!preview.sending_enabled) content.append(node("p", t("Sending to HERO is switched off globally."), "text-warning"));
            dialog.get_primary_btn().prop("disabled", !preview.can_send);
        }
        async function load() {
            if (flow.closed || flow.attempted) return;
            flow.preview = null; disable(); dialog.fields_dict.phase.$input.prop("disabled", true);
            const request = ++flow.request;
            if (!fresh()) { message(t("The project changed. Save it and reopen the preview."), true); return; }
            message(t("Loading Project changes and logbook activities …"));
            try {
                const preview = await rpc("project_send_preview", {project, phase_id: dialog.fields_dict.phase.get_value() || null});
                if (flow.closed || request !== flow.request) return;
                if (!fresh()) { message(t("The project changed. Save it and reopen the preview."), true); return; }
                if (preview?.project !== project || !Array.isArray(preview.data_changes) || !Array.isArray(preview.activities)
                    || !Array.isArray(preview.changes) || !Array.isArray(preview.conflicts) || !preview.delivery || !preview.fingerprint)
                    throw new Error(t("Invalid Project send preview."));
                const phase = dialog.fields_dict.phase;
                phase.df.options = [{value: "", label: "—"}].concat((preview.phases || []).map(item => ({value: String(item.id), label: item.name})));
                phase.df.hidden = !(preview.phases || []).length;
                phase.refresh();
                flow.preview = preview; dialog.fields_dict.phase.$input.prop("disabled", false); render(preview);
            } catch (error) {
                if (!flow.closed && request === flow.request) message(errorText(error), true);
            }
        }
        async function send() {
            const preview = flow.preview;
            if (flow.closed || flow.attempted || !preview?.can_send) return;
            if (!fresh()) { disable(); message(t("The project changed. Save it and reopen the preview."), true); return; }
            flow.attempted = true; disable(); dialog.fields_dict.phase.$input.prop("disabled", true);
            message(t("Sending Project changes and logbook activities …"));
            try {
                const result = await rpc("project_send", {project, phase_id: dialog.fields_dict.phase.get_value() || null,
                    fingerprint: preview.fingerprint, delivery: JSON.stringify(preview.delivery), confirmed: 1});
                if (flow.closed) return;
                if (result?.status !== "SENT") {
                    message(t("Sending stopped. Confirmed changes remain sent; review the result before sending again.")
                        + ` (${result?.changes?.length || 0} / ${result?.activities?.length || 0})`, true);
                    return;
                }
                dialog.hide(); frappe.show_alert({message: t("Project changes and activities sent to HERO."), indicator: "green"});
                if (forms.get(frm) === owner && frm.doc.name === project && !frm.is_dirty()) await frm.reload_doc();
            } catch (error) {
                if (!flow.closed) message(t("Sending could not be confirmed. Review the result before sending again.") + " " + errorText(error), true);
            }
        }
        dialog.show(); disable(); load();
    }
    frappe.ui.form.on("Project", {refresh(frm) {
        forms.get(frm)?.flow?.cancel();
        const owner = {}; forms.set(frm, owner);
        frm.remove_custom_button?.(t("Send to HERO"));
        frm.remove_custom_button?.(t("Send activity to HERO"));
        if (frm.is_new() || !frm.doc.name || !allowed()) return;
        frm.add_custom_button(t("Send to HERO"), () => open(frm, owner));
    }});
})();
