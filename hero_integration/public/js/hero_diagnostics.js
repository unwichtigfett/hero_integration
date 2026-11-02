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
        const preserved = text.match(/^(.*) Local changes were preserved: (.+)\.$/);
        if (preserved) {
            const translated = format('{0} Local changes were preserved: {1}.', tr(preserved[1]), preserved[2]);
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
                nameConflict[1], nameConflict[2], nameConflict[3]);
            return recheck ? format("Recheck requested: {0}", translated) : translated;
        }
        const numberDrift = text.match(/^HERO (customer number|project display_id) changed from (.*?) to (.*?)\. (Customer|Project) (.*?) keeps its name and number until reviewed; set (.*?) to \3 to accept the new number\.$/);
        if (numberDrift) {
            const translated = format('HERO {0} changed from {1} to {2}. {3} {4} keeps its name and number until reviewed; set {5} to {2} to accept the new number.',
                tr(numberDrift[1]), ...numberDrift.slice(2));
            return recheck ? format("Recheck requested: {0}", translated) : translated;
        }
        const templates = [
            [/^HERO response omits the source of: (.+)\.$/, "HERO response omits the source of: {0}."],
            [/^HERO cleared values that the no-clear policy does not write: (.+)\.$/, "HERO cleared values that the no-clear policy does not write: {0}."],
            [/^Existing (.+) has no stable HERO link\.$/, "Existing {0} has no stable HERO link."],
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
                const values = match.slice(1).map((value, index) => translatedValues.includes(index) ? tr(value) : value);
                const translated = format(template, ...values);
                return recheck ? format("Recheck requested: {0}", translated) : translated;
            }
        }
        const translated = tr(text);
        return recheck ? format("Recheck requested: {0}", translated) : translated;
    }
    window.hero_integration_diagnostics = Object.freeze({translate: diagnostic});
})();
