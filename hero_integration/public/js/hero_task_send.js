// Send through the configured native Mutation mapping.
(() => {
	"use strict";

	const method = "hero_integration.api.";
	const allowed = () => frappe.user.has_role("System Manager") || frappe.user.has_role("HERO Writeback");
	const shown = (value) => (value == null || value === "" ? "—" : String(value));

	const render = (dialog, preview) => {
		const wrapper = dialog.fields_dict.summary.$wrapper.empty();
		if (!preview.sending_enabled) {
			wrapper.append($("<p class='text-warning'></p>").text(__("Sending to HERO is switched off globally.")));
		}
		if (preview.create) {
			wrapper.append($("<p></p>").text(__("The task does not exist in HERO yet and will be created there.")));
		}
		const table = $("<table class='table table-bordered'></table>").append(
			$("<tr></tr>").append($("<th></th>").text(__("Field")), $("<th></th>").text(__("In HERO")),
				$("<th></th>").text(__("Will be sent"))),
		);
		preview.changes.forEach((change) => table.append($("<tr></tr>").append(
			$("<td></td>").text(__(change.label)), $("<td></td>").text(shown(change.hero)),
			$("<td></td>").text(shown(change.new)))));
		wrapper.append(preview.changes.length ? table : $("<p></p>").text(__("No changes to send.")));
		preview.conflicts.forEach((change) => wrapper.append($("<p class='text-danger'></p>").text(
			__("{0} was changed in HERO since the last import and is not overwritten.", [__(change.label)]))));
		dialog.preview = preview;
		dialog.get_primary_btn().prop("disabled", !preview.can_send);
	};

	const open = (frm) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Send to HERO"),
			fields: [{fieldtype: "HTML", fieldname: "summary"}],
			primary_action_label: __("Confirm and send once"),
			primary_action: () => {
				frappe.call({method: method + "task_send", freeze: true, args: {
					task: frm.doc.name, fingerprint: dialog.preview.fingerprint, confirmed: 1,
				}}).then((response) => {
					dialog.hide();
					frappe.show_alert({message: __("Sent to HERO: {0}", [response.message.changes.map((label) => __(label)).join(", ")]), indicator: "green"});
					frm.reload_doc();
				});
			},
		});
		frappe.call({method: method + "task_send_preview", args: {task: frm.doc.name}}).then((response) => {
			render(dialog, response.message);
			dialog.show();
		});
	};

	frappe.ui.form.on("Task", {
		refresh(frm) {
			if (frm.is_new() || !allowed()) return;
			frm.add_custom_button(__("Send to HERO"), () => {
				if (frm.is_dirty()) {
					frappe.msgprint(__("Save the task before sending it to HERO."));
					return;
				}
				open(frm);
			});
		},
	});
})();
