/* Presentation-only translations for canonical, stored HERO diagnostics. */
(() => {
    "use strict";
    if (window.hero_integration_diagnostics) return;
    const tr = (value) => value == null ? value : (window.hero_integration_i18n?.text(value) ?? window.__?.(value) ?? value);
    const format = (message, ...values) => tr(message).replace(/\{(\d+)\}/g, (_, index) => String(values[index] ?? ""));
    // Stored diagnostics stay canonical in the database. Translate known messages
    // when displaying them so changing a user's language also changes old runs.
    function diagnostic(value) {
        if (!value) return value;
        const prefix = "Recheck requested: ";
        const recheck = String(value).startsWith(prefix);
        const text = recheck ? String(value).slice(prefix.length) : String(value);
        const exact = tr(text);
        if (exact !== text) return recheck ? format("Recheck requested: {0}", exact) : exact;
        const customerReviewReasons = [
            "The HERO customer has no unambiguous ERPNext link; the Project has no customer.",
            "The HERO customer is deleted in HERO; the Project has no customer.",
            "The HERO customer is a contact person, not a customer; the Project has no customer.",
        ];
        for (const reason of customerReviewReasons) {
            if (text.startsWith(`${reason} The project address is missing or incomplete (`)) {
                const translated = `${tr(reason)} ${diagnostic(text.slice(reason.length + 1))}`;
                return recheck ? format("Recheck requested: {0}", translated) : translated;
            }
        }
        // Import results append zero or more notes to a canonical result. Peel
        // only recognized suffixes so names and external text remain intact.
        const appendedNotes = [
            "The HERO email address is not valid in ERPNext; it was not imported.",
            "A HERO phone text is not a valid ERPNext number; the first number was imported and the full text kept as a comment.",
            "Customer primary address is incomplete. It was not imported.",
            "The HERO address has no country; Germany was assumed.",
        ];
        for (const note of appendedNotes) {
            if (text.endsWith(` ${note}`)) {
                const translated = `${diagnostic(text.slice(0, -note.length - 1))} ${tr(note)}`;
                return recheck ? format("Recheck requested: {0}", translated) : translated;
            }
        }
        const namingNote = text.match(/^(.*?) (HERO has no (?:customer number|project display_id)\.|HERO number .*? (?:is not text\.|cannot be an ERPNext document name \(only letters, digits and single inner hyphens, at most 140 characters\)\.)) (A collision-safe internal ID names the document; no replacement business number was generated\. The original HERO value stays in its field\.|ERPNext's naming series names the new document; the HERO value stays in its field\.)$/);
        if (namingNote) {
            const translated = `${diagnostic(namingNote[1])} ${diagnostic(namingNote[2])} ${tr(namingNote[3])}`;
            return recheck ? format("Recheck requested: {0}", translated) : translated;
        }
        const addressReview = text.match(/^The project address is missing or incomplete \((.*)\); the title omits it\.$/);
        if (addressReview) {
            const translated = format("The project address is missing or incomplete ({0}); the title omits it.", diagnostic(addressReview[1]));
            return recheck ? format("Recheck requested: {0}", translated) : translated;
        }
        const preserved = text.match(/^(.*) Local changes were preserved: (.+)\.$/);
        if (preserved) {
            const translated = format('{0} Local changes were preserved: {1}.', diagnostic(preserved[1]), preserved[2]);
            return recheck ? format("Recheck requested: {0}", translated) : translated;
        }
        const areaBlocker = text.match(/^(.*?) needs an approved read service with an active mapping for (.*?) on a snapshot of the HERO source instance "(.*)"\. Approve one under API & Schema and Mapping\.$/);
        if (areaBlocker) {
            const translated = format('{0} needs an approved read service with an active mapping for {1} on a snapshot of the HERO source instance "{2}". Approve one under API & Schema and Mapping.',
                tr(areaBlocker[1]), tr(areaBlocker[2]), areaBlocker[3]);
            return recheck ? format("Recheck requested: {0}", translated) : translated;
        }
        const nameConflict = text.match(/^(.+?) (.+?) already exists without this HERO record's link\. The HERO number is not reassigned, suffixed or merged; check whether \2 is the same (.+?) and link or rename it\.$/);
        if (nameConflict) {
            const translated = format("{0} {1} already exists without this HERO record's link. The HERO number is not reassigned, suffixed or merged; check whether {1} is the same {2} and link or rename it.",
                tr(nameConflict[1]), nameConflict[2], tr(nameConflict[3]));
            return recheck ? format("Recheck requested: {0}", translated) : translated;
        }
        const numberDrift = text.match(/^HERO (customer number|project display_id) changed from (.*?) to (.*?)\. (Customer|Project) (.*?) keeps its name and number until reviewed; set (.*?) to \3 to accept the new number\.$/);
        if (numberDrift) {
            const translated = format('HERO {0} changed from {1} to {2}. {3} {4} keeps its name and number until reviewed; set {5} to {2} to accept the new number.',
                tr(numberDrift[1]), numberDrift[2], numberDrift[3], tr(numberDrift[4]), numberDrift[5], numberDrift[6]);
            return recheck ? format("Recheck requested: {0}", translated) : translated;
        }
        const templates = [
            [/^A new target record would be created named (.+)\.$/, "A new target record would be created named {0}."],
            [/^ERPNext rejected the record \(([A-Za-z_][A-Za-z0-9_]*)\); see Error Log\.$/, "ERPNext rejected the record ({0}); see Error Log."],
            [/^([A-Za-z][A-Za-z ]+) is not installed on this site\.$/, "{0} is not installed on this site.", [0]],
            [/^HERO has no (customer number|project display_id)\.$/, "HERO has no {0}.", [0]],
            [/^HERO number (.+) is not text\.$/, "HERO number {0} is not text."],
            [/^HERO number (.+) cannot be an ERPNext document name \(only letters, digits and single inner hyphens, at most 140 characters\)\.$/, "HERO number {0} cannot be an ERPNext document name (only letters, digits and single inner hyphens, at most 140 characters)."],
            [/^Generated Project\.project_name exceeds its (\d+)-character field limit\.$/, "Generated Project.project_name exceeds its {0}-character field limit."],
            [/^Project trade (.+) has no confirmed HERO-to-ERPNext title mapping\.$/, "Project trade {0} has no confirmed HERO-to-ERPNext title mapping."],
            [/^(ProjectMatch\.[\w.]+) is not a complete formatted project address\.$/, "{0} is not a complete formatted project address."],
            [/^(ProjectMatch\.[\w.]+) is required and must be an object\.$/, "{0} is required and must be an object."],
            [/^(ProjectMatch\.[\w.]+) is required for the ERPNext project title\.$/, "{0} is required for the ERPNext project title."],
            [/^HERO response omits the source of: (.+)\.$/, "HERO response omits the source of: {0}."],
            [/^HERO cleared values that the no-clear policy does not write: (.+)\.$/, "HERO cleared values that the no-clear policy does not write: {0}."],
            [/^Existing (.+) has no stable HERO link\.$/, "Existing {0} has no stable HERO link.", [0]],
            [/^Awaits administrator approval: (.*)$/, "Awaits administrator approval: {0}", [0]],
            [/^Event time or recurrence requires review; unrelated field changes were considered\. (.*)$/, "Event time or recurrence requires review; unrelated field changes were considered. {0}", [0]],
            [/^Field conflict requires review: (.+)\.$/, "Field conflict requires review: {0}."],
            [/^Task fields require review: (.+)$/, "Task fields require review: {0}"],
            [/^Another HERO run is active: (.+)\.$/, "Another HERO run is active: {0}."],
            [/^Polling configuration is not supported \((.*)\); no HERO request was made\.$/, "Polling configuration is not supported ({0}); no HERO request was made."],
            [/^Automatic sync is blocked: (.+)$/, "Automatic sync is blocked: {0}", [0]],
            [/^Materialized monitoring is blocked: (.+)$/, "Materialized monitoring is blocked: {0}", [0]],
            [/^(.+): the automatic run could not be started; see the error log\.$/, "{0}: the automatic run could not be started; see the error log.", [0]],
            [/^(.+): run (.+) queued\.$/, "{0}: run {1} queued.", [0]],
        ];
        for (const [pattern, template, translatedValues = []] of templates) {
            const match = text.match(pattern);
            if (match) {
                const values = match.slice(1).map((value, index) => translatedValues.includes(index) ? diagnostic(value) : value);
                const translated = format(template, ...values);
                return recheck ? format("Recheck requested: {0}", translated) : translated;
            }
        }
        const translated = tr(text);
        return recheck ? format("Recheck requested: {0}", translated) : translated;
    }
    window.hero_integration_diagnostics = Object.freeze({translate: diagnostic});
})();
