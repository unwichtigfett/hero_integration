import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../public/js/hero_workspace.js", import.meta.url), "utf8");

function workspaceUi({ styleFailure = false, loaded = {}, requests = [], capture = () => {} } = {}) {
    const sandbox = {
        URL,
        URLSearchParams,
        console,
        setTimeout,
        window: {...loaded},
        location: { search: "" },
        __: value => value,
        frappe: {
            csrf_token: "",
            pages: { "hero-integration": {} },
            require: (path) => { requests.push(path); return Promise.resolve(); },
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
    capture(sandbox.window);
    return sandbox.window.hero_integration_ui;
}

test("workspace delegates its text to the current Frappe translation dictionary", () => {
    const messages = {"Settings": "Einstellungen"};
    const sandbox = {window: {}, frappe: {}, __: value => messages[value] || value};
    vm.runInNewContext(source, sandbox, {filename: "hero_workspace.js"});
    assert.equal(sandbox.window.hero_integration_i18n.text("Settings"), "Einstellungen");
    assert.equal(sandbox.window.hero_integration_i18n.text("Unknown text"), "Unknown text");
    delete messages.Settings;
    assert.equal(sandbox.window.hero_integration_i18n.text("Settings"), "Settings");
});

test("a context picks the HERO wording over a generic Frappe translation", () => {
    const messages = {"Area": "Fläche", "Area:HERO data area": "Bereich"};
    const __ = (value, replace, context) => (context && messages[`${value}:${context}`]) || messages[value] || value;
    const sandbox = {window: {}, frappe: {}, __};
    vm.runInNewContext(source, sandbox, {filename: "hero_workspace.js"});
    assert.equal(sandbox.window.hero_integration_i18n.text("Area", "HERO data area"), "Bereich");
    assert.equal(sandbox.window.hero_integration_i18n.text("Area"), "Fläche");
});

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

test("the content-addressed editor is reused rather than loading an unhashed legacy copy", async () => {
    const requests = [];
    const loaded = Object.fromEntries(["hero_workspace_tools", "hero_integration_overview", "hero_integration_settings_ui", "hero_integration_api_summary", "hero_integration_mapping_editor", "hero_integration_mapping_simple", "hero_integration_diagnostics", "hero_integration_migration"].map((name) => [name, {}]));
    await workspaceUi({loaded, requests}).ensureStyles();
    assert.deepEqual(requests, []);
});

test("workspace routes remain local and encode untrusted parameter text", () => {
    let platform;
    workspaceUi({capture: (window) => { platform = window.hero_integration_platform; }});
    const value = "snapshot&hero_section=settings#fragment";
    const route = platform.url("api", {hero_tool: "graph", snapshot: value});
    const parsed = new URL(route, "https://example.invalid");
    assert.equal(parsed.origin, "https://example.invalid");
    assert.equal(parsed.pathname, "/app/hero-integration");
    assert.equal(parsed.searchParams.get("snapshot"), value);
    assert.equal(parsed.searchParams.get("hero_section"), "api");
    assert.equal(parsed.searchParams.get("hero_tool"), "graph");
    assert.equal(parsed.hash, "");
});

test("API summary renders an upstream error as text in the status region", async () => {
    class Element {
        constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.textContent = ""; }
        append(...children) { this.children.push(...children); }
        replaceChildren(...children) { this.children = children; }
        setAttribute(name, value) { this.attributes[name] = String(value); }
        addEventListener() {}
        find(predicate) { if (predicate(this)) return this; for (const child of this.children) { const found = child.find(predicate); if (found) return found; } }
    }
    const markup = '<img src=x onerror=alert(1)>';
    const api = async () => { throw new Error(markup); };
    const window = {hero_integration_platform: {api, url: () => "/app/hero-integration"}};
    const document = {createElement: tag => new Element(tag)};
    const summary = await readFile(new URL("../public/js/hero_api_summary.js", import.meta.url), "utf8");
    vm.runInNewContext(summary, {window, document});
    const workspace = new Element("main");
    await window.hero_integration_api_summary.mount(workspace);
    const status = workspace.find(element => element.attributes.role === "status");
    assert.equal(status.textContent, markup);
    assert.equal(status.dataset.state, "error");
    assert.equal(workspace.find(element => element.tag === "img"), undefined);
});
