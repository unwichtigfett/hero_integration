// Explicitly send one existing Project activity through its approved logbook mapping.
(() => {
    "use strict";

    const method = "hero_integration.api.";
    const forms = new WeakMap();
    const activityKind = value => value === undefined ? "Comment" : value;
    const activityKey = (kind, name) => JSON.stringify([kind, name]);
    const t = value => typeof __ === "function" ? __(value) : String(value);
    const allowed = () => frappe.user.has_role("System Manager") || frappe.user.has_role("HERO Writeback");
    const text = value => {
        if (value == null) return "";
        const raw = typeof value === "string" ? value : JSON.stringify(value);
        const parsed = new DOMParser().parseFromString(raw, "text/html");
        parsed.querySelectorAll("script,style,template").forEach(node => node.remove());
        parsed.querySelectorAll("br").forEach(node => node.replaceWith("\n"));
        parsed.querySelectorAll("p,div,li").forEach(node => node.append("\n"));
        return (parsed.body.textContent || "").replace(/\n{3,}/g, "\n\n").trim();
    };
    const node = (tag, value, className) => {
        const element = document.createElement(tag);
        if (value !== undefined) element.textContent = value;
        if (className) element.className = className;
        return element;
    };
    const errorText = error => {
        try {
            const messages = JSON.parse(error?._server_messages || error?.responseJSON?._server_messages || "[]");
            const first = typeof messages[0] === "string" ? JSON.parse(messages[0]) : messages[0];
            if (first?.message) return text(first.message);
        } catch (_) { /* Fall back to a plain error message. */ }
        return text(error?.message || t("The request failed."));
    };
    const rpc = async (name, args, write = false) => {
        const result = await frappe.call({method: method + name, args, type: write ? "POST" : "GET"});
        if (result?.exc) throw result;
        return result?.message;
    };

    function open(frm, owner) {
        if (owner.flow || forms.get(frm) !== owner) return;
        if (frm.is_dirty()) {
            frappe.msgprint(t("Save the project before sending it to HERO."));
            return;
        }
        const project = frm.doc.name, modified = frm.doc.modified;
        const flow = {closed: false, request: 0, preview: null, attempted: false, candidates: []};
        owner.flow = flow;
        let dialog;
        const fresh = () => {
            const route = frappe.get_route?.();
            return !flow.closed && forms.get(frm) === owner && allowed() && !frm.is_new() && !frm.is_dirty()
                && frm.doc.name === project && frm.doc.modified === modified
                && (!route || route[0] === "Form" && route[1] === "Project" && route[2] === project);
        };
        const disableSend = () => dialog.get_primary_btn().prop("disabled", true);
        const summary = () => dialog.fields_dict.summary.$wrapper.empty()[0];
        const message = (value, danger = false) => {
            const status = node("p", value, danger ? "text-danger" : "text-muted");
            status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
            summary().append(status);
        };
        const stale = () => {
            if (flow.closed) return;
            flow.preview = null; disableSend();
            dialog.fields_dict.activity.$input.prop("disabled", true);
            message(t("The project changed. Save it and reopen the activity preview."), true);
        };
        const selected = () => dialog.fields_dict.activity.get_value();
        const render = preview => {
            const wrapper = summary();
            const activity = flow.candidates.find(item => item.key === selected());
            wrapper.append(node("h5", t("Activity")));
            const content = node("div", text(activity.content));
            content.style.cssText = "white-space:pre-wrap;overflow-wrap:anywhere;max-height:20rem;overflow:auto";
            wrapper.append(content);
            wrapper.append(node("p", t("This adds one entry to the HERO project logbook."), "text-muted"));
            if (preview.sending_enabled !== true) wrapper.append(node("p", t("Sending to HERO is switched off globally."), "text-warning"));
            if (preview.blocked_reason) wrapper.append(node("p", text(preview.blocked_reason), "text-danger"));
            const table = node("table", undefined, "table table-bordered");
            const heading = node("tr");
            for (const label of ["Field", "In HERO", "Will be sent"]) heading.append(node("th", t(label)));
            table.append(heading);
            for (const change of preview.changes) {
                const row = node("tr");
                for (const value of [t(change.label || "Field"), text(change.hero) || "—", text(change.new) || "—"]) {
                    const cell = node("td", value); cell.style.whiteSpace = "pre-wrap"; row.append(cell);
                }
                table.append(row);
            }
            if (preview.changes.length) wrapper.append(table);
            else wrapper.append(node("p", t("No changes to send."), "text-muted"));
            dialog.get_primary_btn().prop("disabled", !(preview.can_send === true && preview.sending_enabled === true && preview.changes.length));
        };
        async function previewSelected() {
            if (flow.closed || flow.attempted) return;
            flow.preview = null; disableSend();
            const request = ++flow.request;
            if (!fresh()) { stale(); return; }
            const activity = flow.candidates.find(item => item.key === selected());
            if (!activity) {
                message(t("Choose an activity to preview.")); return;
            }
            const {name: comment, activity_doctype} = activity;
            message(t("Loading activity preview …"));
            try {
                const preview = await rpc("project_logbook_preview", {comment, activity_doctype});
                if (flow.closed || request !== flow.request) return;
                if (!fresh()) { stale(); return; }
                if (preview?.comment !== comment || activityKind(preview.activity_doctype) !== activity_doctype
                    || preview.project !== project || !Array.isArray(preview.changes)
                    || typeof preview.fingerprint !== "string" || !preview.fingerprint) throw new Error(t("Invalid activity preview."));
                flow.preview = {...preview, activity_doctype}; render(flow.preview);
            } catch (error) {
                if (!flow.closed && request === flow.request) message(errorText(error), true);
            }
        }
        async function send() {
            const preview = flow.preview;
            if (flow.closed || flow.attempted || !preview || preview.can_send !== true || preview.sending_enabled !== true
                || !preview.changes.length || selected() !== activityKey(preview.activity_doctype, preview.comment)) return;
            if (!fresh()) { stale(); return; }
            // A network error can be ambiguous. Never retry from this confirmation.
            flow.attempted = true; disableSend();
            dialog.fields_dict.activity.$input.prop("disabled", true);
            message(t("Sending activity to HERO …"));
            try {
                const result = await rpc("project_logbook_send", {comment: preview.comment, activity_doctype: preview.activity_doctype,
                    fingerprint: preview.fingerprint, confirmed: 1}, true);
                if (result?.status !== "SENT") throw new Error(t("Unexpected activity send result."));
                if (flow.closed) return;
                dialog.hide();
                frappe.show_alert({message: t("Activity sent to HERO."), indicator: "green"});
                if (forms.get(frm) === owner && frm.doc.name === project && !frm.is_dirty()) await frm.reload_doc();
            } catch (error) {
                if (!flow.closed) message(`${t("The activity could not be confirmed as sent. Reopen the activity list before trying again.")} ${errorText(error)}`, true);
            }
        }
        dialog = new frappe.ui.Dialog({
            title: t("Send activity to HERO"),
            fields: [
                {fieldtype: "Select", fieldname: "activity", label: t("Activity"), options: [], reqd: 1, onchange: previewSelected},
                {fieldtype: "HTML", fieldname: "summary"},
            ],
            primary_action_label: t("Confirm and send once"), primary_action: send,
            on_hide: () => { flow.closed = true; flow.request++; if (owner.flow === flow) owner.flow = null; },
        });
        flow.cancel = () => { flow.closed = true; flow.request++; dialog.hide(); };
        dialog.show(); disableSend();
        dialog.fields_dict.activity.$input.prop("disabled", true);
        message(t("Loading project activities …"));
        (async () => {
            try {
                const result = await rpc("project_logbook_candidates", {project});
                if (flow.closed) return;
                if (!fresh()) { stale(); return; }
                if (!Array.isArray(result?.comments)) throw new Error(t("Invalid activity list."));
                if (result.blocked_reason) { message(text(result.blocked_reason), true); return; }
                const names = new Set();
                flow.candidates = result.comments.map(item => {
                    const kind = activityKind(item?.activity_doctype);
                    if (!["Comment", "Version"].includes(kind)) throw new Error(t("Invalid activity list."));
                    return {...item, activity_doctype: kind, key: activityKey(kind, item?.name)};
                }).filter(item => typeof item.name === "string" && item.name && !names.has(item.key) && names.add(item.key));
                if (!flow.candidates.length) { message(t("No eligible project activities are available.")); return; }
                const field = dialog.fields_dict.activity;
                field.df.options = [{value: "", label: t("Choose an activity")}].concat(flow.candidates.map((item, index) => {
                    const body = text(item.content).replace(/\s+/g, " ");
                    return {value: item.key, label: `${index + 1}. ${text(item.creation)} · ${body.length > 120 ? body.slice(0, 120) + "…" : body}`};
                }));
                field.refresh(); field.$input.prop("disabled", false);
                message(t("Choose an activity to preview."));
            } catch (error) { if (!flow.closed) message(errorText(error), true); }
        })();
    }

    frappe.ui.form.on("Project", {
        refresh(frm) {
            forms.get(frm)?.flow?.cancel();
            const owner = {}; forms.set(frm, owner);
            frm.remove_custom_button?.(t("Send activity to HERO"));
            if (frm.is_new() || !frm.doc.name || !allowed()) return;
            frm.add_custom_button(t("Send activity to HERO"), () => open(frm, owner));
        },
    });
})();
