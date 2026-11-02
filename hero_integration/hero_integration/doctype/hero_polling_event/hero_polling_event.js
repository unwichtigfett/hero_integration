frappe.ui.form.on("HERO Polling Event", {
	refresh(frm) {
		window.hero_integration_diagnostics?.bindField?.(frm, "detail");
	},
});
