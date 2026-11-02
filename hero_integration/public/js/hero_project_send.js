// One "Send to HERO" button per imported Project: Notes and Phase (owner decision 2026-11-01).
(() => {
	"use strict";

	const method = "hero_integration.api.";
	const allowed = () => frappe.user.has_role("System Manager") || frappe.user.has_role("HERO Writeback");

	const row = (label, hero, value) =>
		$("<tr></tr>").append(
			$("<td></td>").text(label),
			$("<td></td>").text(hero == null ? "—" : String(hero)),
			$("<td></td>").text(value == null ? "—" : String(value)),
		);

	const render = (dialog, preview) => {
		const wrapper = dialog.fields_dict.summary.$wrapper.empty();
		if (!preview.sending_enabled) {
			wrapper.append($("<p class='text-warning'></p>").text(__("Sending to HERO is switched off globally.")));
		}
		const table = $("<table class='table table-bordered'></table>").append(
			$("<tr></tr>").append($("<th></th>").text(__("Field")), $("<th></th>").text(__("In HERO")),
				$("<th></th>").text(__("Will be sent"))),
		);
		preview.changes.forEach((change) => table.append(row(__(change.label), change.hero, change.new)));
		wrapper.append(preview.changes.length ? table : $("<p></p>").text(__("No changes to send.")));
		preview.conflicts.forEach((change) => wrapper.append($("<p class='text-danger'></p>").text(
			__("{0} was changed in HERO since the last import and is not overwritten.", [__(change.label)]))));
		dialog.preview = preview;
		dialog.get_primary_btn().prop("disabled", !preview.can_send);
	};

	const load = (frm, dialog, phase_id) =>
		frappe.call({method: method + "project_send_preview", args: {project: frm.doc.name, phase_id: phase_id || null}})
			.then((response) => render(dialog, response.message));

	const open = (frm) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Send to HERO"),
			fields: [
				{fieldtype: "Select", fieldname: "phase", label: __("Phase"), options: []},
				{fieldtype: "HTML", fieldname: "summary"},
			],
			primary_action_label: __("Confirm and send once"),
			primary_action: (values) => {
				frappe.call({method: method + "project_send", freeze: true, args: {
					project: frm.doc.name, phase_id: values.phase || null,
					fingerprint: dialog.preview.fingerprint, confirmed: 1,
				}}).then((response) => {
					dialog.hide();
					frappe.show_alert({message: __("Sent to HERO: {0}", [response.message.changes.map((label) => __(label)).join(", ")]), indicator: "green"});
					frm.reload_doc();
				});
			},
		});
		frappe.call({method: method + "project_send_preview", args: {project: frm.doc.name}}).then((response) => {
			const preview = response.message;
			const field = dialog.fields_dict.phase;
			field.df.options = preview.phases.map((phase) => ({value: String(phase.id), label: phase.name}));
			field.refresh();
			field.set_value(preview.hero.phase_id ? String(preview.hero.phase_id) : "");
			field.$input.on("change", () => load(frm, dialog, field.get_value()));
			render(dialog, preview);
			dialog.show();
		});
	};

	frappe.ui.form.on("Project", {
		refresh(frm) {
			if (frm.is_new() || !frm.doc.custom_hero_source_id || !allowed()) return;
			frm.add_custom_button(__("Send to HERO"), () => {
				if (frm.is_dirty()) {
					frappe.msgprint(__("Save the project before sending it to HERO."));
					return;
				}
				open(frm);
			});
		},
	});
})();
