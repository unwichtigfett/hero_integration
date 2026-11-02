// Held HERO records offer only Approve / Reject (user decision 2026-10-31).
frappe.ui.form.on("HERO Integration Link", {
	refresh(frm) {
		// Translate known read-only diagnostics for display without changing the
		// stored canonical text, which is used by the review workflow.
		const field = frm.fields_dict?.conflict_detail;
		const renderDetail = (control, detail) => {
			if (!control?.disp_area || typeof detail !== "string" || !detail) return;
			const translated = window.hero_integration_diagnostics?.translate(detail) ?? __(detail);
			if (translated !== detail) control.disp_area.textContent = translated;
		};
		if (field && typeof field.set_disp_area === "function" && !field._heroDiagnosticWrapped) {
			const original = field.set_disp_area;
			field.set_disp_area = function (...args) {
				const result = original.apply(this, args);
				renderDetail(this, this.value ?? args[0]);
				return result;
			};
			field._heroDiagnosticWrapped = true;
		}
		renderDetail(field, frm.doc.conflict_detail);
		if (frm.doc.sync_status !== "Pending Review" || !frappe.user.has_role("System Manager")) {
			return;
		}
		const decide = (decision, message) =>
			frappe.confirm(message, () =>
				frappe
					.call({
						method: "hero_integration.api.decide_hero_review",
						args: { link: frm.doc.name, decision },
						freeze: true,
					})
					.then(() => frm.reload_doc())
			);
		frm.add_custom_button(__("Approve"), () =>
			decide("approve", __("Approve this record? Linked HERO records are imported by the next run."))
		);
		frm.add_custom_button(__("Reject"), () =>
			decide("reject", __("Reject this record? The {0} {1} is deleted and not imported again.", [
				frm.doc.target_doctype,
				frm.doc.target_name,
			]))
		);
	},
});
