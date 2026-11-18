// Address defaults are ordinary unsaved Project edits, in both Design modes.
(() => {
    "use strict";
    const addressField = "custom_project_address", editField = "custom_edit_project_address";
    const states = new WeakMap();
    const manual = frm => Boolean(Number(frm.doc[editField]));
    const signature = frm => JSON.stringify([frm.doc.name, frm.doc.customer, frm.doc[addressField], manual(frm)]);

    function state(frm) {
        if (!states.has(frm)) states.set(frm, {sequence: 0, targets: new Set()});
        return states.get(frm);
    }
    function paint(frm, text) {
        for (const target of state(frm).targets) {
            if (target.isConnected === false) state(frm).targets.delete(target);
            else target.textContent = text;
        }
    }
    function update(frm, fill = false) {
        if (!frm.fields_dict?.[addressField]) return Promise.resolve();
        const current = state(frm), snapshot = signature(frm);
        if (current.job?.signature === snapshot) {
            current.job.fill ||= fill;
            return current.job.promise;
        }
        const job = {signature: snapshot, token: ++current.sequence, fill};
        current.job = job;
        job.promise = load(frm, job).finally(() => {
            if (current.job === job) current.job = null;
        });
        return job.promise;
    }
    async function load(frm, job) {
        const current = state(frm), {token, signature: snapshot} = job;
        paint(frm, __("Loading project site address…"));
        try {
            const response = await frappe.call({
                method: "hero_integration.services.project_address.get_address_context",
                type: "GET", quiet: true,
                args: {project: frm.is_new() ? null : frm.doc.name, customer: frm.doc.customer || null,
                    address: frm.doc[addressField] || null, manual: manual(frm) ? 1 : 0},
            });
            if (current.sequence !== token || signature(frm) !== snapshot) return;
            const data = response.message;
            if (response.exc || !["ready", "missing"].includes(data?.state)) throw new Error("Invalid address context");
            if (job.fill && !manual(frm) && !frm.doc[addressField] && data.name && (frm.is_new() || frm.has_perm("write"))
                && (frm.doc[addressField] || null) !== data.name) {
                // set_value runs native field triggers and dirty tracking, never save().
                await frm.set_value(addressField, data.name);
                return;
            }
            paint(frm, data.address || __("No project address is available. The address is optional."));
        } catch (_) {
            if (current.sequence === token && signature(frm) === snapshot) {
                paint(frm, __("The project address is unavailable or you do not have permission to read it."));
            }
        }
    }
    frappe.provide("frappe.hero_project_address");
    frappe.hero_project_address.render = (frm, target) => {
        state(frm).targets.add(target);
        return update(frm);
    };
    frappe.ui.form.on("Project", {
        setup(frm) {
            frm.set_query(addressField, () => ({filters: {disabled: 0}}));
        },
        refresh(frm) {
            // Existing projects are not silently changed merely by opening them.
            return update(frm, frm.is_new());
        },
        customer(frm) { return update(frm, true); },
        custom_edit_project_address(frm) { return update(frm, !manual(frm)); },
        custom_project_address(frm) { return update(frm); },
        async validate(frm) {
            // Wait for an optional default; never replace an existing selection.
            if (!manual(frm)) {
                await update(frm, true);
            }
        },
    });
})();
