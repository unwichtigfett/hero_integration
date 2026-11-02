(() => {
	function showTranslation(frm) {
		const field = frm.fields_dict?.detail;
		const wrapper = field?.$wrapper?.[0];
		if (!wrapper) return;
		if (!field._heroTranslationNote) {
			field._heroTranslationNote = document.createElement("div");
			field._heroTranslationNote.className = "text-muted small hero-translated-diagnostic";
			wrapper.appendChild(field._heroTranslationNote);
		}
		const value = frm.doc.detail || "";
		const translated = window.hero_integration_diagnostics?.translate(value) ?? value;
		field._heroTranslationNote.hidden = !translated || translated === value;
		field._heroTranslationNote.textContent = translated === value ? "" : __("Translation: {0}", [translated]);
	}
	frappe.ui.form.on("HERO Integration Record", {refresh: showTranslation, detail: showTranslation});
})();
