import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import test from "node:test";

const source = readFileSync(new URL("../public/js/project_workspace.js", import.meta.url), "utf8");
class Element {
    constructor(tag) {
        this.tagName = tag; this.children = []; this.attributes = {}; this.events = {};
        this.dataset = {}; this.className = ""; this.value = ""; this.text = ""; this.style = {};
        this.classList = {
            add: (...values) => { this.className = [...new Set([...this.className.split(" ").filter(Boolean), ...values])].join(" "); },
            remove: value => { this.className = this.className.split(" ").filter(item => item !== value).join(" "); },
            contains: value => this.className.split(" ").includes(value),
        };
    }
    set textContent(value) { this.text = String(value); this.replaceChildren(); }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(""); }
    append(...children) { for (const child of children) { child.remove(); child.parent = this; this.children.push(child); } }
    prepend(...children) { for (const child of children.reverse()) { child.remove(); child.parent = this; this.children.unshift(child); } }
    replaceChildren(...children) { for (const child of this.children) child.parent = null; this.children = []; this.append(...children); }
    insertBefore(child, before) { child.remove(); child.parent = this; this.children.splice(this.children.indexOf(before), 0, child); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name]; }
    addEventListener(name, handler) { (this.events[name] ||= []).push(handler); }
    dispatch(name) { for (const handler of this.events[name] || []) handler({preventDefault() {}, currentTarget: this}); }
    all() { return this.children.flatMap(child => [child, ...child.all()]); }
    querySelectorAll(selector) {
        return this.all().filter(child => selector.startsWith(".") ? child.classList.contains(selector.slice(1)) : child.tagName === selector);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const deferred = () => {
    let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve};
};
async function setup({handler, isNew = false, canWrite = true, canCreate = true, maxReached = false,
    translate = value => value, addressHandler, isAdmin = false, categoryHandler, pdfHandler,
    responsibleField = false, withSidebar = false, uploadOptionsHandler, designMode = "Custom"} = {}) {
    let controller, active = "__details", route = ["Form", "Project", "PROJECT-1"];
    const calls = [], routes = [], creates = [], dialogs = [], addressCalls = [], categoryCalls = [], pdfCalls = [],
        prompts = [], messages = [], imageUploaders = [], uploadOptionCalls = [], toolbarButtons = new Map();
    const native = {uploads: 0, limits: 0, assigns: 0, saves: 0, uploadedFiles: []};
    const document = {
        createElement: tag => new Element(tag), head: new Element("head"),
        getElementById(id) { return this.head.all().find(element => element.id === id); },
    };
    const wrapper = new Element("main");
    const pageRoot = new Element("div"), sidebar = new Element("aside"), workspaceNavigation = new Element("nav");
    pageRoot.append(wrapper, sidebar);
    const fieldnames = ["__details", "custom_project_documents_tab", "custom_project_images_tab",
        "more_info_tab", "connections_tab", "monitor_progress_tab", "costing_tab"];
    const nativeTabs = fieldnames.map(fieldname => {
        const element = new Element("div"), link = new Element("a"), tabLink = new Element("li");
        link.className = "nav-link"; tabLink.append(link); wrapper.append(element);
        element.append(new Element("native-control"));
        return {df: {fieldname}, wrapper: [element], tab_link: [tabLink], is_active: () => active === fieldname};
    });
    class Dialog {
        constructor(config) {
            this.config = config; this.fields_dict = {preview: {$wrapper: [new Element("div")]}};
            dialogs.push(this);
        }
        show() { this.shown = true; }
    }
    class FileUploader {
        constructor(config) { imageUploaders.push(config); }
    }
    const frappe = {
        boot: {hero_design_mode: designMode},
        ui: {form: {on: (doctype, events) => { assert.equal(doctype, "Project"); controller = events; }}, Dialog, FileUploader},
        get_route: () => route,
        datetime: {str_to_user: value => String(value), get_today: () => "2026-11-16"},
        model: {can_read: () => true, can_create: () => canCreate},
        user: {has_role: role => isAdmin && role === "System Manager"},
        prompt: (fields, callback, title) => prompts.push({fields, callback, title}),
        msgprint: text => messages.push(text),
        set_route: (...args) => routes.push(args), new_doc: (...args) => creates.push(args),
        call: async request => {
            if (request.method.endsWith(".get_upload_options")) {
                uploadOptionCalls.push(request);
                return {message: uploadOptionsHandler ? await uploadOptionsHandler(request) : {
                    project: request.args.project, category: request.args.category, folder: "Home/Attachments/managed-image-folder",
                }};
            }
            if (request.method.endsWith("project_pdf_preview.get_page")) {
                pdfCalls.push(request);
                return {message: pdfHandler ? await pdfHandler(request) : {
                    page: request.args.page, page_count: 2,
                    data_url: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=",
                }};
            }
            if (request.method.endsWith(".get_address_context")) {
                addressCalls.push(request);
                return {message: addressHandler ? await addressHandler(request) :
                    {project: "PROJECT-1", state: "ready", address: "Synthetic site address"}};
            }
            if (request.method.endsWith(".set_image_category")) {
                categoryCalls.push(request);
                return {message: categoryHandler ? await categoryHandler(request) :
                    {file: request.args.file, category: request.args.category}};
            }
            calls.push(request);
            const data = {project: "PROJECT-1", modified: "2026-11-16 12:00:00", section: request.args.section,
                state: "ready", rows: [], next_start: null, context: {}};
            return {message: handler ? await handler(request, data) : data};
        },
    };
    class Parser {
        parseFromString(value) {
            return {body: {textContent: value.replace(/<(script|style|template)\b[^>]*>[\s\S]*?<\/\1>/gi, "").replace(/<[^>]*>/g, "")},
                querySelectorAll: () => []};
        }
    }
    const __ = (value, params = []) => translate(value).replace(/\{(\d+)\}/g,
        (match, index) => index < params.length ? String(params[index]) : match);
    vm.runInNewContext(source, {frappe, __, document, HTMLElement: Element, DOMParser: Parser,
        URL, window: {location: {origin: "https://example.invalid"}}});
    let dirty = false;
    const frm = {
        doc: {name: "PROJECT-1", project_name: "<img src=x onerror=alert(1)>", modified: "2026-11-16 12:00:00", status: "Open"},
        layout: {wrapper: [wrapper], tabs: nativeTabs},
        meta: {max_attachments: 4},
        is_new: () => isNew, is_dirty: () => dirty, has_perm: () => canWrite,
        attachments: {max_reached: () => { native.limits++; return maxReached; }, new_attachment: () => { native.uploads++; },
            get_attachments: () => [{}], attachment_uploaded: file => native.uploadedFiles.push(file)},
        assign_to: {add: () => { native.assigns++; }}, save: () => { native.saves++; },
        fields_dict: responsibleField ? {custom_project_responsible: {}} : {},
        set_query: (fieldname, query) => { native.responsibleQuery = {fieldname, query}; },
    };
    if (withSidebar) {
        frm.page = {wrapper: [pageRoot], sidebar: [sidebar]};
        frm.add_custom_button = (label, action) => {
            const element = new Element("button");
            const item = {element, action, attr: (name, value) => { element.setAttribute(name, value); return item; }};
            toolbarButtons.set(label, item); return item;
        };
        frm.remove_custom_button = label => toolbarButtons.delete(label);
    }
    const flush = async () => { for (let index = 0; index < 12; index++) await Promise.resolve(); };
    controller.refresh(frm); await flush();
    const panels = () => new Map(wrapper.all().filter(element => element.dataset.projectPanel)
        .map(element => [element.dataset.projectPanel, element]));
    const switchTab = async key => {
        const keys = ["details", "documents", "images", "appointments", "tasks", "assignments", "advanced"];
        active = fieldnames[keys.indexOf(key)]; controller.on_tab_change(frm); await flush();
        return panels().get(key);
    };
    const click = (root, label) => {
        const found = root.querySelectorAll("button").find(element => element.textContent === label);
        assert.ok(found, "Button: " + label); found.dispatch("click"); return found;
    };
    return {frm, controller, calls, routes, creates, dialogs, native, wrapper, panels, switchTab, click, flush, document,
        addressCalls, categoryCalls, pdfCalls, prompts, messages,
        imageUploaders, uploadOptionCalls, toolbarButtons, pageRoot, sidebar, workspaceNavigation,
        setLimit: value => { maxReached = value; },
        setRoute: value => { route = value; }, setDirty: value => { dirty = value; }};
}

test("seven panels augment native tabs; controls, saving and HERO actions are not replaced", async () => {
    const app = await setup();
    assert.equal(app.panels().size, 7);
    assert.equal(app.wrapper.querySelectorAll("native-control").length, 7);
    assert.equal(app.native.saves, 0);
    assert.deepEqual(app.calls.map(call => call.args.section), ["details"]);
    assert.equal(app.wrapper.querySelectorAll(".hp-header").length, 0);
    assert.equal(app.wrapper.querySelectorAll("h2").length, 0);
    assert.equal(app.wrapper.querySelectorAll("img").length, 0);
    assert.equal(app.document.head.children.length, 1);
});
test("panels load lazily and refresh does not accumulate DOM or stylesheet nodes", async () => {
    const app = await setup();
    await app.switchTab("tasks"); await app.switchTab("tasks");
    assert.equal(app.calls.filter(call => call.args.section === "tasks").length, 1);
    app.controller.refresh(app.frm); await app.flush();
    assert.equal(app.panels().size, 7);
    assert.equal(app.wrapper.querySelectorAll("native-control").length, 7);
    assert.equal(app.document.head.children.length, 1);
    assert.ok(app.calls.every(call => call.type === "GET"));
});
test("unsaved projects do not issue related-record reads", async () => {
    const app = await setup({isNew: true});
    for (const key of ["documents", "images", "tasks", "appointments"]) await app.switchTab(key);
    assert.equal(app.calls.length, 0);
    assert.match(app.panels().get("tasks").textContent, /Save the project/);
});
test("denied, failed and empty results are distinguishable and errors offer retry", async () => {
    const app = await setup({handler: (request, data) => {
        if (request.args.section === "tasks") return {...data, state: "permission_denied"};
        if (request.args.section === "documents") throw new Error("<script>private server detail</script>");
        return data;
    }});
    assert.match((await app.switchTab("tasks")).textContent, /do not have permission/);
    const documents = await app.switchTab("documents");
    assert.match(documents.textContent, /could not be loaded/);
    assert.ok(!documents.textContent.includes("private server detail"));
    assert.ok(documents.querySelectorAll("button").some(button => button.textContent === "Try again"));
    assert.match((await app.switchTab("images")).textContent, /No matching records/);
});
test("stale owner, route and document responses cannot render into another project", async () => {
    for (const mutate of [
        app => app.setRoute(["Form", "Project", "OTHER"]),
        app => { app.frm.doc.name = "OTHER"; },
        app => app.controller.refresh(app.frm),
    ]) {
        const waiting = deferred();
        const app = await setup({handler: (request, data) => request.args.section === "tasks" ? waiting.promise : data});
        const panel = await app.switchTab("tasks");
        mutate(app);
        waiting.resolve({project: "PROJECT-1", section: "tasks", state: "ready", rows: [{name: "SECRET", subject: "Late payload"}]});
        await app.flush();
        assert.ok(!panel.textContent.includes("Late payload"));
    }
});
test("overlapping refreshes ignore older responses", async () => {
    const first = deferred(); let count = 0;
    const app = await setup({handler: (request, data) => {
        if (request.args.section !== "tasks") return data;
        return ++count === 1 ? first.promise : {...data, rows: [{name: "T2", subject: "Current payload"}]};
    }});
    const panel = await app.switchTab("tasks");
    app.click(panel, "Refresh"); await app.flush();
    first.resolve({project: "PROJECT-1", section: "tasks", state: "ready", rows: [{name: "T1", subject: "Old payload"}]});
    await app.flush();
    assert.match(panel.textContent, /Current payload/);
    assert.ok(!panel.textContent.includes("Old payload"));
});
test("wrong response identity is rejected rather than displayed", async () => {
    const app = await setup({handler: (request, data) => ({...data, project: "OTHER"})});
    assert.match(app.panels().get("details").textContent, /could not be loaded/);
});
test("dirty customer changes invalidate an in-flight context without saving", async () => {
    const waiting = deferred();
    const app = await setup({handler: () => waiting.promise});
    app.setDirty(true); app.controller.customer(app.frm);
    waiting.resolve({project: "PROJECT-1", section: "details", state: "ready", rows: [],
        context: {customer: {name: "OLD", customer_name: "Old customer"}}});
    await app.flush();
    const panel = app.panels().get("details");
    assert.match(panel.textContent, /Save the project to refresh/);
    assert.ok(!panel.textContent.includes("Old customer"));
    assert.equal(app.native.saves, 0);
});
test("image upload requires category before opening the native uploader and retains limits", async () => {
    for (const maxReached of [false, true]) {
        const app = await setup({maxReached});
        const panel = await app.switchTab("images");
        app.click(panel, "Add images");
        assert.equal(app.native.limits, 1);
        assert.equal(app.imageUploaders.length, 0);
        assert.equal(app.prompts.length, maxReached ? 0 : 1);
        if (!maxReached) {
            const prompt = app.prompts.at(-1);
            assert.equal(prompt.fields[0].reqd, 1);
            await prompt.callback({category: "Dach"}); await app.flush();
            assert.equal(app.imageUploaders.length, 1);
            const upload = app.imageUploaders[0];
            assert.equal(upload.doctype, "Project"); assert.equal(upload.docname, "PROJECT-1");
            assert.equal(upload.folder, "Home/Attachments/managed-image-folder");
            assert.equal(upload.restrictions.max_number_of_files, 3);
            assert.ok(!upload.restrictions.allowed_file_types.includes(".svg"));
            assert.equal(upload.frm, app.frm); assert.equal(upload.make_attachments_public, undefined);
            assert.equal(app.native.uploads, 0);
        }
    }
    const readonly = await setup({canWrite: false});
    assert.ok(!(await readonly.switchTab("images")).querySelectorAll("button").some(button => button.textContent === "Add images"));
});
test("empty or obsolete category and quota changes cannot open an image upload", async () => {
    const waiting = deferred();
    const app = await setup({uploadOptionsHandler: () => waiting.promise});
    const panel = await app.switchTab("images");
    app.click(panel, "Add images");
    await app.prompts.at(-1).callback({category: ""});
    assert.equal(app.uploadOptionCalls.length, 0);
    const pending = app.prompts.at(-1).callback({category: "Dach"});
    app.setLimit(true);
    waiting.resolve({project: "PROJECT-1", category: "Dach", folder: "FOLDER"});
    await pending; await app.flush();
    assert.equal(app.imageUploaders.length, 0);
});
test("image categories follow the HTML groups with count and per-category add tile", async () => {
    const app = await setup({handler: (request, data) => ({...data,
        categories: request.args.section === "images" ? ["Dach", "Elektro"] : []})});
    const panel = await app.switchTab("images");
    const groups = panel.querySelectorAll(".hp-image-group");
    assert.equal(groups.length, 2);
    assert.match(groups[0].textContent, /Dach/); assert.match(groups[1].textContent, /Elektro/);
    app.click(groups[1], "Add images"); await app.flush();
    assert.equal(app.prompts.length, 0);
    assert.equal(app.uploadOptionCalls[0].args.category, "Elektro");
});
test("only the Project form sidebar collapses and its session choice survives refresh", async () => {
    const app = await setup({withSidebar: true});
    app.toolbarButtons.set("Send to HERO", {action() {}});
    const button = app.toolbarButtons.get("Project sidebar");
    assert.equal(button.element.getAttribute("aria-expanded"), "true");
    button.action();
    assert.ok(app.pageRoot.classList.contains("hp-project-sidebar-collapsed"));
    assert.equal(button.element.getAttribute("aria-expanded"), "false");
    assert.ok(!app.workspaceNavigation.className);
    app.controller.refresh(app.frm); await app.flush();
    assert.ok(app.pageRoot.classList.contains("hp-project-sidebar-collapsed"));
    assert.ok(app.toolbarButtons.has("Send to HERO"));
    app.sidebar.hidden = true;
    app.toolbarButtons.get("Project sidebar").action();
    assert.ok(!app.pageRoot.classList.contains("hp-project-sidebar-collapsed"));
    assert.equal(app.sidebar.hidden, true);
    assert.equal(app.native.saves, 0);
});
test("Native design skips custom views/styles but retains mandatory category upload and native responsibility", async () => {
    const app = await setup({designMode: "Native", withSidebar: true, responsibleField: true});
    assert.equal(app.panels().size, 0);
    assert.equal(app.document.head.children.length, 0);
    assert.equal(app.calls.length, 0);
    assert.equal(app.wrapper.querySelectorAll("native-control").length, 7);
    assert.ok(!app.toolbarButtons.has("Project sidebar"));
    assert.equal(app.native.responsibleQuery.fieldname, "custom_project_responsible");
    app.toolbarButtons.get("Upload project images").action();
    assert.equal(app.prompts.at(-1).fields[0].reqd, 1);
    await app.prompts.at(-1).callback({category: "Dach"}); await app.flush();
    assert.equal(app.imageUploaders.length, 1);
    assert.equal(app.native.saves, 0);
});
test("tasks use native creation, forms and list; board is explicitly page-scoped", async () => {
    const app = await setup({handler: (request, data) => ({...data, rows: request.args.section === "tasks"
        ? [{name: "TASK-1", subject: "Example task", status: "Working", _assign: '["worker@example.invalid"]'}] : []})});
    const panel = await app.switchTab("tasks");
    app.click(panel, "New Task"); assert.equal(app.creates[0][0], "Task");
    assert.equal(app.creates[0][1].project, "PROJECT-1");
    app.click(panel, "Example task"); assert.deepEqual(app.routes.at(-1), ["Form", "Task", "TASK-1"]);
    app.click(panel, "Board"); assert.match(panel.textContent, /one page/); assert.match(panel.textContent, /worker@example.invalid/);
});
test("task search status and pagination go through the constrained read API", async () => {
    const app = await setup({handler: (request, data) => ({...data, next_start: 20})});
    const panel = await app.switchTab("tasks"), form = panel.querySelector("form");
    form.querySelector("input").value = "Synthetic";
    form.dispatch("submit"); await app.flush();
    assert.equal(app.calls.at(-1).args.search, "Synthetic"); assert.equal(app.calls.at(-1).args.start, 0);
    const select = form.querySelector("select"); select.value = "Working"; select.dispatch("change"); await app.flush();
    assert.equal(app.calls.at(-1).args.status, "Working");
    app.click(panel, "Next"); await app.flush(); assert.equal(app.calls.at(-1).args.start, 20);
    app.click(panel, "Previous"); await app.flush(); assert.equal(app.calls.at(-1).args.start, 0);
});
test("unsafe attachment URLs and active formats are not embedded", async () => {
    for (const [url, type] of [["javascript:alert(1)", "PDF"], ["https://external.invalid/a.png", "PNG"],
        ["/files/%2e%2e/secret", "PDF"], ["/files/x.svg", "SVG"], ["/files/x.html", "HTML"]]) {
        const app = await setup({handler: (request, data) => ({...data, rows: [{name: "F1", file_name: "Example", file_url: url, file_type: type}]})});
        const panel = await app.switchTab("documents");
        assert.equal(panel.querySelectorAll("iframe").length, 0); assert.equal(panel.querySelectorAll("img").length, 0);
        assert.match(panel.textContent, /Preview unavailable/);
    }
});
test("PDF pages render as images without an iframe, object, embed or browser PDF plug-in", async () => {
    const app = await setup({handler: (request, data) => ({...data, rows: [
        {name: "F1", file_name: "<img onerror=alert(1)>", file_url: "/private/files/safe.pdf", file_type: "PDF", is_private: 1, modified: "v1"},
    ]})});
    const panel = await app.switchTab("documents");
    for (const tag of ["iframe", "object", "embed"]) assert.equal(panel.querySelectorAll(tag).length, 0);
    assert.ok(panel.querySelector("img").src.startsWith("data:image/png;base64,"));
    assert.equal(app.pdfCalls[0].type, "GET");
    assert.equal(app.pdfCalls[0].args.expected_modified, "v1");
    assert.match(panel.textContent, /Page 1 of 2/);
    app.click(panel, "Next page"); await app.flush();
    assert.equal(app.pdfCalls.at(-1).args.page, 2);
    assert.match(panel.textContent, /Page 2 of 2/);
    assert.equal(panel.querySelectorAll("button").find(button => button.textContent === "Next page").disabled, true);
    assert.equal(panel.querySelector("a").rel, "noopener noreferrer");
    assert.equal(panel.querySelectorAll("img").length, 1);
    app.click(panel, "Enlarge"); await app.flush(); assert.equal(app.dialogs[0].shown, true);
    assert.ok(app.dialogs[0].fields_dict.preview.$wrapper[0].querySelector("img"));
});
test("PDF rendering failures retain original-file access and cannot inject an active format", async () => {
    for (const pdfHandler of [
        () => { throw new Error("private parser diagnostic"); },
        () => ({page: 1, page_count: 2, data_url: "data:text/html,<script>bad()</script>"}),
    ]) {
        const app = await setup({pdfHandler, handler: (request, data) => ({...data, rows: [{
            name: "F1", file_name: "Synthetic PDF", file_url: "/private/files/safe.pdf", file_type: "PDF", modified: "v1",
        }]})});
        const panel = await app.switchTab("documents");
        assert.match(panel.textContent, /PDF preview unavailable/);
        assert.equal(panel.querySelectorAll("img").length, 0);
        assert.equal(panel.querySelector("a").href, "https://example.invalid/private/files/safe.pdf");
        assert.ok(!panel.textContent.includes("private parser diagnostic"));
    }
});
test("a late PDF page cannot replace a newly selected document", async () => {
    const waiting = deferred();
    const app = await setup({pdfHandler: () => waiting.promise,
        handler: (request, data) => ({...data, rows: [
            {name: "F1", file_name: "First PDF", file_url: "/private/files/safe.pdf", file_type: "PDF", modified: "v1"},
            {name: "F2", file_name: "Second text", file_url: "/files/text.txt", file_type: "TXT"},
        ]})});
    const panel = await app.switchTab("documents");
    app.click(panel, "Second text");
    waiting.resolve({page: 1, page_count: 1, data_url: "data:image/png;base64,iVBORw0KGgo="}); await app.flush();
    assert.equal(panel.querySelectorAll("img").length, 0);
    assert.equal(panel.querySelectorAll("h4")[0].textContent, "Second text");
});
test("appointments disclose grid and recurrence limits; native Event forms remain available", async () => {
    const app = await setup({handler: (request, data) => ({...data, rows: request.args.section === "appointments" ? [
        {name: "EV1", subject: "Synthetic appointment", starts_on: "2026-11-16 08:00:00", ends_on: "2026-11-16 09:00:00",
            participants: [{doctype: "User", name: "worker@example.invalid"}]},
    ] : []})});
    const panel = await app.switchTab("appointments");
    assert.match(panel.textContent, /not employee availability/); assert.match(panel.textContent, /Recurring appointments/);
    assert.match(panel.textContent, /2026-11-16/);
    app.click(panel, "Synthetic appointment"); app.click(panel, "Open appointment");
    assert.deepEqual(app.routes.at(-1), ["Form", "Event", "EV1"]);
    app.click(panel, "New Event");
    assert.equal(app.creates.at(-1)[0], "Event");
    assert.equal(app.creates.at(-1)[1].reference_doctype, "Project");
    assert.equal(app.creates.at(-1)[1].reference_docname, "PROJECT-1");
    app.click(panel, "Next week"); assert.match(panel.textContent, /2026-11-23/);
});
test("refresh preserves native values without recreating a duplicate project title card", async () => {
    const app = await setup();
    app.frm.doc.project_name = "Edited native title";
    app.frm.doc.status = "On hold";
    app.controller.refresh(app.frm); await app.flush();
    assert.equal(app.frm.doc.project_name, "Edited native title");
    assert.equal(app.frm.doc.status, "On hold");
    assert.equal(app.wrapper.querySelectorAll("h2").length, 0);
    assert.equal(app.native.saves, 0);
});
test("direct assignments stay separate from membership and use the native assignment action", async () => {
    const app = await setup({handler: (request, data) => ({...data, rows: request.args.section === "assignments" ? [
        {name: "TODO-1", description: "<b>Do this</b><script>bad()</script>", allocated_to: "worker@example.invalid"},
    ] : []})});
    const panel = await app.switchTab("assignments");
    assert.match(panel.textContent, /membership is edited below/);
    assert.match(panel.textContent, /Do this/); assert.ok(!panel.textContent.includes("bad()"));
    app.click(panel, "Assign"); assert.equal(app.native.assigns, 1);
});
test("German labels do not translate record titles or stored Task status codes", async () => {
    const messages = {Appointments: "Termine", Open: "Offen", "All statuses": "Alle Status"};
    const app = await setup({translate: value => messages[value] || value,
        handler: (request, data) => ({...data, rows: request.args.section === "tasks"
            ? [{name: "TASK-1", subject: "Open", status: "Open"}] : []})});
    assert.equal(app.panels().get("appointments").querySelector("h3").textContent, "Termine");
    const panel = await app.switchTab("tasks");
    assert.ok(panel.querySelectorAll("button").some(button => button.textContent === "Open"));
    assert.ok(panel.querySelectorAll("span").some(span => span.textContent === "Offen"));
    const select = panel.querySelector("select");
    assert.equal(select.children.find(option => option.value === "Open").textContent, "Offen");
});
test("only administrators see category management, while readers can filter classifications", async () => {
    const admin = await setup({isAdmin: true});
    admin.click(await admin.switchTab("images"), "Manage image categories");
    assert.deepEqual(admin.routes.at(-1), ["List", "Project Image Category"]);
    const reader = await setup({canWrite: false});
    const panel = await reader.switchTab("images");
    assert.ok(!panel.querySelectorAll("button").some(button => button.textContent === "Manage image categories"));
    assert.ok(!panel.querySelectorAll("button").some(button => button.textContent === "Uncategorized"));
    reader.click(panel, "Filter by category");
    await reader.prompts.at(-1).callback({category: "Elektro"}); await reader.flush();
    assert.equal(reader.calls.at(-1).args.category, "Elektro");
    assert.equal(reader.calls.at(-1).args.uncategorized, 0);
    reader.click(panel, "All images"); await reader.flush();
    assert.equal(reader.calls.at(-1).args.category, "");
});
test("document upload opens the native uploader and retains attachment quota", async () => {
    const app = await setup();
    const panel = await app.switchTab("documents");
    app.click(panel, "Upload document");
    assert.equal(app.native.uploads, 1);
    assert.equal(app.native.limits, 1);
    assert.equal(app.native.saves, 0);
    const limited = await setup({maxReached: true});
    limited.click(await limited.switchTab("documents"), "Upload document");
    assert.equal(limited.native.uploads, 0);
});
test("image categories group native files and classification is one explicit guarded POST", async () => {
    const waiting = deferred();
    const app = await setup({handler: (request, data) => ({...data, rows: request.args.section === "images" ? [
        {name: "IMG1", file_name: "Synthetic image", file_type: "PNG", category: "Dach", modified: "v1"}] : []}),
        categoryHandler: () => waiting.promise});
    const panel = await app.switchTab("images");
    assert.match(panel.textContent, /Dach/);
    assert.equal(app.categoryCalls.length, 0);
    app.click(panel, "Set image category");
    const prompt = app.prompts.at(-1);
    assert.equal(prompt.fields[0].options, "Project Image Category");
    const first = prompt.callback({category: "Elektro"});
    await prompt.callback({category: "Elektro"});
    assert.equal(app.categoryCalls.length, 1);
    const request = app.categoryCalls[0];
    assert.equal(request.type, "POST"); assert.equal(request.args.expected_modified, "v1");
    assert.equal(request.args.project, "PROJECT-1"); assert.equal(request.args.file, "IMG1");
    waiting.resolve({file: "IMG1", category: "Elektro"}); await first; await app.flush();
    assert.equal(app.native.saves, 0);
});
test("site address is displayed as text independently from customer data", async () => {
    const app = await setup({addressHandler: () =>
        ({project: "PROJECT-1", state: "ready", address: "<img src=x onerror=alert(1)> HERO site"})});
    const panel = app.panels().get("details");
    assert.match(panel.textContent, /Project site address/);
    assert.match(panel.textContent, /HERO site/); assert.equal(panel.querySelectorAll("img").length, 0);
    assert.equal(app.addressCalls[0].type, "GET");
    assert.ok(app.addressCalls[0].method.endsWith("project_address.get_address_context"));
});
test("details contain only project address left and primary contact right, never the redundant customer card", async () => {
    const app = await setup({handler: (request, data) => ({...data, context: {
        customer: {name: "CUSTOMER-1", customer_name: "Redundant customer card"},
        address: {address_line1: "Customer address must not be displayed"},
        contact: {name: "CONTACT-1", first_name: "<img src=x onerror=alert(1)>", email_id: "example@example.invalid"},
    }})});
    const panel = app.panels().get("details"), grid = panel.querySelector(".hp-context-grid");
    assert.equal(grid.children.length, 2);
    assert.equal(grid.children[0].dataset.projectContext, "address");
    assert.equal(grid.children[1].dataset.projectContext, "contact");
    assert.ok(!panel.textContent.includes("Redundant customer card"));
    assert.ok(!panel.textContent.includes("Customer address must not be displayed"));
    assert.equal(panel.querySelectorAll("img").length, 0);
    assert.match(panel.textContent, /primary address unless Edit project address/);
});
test("local project responsibility uses the native User Link and never auto-saves or writes HERO", async () => {
    const app = await setup({responsibleField: true});
    assert.equal(app.native.responsibleQuery.fieldname, "custom_project_responsible");
    const filters = app.native.responsibleQuery.query().filters;
    assert.equal(filters.enabled, 1); assert.equal(filters.user_type, "System User");
    const panel = await app.switchTab("assignments");
    assert.match(panel.textContent, /ERPNext project responsible/);
    assert.equal(app.native.saves, 0); assert.equal(app.categoryCalls.length, 0);
    assert.ok(app.calls.every(call => call.type === "GET"));
});
test("missing local address explains the default/manual choice and failures keep native fields", async () => {
    const app = await setup({addressHandler: () => ({project: "PROJECT-1", state: "missing", address: null})});
    assert.match(app.panels().get("details").textContent, /Choose a customer with a primary address/);
    assert.equal(app.wrapper.querySelectorAll("native-control").length, 7);
    const failed = await setup({addressHandler: () => { throw new Error("upstream secret"); }});
    assert.match(failed.panels().get("details").textContent, /unavailable or you do not have permission/);
    assert.ok(!failed.panels().get("details").textContent.includes("upstream secret"));
});
