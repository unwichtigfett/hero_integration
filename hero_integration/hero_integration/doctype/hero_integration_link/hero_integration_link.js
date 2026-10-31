// Held HERO records offer only Approve / Reject (user decision 2026-10-31).
frappe.ui.form.on("HERO Integration Link", {
	refresh(frm) {
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
