import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import vm from "node:vm";

const formSource = readFileSync(new URL(
    "../hero_integration/doctype/hero_sync_change_event/hero_sync_change_event.js", import.meta.url), "utf8");

test("a field decision previews the wire value and waits for separate confirmation", async () => {
    let refresh, openDialog, dialog, confirmation, message, reloads = 0;
    const calls = [];
    const frappe = {
        user: {has_role: () => true},
        ui: {
            form: {on: (_, handlers) => { refresh = handlers.refresh; }},
            Dialog: class {
                constructor(options) { dialog = options; }
                show() {}
                hide() {}
            }
        },
        call: async ({method, args}) => {
            calls.push({method, args});
            return {message: {
                confirmation_fingerprint: "review-signature", chosen_value: "new", hero_value: "<Bonn>",
                erpnext_value: "Kiel", field: "city", target_name: "A1", target_doctype: "Address",
                operation: "fixtureUpdate", updates_local: true,
                remote_change: {object_type: "Contact", id: 12, field: "ort", value: "wire_new"}
            }};
        },
        utils: {escape_html: (text) => String(text).replaceAll("<", "&lt;").replaceAll(">", "&gt;")},
        confirm: (html, onYes) => { message = html; confirmation = onYes; }
    };
    vm.runInNewContext(formSource, {frappe, __: (text) => text});
    refresh({doc: {outcome: "FIELD_CONFLICT", name: "event", modified: "revision"},
        add_custom_button: (_, onClick) => { openDialog = onClick; },
        reload_doc: () => { reloads++; }});
    openDialog();
    await dialog.primary_action({choice: "CUSTOM", mutation_mapping: "approved", value_json: '"new"'});
    assert.deepEqual(calls.map(c => c.method), ["hero_integration.api.preview_sync_field"]);
    for (const text of ["&lt;Bonn&gt;", "Kiel", "fixtureUpdate", "Contact / 12", "ort", "wire_new"])
        assert.ok(message.includes(text));
    assert.ok(!message.includes("<Bonn>"));
    assert.equal(reloads, 0);
    await confirmation();
    assert.equal(calls[1].method, "hero_integration.api.resolve_sync_field");
    assert.equal(calls[1].args.confirmation_fingerprint, "review-signature");
    assert.equal(calls[1].args.confirmed, 1);
    assert.equal(calls[1].args.expected_modified, "revision");
    assert.equal(reloads, 1);
});
