import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../public/js/hero_workspace.js", import.meta.url), "utf8");

function workspaceUi({ styleFailure = false } = {}) {
    const sandbox = {
        URL,
        URLSearchParams,
        console,
        setTimeout,
        window: {},
        location: { search: "" },
        frappe: {
            csrf_token: "",
            pages: { "hero-integration": {} },
            require: () => Promise.resolve(),
        },
    };
    sandbox.document = {
        querySelector: () => null,
        createElement: () => ({ dataset: {} }),
        head: {
            append: (element) => queueMicrotask(() => (styleFailure ? element.onerror?.() : element.onload?.())),
        },
    };
    vm.runInNewContext(source, sandbox, { filename: "hero_workspace.js" });
    return sandbox.window.hero_integration_ui;
}

test("a stylesheet failure reaches the HERO error boundary", async () => {
    await assert.rejects(
        workspaceUi({ styleFailure: true }).ensureStyles(),
        /HERO interface stylesheet could not be loaded/,
    );
});

test("Frappe's resolving script error path is surfaced through missing module contracts", async () => {
    await assert.rejects(
        workspaceUi().ensureStyles(),
        /HERO interface assets could not be loaded: hero_tools\.js/,
    );
});
