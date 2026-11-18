// Enhance native Project tabs without replacing fields, saving or HERO actions.
(() => {
    "use strict";
    const owners = new WeakMap();
    const sidebarPreferences = new WeakMap();
    let sidebarSequence = 0;
    const tabs = [
        ["__details", "details", "Details"],
        ["custom_project_documents_tab", "documents", "Documents"],
        ["custom_project_images_tab", "images", "Images"],
        ["more_info_tab", "appointments", "Appointments"],
        ["connections_tab", "tasks", "Tasks"],
        ["monitor_progress_tab", "assignments", "Assignments"],
        ["costing_tab", "advanced", "Advanced"],
    ];
    const statuses = ["Open", "Working", "Pending Review", "Overdue", "Template", "Completed", "Cancelled"];
    const imageTypes = new Set(["JPG", "JPEG", "PNG", "GIF", "WEBP", "BMP", "AVIF"]);
    const node = (tag, text, className) => {
        const element = document.createElement(tag);
        if (text != null) element.textContent = String(text);
        if (className) element.className = className;
        return element;
    };
    const button = (text, action, primary = false, translate = true) => {
        const element = node("button", translate ? __(text) : text, "btn btn-sm " + (primary ? "btn-primary" : "btn-default"));
        element.type = "button";
        element.addEventListener("click", action);
        return element;
    };
    const note = text => node("p", __(text), "hp-note");
    const date = value => value ? frappe.datetime.str_to_user(String(value)) : "—";
    const plain = value => {
        const parsed = new DOMParser().parseFromString(String(value || ""), "text/html");
        for (const element of parsed.querySelectorAll("script,style,template")) element.remove();
        return parsed.body.textContent || "";
    };
    const assignees = value => {
        try { const result = JSON.parse(value || "[]"); return Array.isArray(result) ? result.join(", ") : "—"; }
        catch (_) { return "—"; }
    };
    const badge = value => {
        const colors = {Open: "blue", Working: "green", Completed: "green", Overdue: "orange",
            Cancelled: "red", "On hold": "purple", "Pending Review": "purple", Closed: "gray"};
        return node("span", value ? __(value) : "—", "hp-badge hp-" + (colors[value] || "gray"));
    };
    function routeLink(doctype, name, label) {
        return button(label || name, () => frappe.set_route("Form", doctype, name), false, false);
    }
    function localFile(value) {
        if (typeof value !== "string" || !/^\/(?:private\/)?files\//.test(value)) return null;
        try {
            const decoded = decodeURIComponent(value);
            if (/[\u0000-\u001f\\]/.test(decoded) || decoded.split(/[/?#]/).some(part => part === "." || part === "..")) return null;
            const url = new URL(value, window.location.origin);
            return url.origin === window.location.origin ? url.href : null;
        } catch (_) { return null; }
    }
    function stylesheet() {
        const version = "project-usability-20261117";
        const existing = document.getElementById("hero-project-workspace-css");
        if (existing?.dataset.version === version) return;
        existing?.remove();
        const link = document.createElement("link");
        link.id = "hero-project-workspace-css";
        link.rel = "stylesheet";
        link.href = "/assets/hero_integration/css/project_workspace.css?v=" + version;
        link.dataset.version = version;
        // Native fields remain usable even if the optional stylesheet fails.
        link.addEventListener("error", () => { link.remove(); });
        document.head.append(link);
    }
    function table(headings, rows) {
        const wrap = node("div", null, "hp-table-wrap");
        wrap.tabIndex = 0;
        wrap.setAttribute("role", "region");
        wrap.setAttribute("aria-label", headings.map(value => __(value)).join(" · "));
        const grid = node("table", null, "hp-table");
        const head = node("tr");
        for (const heading of headings) {
            const cell = node("th", __(heading)); cell.scope = "col"; head.append(cell);
        }
        const thead = node("thead"); thead.append(head); grid.append(thead);
        const body = node("tbody");
        for (const cells of rows) {
            const row = node("tr");
            for (const value of cells) {
                const cell = node("td");
                cell.append(value instanceof HTMLElement ? value : node("span", value == null || value === "" ? "—" : value));
                row.append(cell);
            }
            body.append(row);
        }
        grid.append(body); wrap.append(grid);
        return wrap;
    }

    class Workspace {
        constructor(frm) {
            this.frm = frm;
            this.project = frm.doc.name;
            this.panels = new Map();
            this.sequence = new Map();
            this.roots = [];
            this.week = 0;
            this.mode = "list";
            this.pdfViews = new WeakMap();
            this.disposed = false;
        }
        mount() {
            stylesheet();
            this.wrapper = this.frm.layout.wrapper[0];
            this.wrapper.classList.add("hero-project-form");
            for (const [fieldname, key, title] of tabs) {
                const tab = (this.frm.layout.tabs || []).find(item => item.df.fieldname === fieldname);
                if (!tab?.wrapper?.[0]) continue;
                const root = node("section", null, "hp-panel form-dashboard-section");
                root.dataset.projectPanel = key;
                const heading = node("div", null, "hp-panel-heading");
                const text = node("div"); text.append(node("h3", __(title)));
                const actions = node("div", null, "hp-actions");
                const content = node("div", null, "hp-panel-content");
                content.setAttribute("aria-live", "polite");
                heading.append(text, actions); root.append(heading, content);
                if (key === "assignments") tab.wrapper[0].append(root);
                else tab.wrapper[0].prepend(root);
                this.roots.push(root);
                this.panels.set(key, {root, content, actions, start: 0, search: "", status: "", loaded: false, tab});
                if (key === "images") Object.assign(this.panels.get(key), {category: "", uncategorized: 0});
                const galleryName = {documents: "custom_project_documents_gallery", images: "custom_project_images_gallery"}[key];
                if (galleryName) this.panels.get(key).gallery = this.frm.fields_dict?.[galleryName]?.$wrapper?.[0];
                const link = tab.tab_link?.[0]?.querySelector(".nav-link");
                if (link) link.textContent = __(title);
                if (key === "advanced") this.advanced(content);
                else this.toolbar(key);
            }
            this.setupSidebar();
            this.openActive();
        }
        dispose() {
            this.disposed = true;
            for (const panel of this.panels.values()) panel.gallery?.classList.remove("hp-native-gallery-suppressed");
            for (const root of this.roots) root.remove();
            this.wrapper?.classList.remove("hero-project-form");
            this.sidebarRoot?.classList.remove("hp-project-page");
            this.sidebarRoot?.classList.remove("hp-project-sidebar-collapsed");
            if (this.sidebarRoot) this.frm.remove_custom_button?.(__("Project sidebar"));
            if (this.nativeUploadAction) this.frm.remove_custom_button?.(__("Upload project images"));
        }
        current() {
            const route = frappe.get_route?.();
            return !this.disposed && owners.get(this.frm) === this && this.frm.doc.name === this.project
                && (!route || route[0] === "Form" && route[1] === "Project" && route[2] === this.project);
        }
        setupSidebar() {
            const sidebar = this.frm.page?.sidebar?.[0], root = this.frm.page?.wrapper?.[0];
            if (!sidebar || !root || this.frm.is_new() || !this.frm.add_custom_button) return;
            this.sidebarRoot = root;
            root.classList.add("hp-project-page");
            if (!sidebar.id) sidebar.id = "hp-project-sidebar-" + (++sidebarSequence);
            const apply = collapsed => {
                if (collapsed) root.classList.add("hp-project-sidebar-collapsed");
                else root.classList.remove("hp-project-sidebar-collapsed");
                this.sidebarButton?.attr?.("aria-expanded", String(!collapsed));
            };
            this.frm.remove_custom_button?.(__("Project sidebar"));
            this.sidebarButton = this.frm.add_custom_button(__("Project sidebar"), () => {
                if (!this.current()) return;
                const collapsed = !sidebarPreferences.get(this.frm);
                sidebarPreferences.set(this.frm, collapsed);
                apply(collapsed);
            });
            this.sidebarButton?.attr?.("aria-controls", sidebar.id);
            apply(sidebarPreferences.get(this.frm) || false);
        }
        openActive() {
            const entry = [...this.panels.entries()].find(([, panel]) => panel.tab.is_active());
            if (entry && !entry[1].loaded && entry[0] !== "advanced") this.load(entry[0], 0);
        }
        toolbar(key) {
            const panel = this.panels.get(key);
            panel.actions.append(button("Refresh", () => this.load(key, 0)));
            if (["documents", "images"].includes(key) && this.frm.has_perm("write") && !this.frm.is_new()) {
                panel.actions.append(button(key === "images" ? "Add images" : "Upload document", () => {
                    if (!this.current()) return;
                    if (this.frm.attachments.max_reached(true)) return;
                    if (key === "images") this.uploadImages();
                    else this.frm.attachments.new_attachment();
                }, true));
            }
            if (key === "tasks") {
                panel.actions.append(button("Task list", () => frappe.set_route("List", "Task", {project: this.project})));
                if (frappe.model.can_create("Task")) panel.actions.append(button("New Task", () =>
                    frappe.new_doc("Task", {project: this.project}), true));
            }
            if (key === "images") {
                panel.actions.append(button("All images", () => {
                    panel.category = ""; panel.uncategorized = 0; this.load(key, 0);
                }), button("Filter by category", () => {
                    if (!this.current()) return;
                    frappe.prompt([{fieldtype: "Link", fieldname: "category", label: __("Project Image Category"),
                        options: "Project Image Category", reqd: 1}], values => {
                        if (!this.current()) return;
                        panel.category = values.category; panel.uncategorized = 0; this.load(key, 0);
                    }, __("Filter by category"), __("Apply"));
                }));
                if (frappe.user?.has_role?.("System Manager")) panel.actions.append(button("Manage image categories", () =>
                    frappe.set_route("List", "Project Image Category")));
            }
            if (key === "appointments") {
                panel.actions.append(button("Calendar", () => frappe.set_route("List", "Event", "Calendar")));
                if (frappe.model.can_create("Event")) panel.actions.append(button("New Event", () =>
                    frappe.new_doc("Event", {reference_doctype: "Project", reference_docname: this.project}), true));
            }
            if (key === "assignments" && this.frm.has_perm("write")) {
                panel.actions.append(button("Assign", () => this.frm.assign_to.add()));
            }
            if (key === "details") return;
            const filter = node("form", null, "hp-filters");
            const input = node("input", null, "form-control");
            input.type = "search"; input.placeholder = __("Search");
            input.setAttribute("aria-label", __("Search") + ": " + panel.root.querySelector("h3").textContent);
            input.maxLength = 120;
            filter.append(input);
            let select;
            if (key === "tasks") {
                select = node("select", null, "form-control");
                select.setAttribute("aria-label", __("Status"));
                for (const value of ["", ...statuses]) {
                    const option = node("option", value ? __(value) : __("All statuses"));
                    option.value = value; select.append(option);
                }
                select.addEventListener("change", () => { panel.status = select.value; this.load(key, 0); });
                filter.append(select);
                panel.actions.append(button("List", () => { this.mode = "list"; this.renderRows(key, panel.data); }),
                    button("Board", () => { this.mode = "board"; this.renderRows(key, panel.data); }));
            }
            const submit = button("Search", () => {});
            submit.type = "submit";
            filter.append(submit);
            filter.addEventListener("submit", event => {
                event.preventDefault(); panel.search = input.value; this.load(key, 0);
            });
            panel.root.insertBefore(filter, panel.content);
        }
        uploadImages(selectedCategory = "") {
            if (!this.current() || this.frm.is_new() || !this.frm.has_perm("write")) return;
            const start = async category => {
                if (!this.current() || !category) return;
                try {
                    const response = await frappe.call({
                        method: "hero_integration.services.project_image_categories.get_upload_options", type: "GET",
                        args: {project: this.project, category}, quiet: true,
                    });
                    if (!this.current()) return;
                    const data = response.message;
                    if (response.exc || data?.project !== this.project || data.category !== category || !data.folder) {
                        throw new Error("Invalid image upload options");
                    }
                    if (this.frm.attachments.max_reached(true)) return;
                    const extensions = [...imageTypes].map(value => "." + value.toLowerCase());
                    const global = frappe.sys_defaults?.allowed_file_extensions?.split("\n")
                        .map(value => "." + value.trim().replace(/^\./, "").toLowerCase()).filter(value => value !== ".");
                    const allowed = global?.length ? extensions.filter(value => global.includes(value)) : extensions;
                    if (!allowed.length) { frappe.msgprint(__("Image uploads are not enabled for this site.")); return; }
                    const limit = this.frm.meta?.max_attachments;
                    new frappe.ui.FileUploader({
                        doctype: "Project", docname: this.project, frm: this.frm, folder: data.folder,
                        fieldname: "custom_project_images_gallery", dialog_title: __("Upload project images"),
                        make_attachments_public: this.frm.meta?.make_attachments_public,
                        allow_web_link: false,
                        restrictions: {allowed_file_types: allowed,
                            max_number_of_files: limit ? limit - this.frm.attachments.get_attachments().length : undefined},
                        on_success: file => {
                            this.frm.attachments.attachment_uploaded(file);
                            if (this.current()) this.load("images", 0);
                        },
                    });
                } catch (_) {
                    if (this.current()) frappe.msgprint(__("The image uploader could not be opened. Refresh and choose an active category."));
                }
            };
            if (selectedCategory) return start(selectedCategory);
            frappe.prompt([{
                fieldname: "category", fieldtype: "Link", label: __("Project Image Category"),
                options: "Project Image Category", reqd: 1,
                get_query: () => ({filters: {disabled: 0}}),
            }], values => start(values.category), __("Choose image category"), __("Continue"));
        }
        async load(key, start) {
            const panel = this.panels.get(key);
            if (!panel || !this.current()) return;
            const token = (this.sequence.get(key) || 0) + 1; this.sequence.set(key, token);
            panel.start = start;
            panel.data = null;
            panel.content.replaceChildren(note("Loading project information…"));
            panel.content.setAttribute("aria-busy", "true");
            if (this.frm.is_new()) {
                panel.content.replaceChildren();
                if (key === "details") this.details(panel.content, {});
                panel.content.append(note("Save the project to load related records."));
                panel.content.setAttribute("aria-busy", "false");
                return;
            }
            try {
                const response = await frappe.call({
                    method: "hero_integration.services.project_workspace.get_panel", type: "GET",
                    args: {project: this.project, section: key, start, search: panel.search, status: panel.status,
                        category: panel.category || "", uncategorized: panel.uncategorized || 0},
                    quiet: true,
                });
                if (!this.current() || this.sequence.get(key) !== token) return;
                if (response.exc) throw new Error(__("Project information could not be loaded."));
                const data = response.message;
                if (data?.project !== this.project || data.section !== key || !Array.isArray(data.rows)
                    || !["ready", "permission_denied"].includes(data.state)) {
                    throw new Error(__("Invalid project information response."));
                }
                panel.loaded = true; panel.data = data;
                this.renderRows(key, data);
            } catch (_) {
                if (!this.current() || this.sequence.get(key) !== token) return;
                panel.loaded = false; panel.data = null;
                panel.gallery?.classList.remove("hp-native-gallery-suppressed");
                panel.content.replaceChildren(note("Project information could not be loaded."),
                    button("Try again", () => this.load(key, start)));
            } finally {
                if (this.current() && this.sequence.get(key) === token) panel.content.setAttribute("aria-busy", "false");
            }
        }
        renderRows(key, data) {
            const panel = this.panels.get(key);
            if (!panel || !data || !this.current()) return;
            const content = panel.content;
            content.replaceChildren();
            if (data.state === "permission_denied") {
                panel.gallery?.classList.remove("hp-native-gallery-suppressed");
                content.append(note("You do not have permission to view these records.")); return;
            }
            panel.gallery?.classList.add("hp-native-gallery-suppressed");
            if (this.frm.is_dirty() || String(data.modified) !== String(this.frm.doc.modified)) {
                content.append(note("Related records refer to the saved project. Save or refresh using the native form."));
            }
            if (key === "details") { this.details(content, data.context || {}); return; }
            if (!data.rows.length) content.append(note("No matching records on this page."));
            if (key === "documents") this.documents(content, data.rows);
            if (key === "images") {
                this.images(content, data.rows, data.categories || []);
                if (data.categories_truncated) content.append(note("More image categories are available through the category filter."));
            }
            if (key === "tasks") this.tasks(content, data.rows);
            if (key === "appointments") this.appointments(content, data.rows);
            if (key === "assignments") this.assignments(content, data.rows);
            const pager = node("div", null, "hp-pager");
            const previous = button("Previous", () => this.load(key, Math.max(0, panel.start - 20)));
            previous.disabled = !panel.start;
            const next = button("Next", () => this.load(key, data.next_start));
            next.disabled = !Number.isInteger(data.next_start);
            pager.append(previous, node("span", __("Page") + " " + (Math.floor(panel.start / 20) + 1)), next);
            content.append(pager);
        }
        details(content, context) {
            const grid = node("div", null, "hp-context-grid");
            const contact = node("article", null, "hp-card");
            contact.dataset.projectContext = "contact";
            contact.append(node("h4", __("Customer Primary Contact")));
            if (context.contact) {
                contact.append(routeLink("Contact", context.contact.name,
                    [context.contact.first_name, context.contact.last_name].filter(Boolean).join(" ") || context.contact.name));
                for (const key of ["email_id", "phone", "mobile_no"]) {
                    if (context.contact[key]) contact.append(node("p", context.contact[key]));
                }
            } else contact.append(note("No readable primary contact is linked."));
            const site = node("article", null, "hp-card");
            site.dataset.projectContext = "address";
            site.append(node("h4", __("Project site address")));
            const address = node("div");
            address.style.whiteSpace = "pre-line";
            site.append(address, note("Uses the customer's primary address unless Edit project address is selected. Changes are saved with the project."));
            grid.append(site, contact); content.append(grid);
            this.loadSiteAddress(address);
        }
        async loadSiteAddress(target) {
            if (frappe.hero_project_address?.render) {
                return frappe.hero_project_address.render(this.frm, target);
            }
            const token = (this.sequence.get("site_address") || 0) + 1;
            this.sequence.set("site_address", token);
            target.replaceChildren(note("Loading project site address…"));
            try {
                const response = await frappe.call({
                    method: "hero_integration.services.project_address.get_address_context", type: "GET",
                    args: {project: this.frm.is_new() ? null : this.project, customer: this.frm.doc.customer || null,
                        address: this.frm.doc.custom_project_address || null,
                        manual: Number(this.frm.doc.custom_edit_project_address) ? 1 : 0}, quiet: true,
                });
                if (!this.current() || this.sequence.get("site_address") !== token) return;
                const data = response.message;
                if (response.exc || !["ready", "missing"].includes(data?.state)) throw new Error("Invalid address response");
                if (data.state === "ready" && typeof data.address === "string") {
                    target.replaceChildren(node("p", data.address));
                } else {
                    target.replaceChildren(note("Choose a customer with a primary address, or select Edit project address."));
                }
            } catch (_) {
                if (this.current() && this.sequence.get("site_address") === token) {
                    target.replaceChildren(note("The project address is unavailable or you do not have permission to read it."),
                        button("Try again", () => this.loadSiteAddress(target)));
                }
            }
        }
        documentLinks(content) {
            const links = node("div", null, "hp-actions");
            for (const type of ["Sales Order", "Sales Invoice"]) {
                if (frappe.model.can_read(type)) links.append(button(type, () =>
                    frappe.set_route("List", type, {project: this.project})));
            }
            content.append(links);
        }
        documents(content, rows) {
            content.append(note("Attachments are separate from ERP records. No stored document categories are configured."));
            this.documentLinks(content);
            if (!rows.length) return;
            const layout = node("div", null, "hp-document-layout");
            const preview = node("aside", null, "hp-preview");
            preview.setAttribute("aria-label", __("Document preview"));
            const list = table(["Document", "Created", "Privacy"], rows.map(row => [
                button(row.file_name || row.name, event => {
                    this.preview(preview, row);
                    for (const item of list.querySelectorAll("button")) item.setAttribute("aria-pressed", "false");
                    event.currentTarget.setAttribute("aria-pressed", "true");
                }, false, false),
                date(row.creation), row.is_private ? __("Private") : __("Public"),
            ]));
            layout.append(list, preview); content.append(layout);
            list.querySelector("button")?.setAttribute("aria-pressed", "true");
            this.preview(preview, rows[0]);
        }
        preview(target, row, enlarged = false) {
            this.pdfViews.delete(target);
            target.replaceChildren(node("h4", row.file_name || row.name));
            const url = localFile(row.file_url);
            if (url && (imageTypes.has(String(row.file_type).toUpperCase()) || String(row.file_type).toUpperCase() === "PDF")) {
                const image = imageTypes.has(String(row.file_type).toUpperCase());
                if (image) {
                    const element = node("img", null, "hp-file-preview");
                    element.alt = row.file_name || row.name; element.loading = "lazy"; element.src = url;
                    element.addEventListener("error", () => {
                        element.remove(); target.append(note("Preview unavailable. Open the file instead."));
                    });
                    target.append(element);
                } else this.pdfPreview(target, row);
                const link = node("a", __("Open file"), "btn btn-default btn-sm");
                link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; target.append(link);
            } else target.append(note("Preview unavailable. Open the file instead."));
            target.append(routeLink("File", row.name, __("File details")));
            if (!enlarged && url) target.append(button("Enlarge", () => {
                const dialog = new frappe.ui.Dialog({
                    title: row.file_name || row.name, size: "extra-large",
                    fields: [{fieldtype: "HTML", fieldname: "preview"}],
                });
                const wrapper = dialog.fields_dict.preview.$wrapper[0];
                wrapper.classList.add("hp-dialog");
                this.preview(wrapper, row, true); dialog.show();
            }));
        }
        pdfPreview(target, row) {
            const controls = node("div", null, "hp-actions hp-pdf-controls");
            const area = node("div", null, "hp-pdf-page");
            area.setAttribute("aria-live", "polite");
            area.setAttribute("aria-label", __("PDF preview"));
            area.tabIndex = 0;
            const counter = node("span", __("PDF preview"));
            const state = {request: 0, page: 1};
            this.pdfViews.set(target, state);
            const fresh = () => this.current() && this.pdfViews.get(target) === state;
            const previous = button("Previous page", () => load(state.page - 1));
            const next = button("Next page", () => load(state.page + 1));
            const failure = page => {
                area.replaceChildren(note("PDF preview unavailable. Open the original file."),
                    button("Try again", () => load(page)));
                counter.textContent = __("PDF preview");
                previous.disabled = page <= 1; next.disabled = true;
                area.setAttribute("aria-busy", "false");
            };
            const load = async page => {
                if (!fresh()) return;
                const request = ++state.request;
                state.page = page;
                previous.disabled = next.disabled = true;
                area.setAttribute("aria-busy", "true");
                area.replaceChildren(note("Loading PDF preview…"));
                try {
                    const response = await frappe.call({
                        method: "hero_integration.services.project_pdf_preview.get_page", type: "GET",
                        args: {project: this.project, file: row.name, expected_modified: row.modified, page},
                        quiet: true,
                    });
                    if (!fresh() || state.request !== request || target.isConnected === false) return;
                    const data = response.message;
                    if (response.exc || data?.page !== page || !Number.isInteger(data.page_count)
                        || data.page_count < page || data.page_count > 200
                        || typeof data.data_url !== "string" || data.data_url.length > 6 * 1024 * 1024
                        || !/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(data.data_url)) {
                        throw new Error("Invalid PDF preview response");
                    }
                    const image = node("img", null, "hp-pdf-raster");
                    image.alt = __("PDF page {0}", [page]);
                    image.src = data.data_url;
                    image.addEventListener("error", () => {
                        if (fresh() && state.request === request) failure(page);
                    });
                    area.replaceChildren(image);
                    counter.textContent = __("Page {0} of {1}", [page, data.page_count]);
                    previous.disabled = page <= 1; next.disabled = page >= data.page_count;
                    area.setAttribute("aria-busy", "false");
                } catch (_) {
                    if (fresh() && state.request === request) failure(page);
                }
            };
            controls.append(previous, counter, next); target.append(controls, area);
            target.append(note("Read-only page image. Open the original for form fields and signature validation."));
            load(1);
        }
        images(content, rows, categories = []) {
            const panel = this.panels.get("images");
            content.append(note("Categories are managed by a System Manager. Existing images are not categorized automatically."));
            if (panel.category) content.append(node("p", __("Project Image Category") + ": " + panel.category));
            if (panel.uncategorized) content.append(node("p", __("Uncategorized")));
            const active = new Set(categories.filter(value => typeof value === "string"));
            const visible = panel.uncategorized ? [] : panel.category
                ? [...active].filter(value => value === panel.category) : [...active];
            const groups = new Map(visible.map(value => [value, []]));
            for (const row of rows) {
                const category = row.category || "";
                if (!groups.has(category)) groups.set(category, []);
                groups.get(category).push(row);
            }
            for (const [category, images] of groups) {
                const section = node("section", null, "hp-card hp-image-group");
                const heading = node("h4", category || __("Uncategorized"));
                heading.append(node("span", String(images.length), "hp-count"));
                section.append(heading);
                const grid = node("div", null, "hp-image-grid");
                for (const row of images) {
                    const card = node("article", null, "hp-card hp-image-card");
                    const tile = button(row.file_name || row.name, () => {
                        const dialog = new frappe.ui.Dialog({
                            title: row.file_name || row.name, size: "extra-large",
                            fields: [{fieldtype: "HTML", fieldname: "preview"}],
                        });
                        const wrapper = dialog.fields_dict.preview.$wrapper[0];
                        wrapper.classList.add("hp-dialog"); this.preview(wrapper, row, true); dialog.show();
                    }, false, false);
                    tile.classList.add("hp-image-tile");
                    const url = localFile(row.file_url);
                    if (url) {
                        const image = node("img"); image.src = url; image.alt = row.file_name || row.name; image.loading = "lazy";
                        image.addEventListener("error", () => { image.remove(); tile.append(note("Preview unavailable.")); });
                        tile.prepend(image);
                    }
                    tile.setAttribute("aria-label", __("Enlarge") + ": " + (row.file_name || row.name));
                    card.append(tile);
                    if (row.creation) card.append(node("p", date(row.creation), "hp-note"));
                    if (this.frm.has_perm("write")) card.append(button("Set image category", () => this.assignCategory(row)));
                    grid.append(card);
                }
                if (category && active.has(category) && this.frm.has_perm("write")) {
                    const add = button("Add images", () => this.uploadImages(category));
                    add.classList.add("hp-image-add");
                    add.setAttribute("aria-label", __("Add images to {0}", [category]));
                    grid.append(add);
                }
                section.append(grid); content.append(section);
            }
        }
        assignCategory(row) {
            if (!this.current()) return;
            let submitted = false;
            frappe.prompt([{
                fieldname: "category", fieldtype: "Link", label: __("Project Image Category"),
                options: "Project Image Category", default: row.category || "", reqd: 1,
                get_query: () => ({filters: {disabled: 0}}),
            }], async values => {
                if (!this.current() || submitted) return;
                submitted = true;
                try {
                    const response = await frappe.call({
                        method: "hero_integration.services.project_image_categories.set_image_category", type: "POST",
                        args: {project: this.project, file: row.name, category: values.category || "",
                            expected_modified: row.modified}, quiet: true,
                    });
                    if (!this.current()) return;
                    if (response.exc || response.message?.file !== row.name) throw new Error("Invalid category response");
                    this.load("images", this.panels.get("images").start);
                } catch (_) {
                    if (this.current()) frappe.msgprint(__("The image category could not be saved. Refresh the image list and try again."));
                }
            }, __("Set image category"), __("Save"));
        }
        tasks(content, rows) {
            content.append(note("This view shows one page. Use the native Task list for Gantt, full boards and dependency editing."));
            if (this.mode === "board") {
                const board = node("div", null, "hp-board");
                for (const status of statuses.filter(value => rows.some(row => row.status === value))) {
                    const column = node("section", null, "hp-board-column"); column.append(badge(status));
                    for (const row of rows.filter(row => row.status === status)) {
                        const card = node("article", null, "hp-card");
                        card.append(routeLink("Task", row.name, row.subject || row.name),
                            node("p", date(row.exp_end_date)), node("p", assignees(row._assign)));
                        column.append(card);
                    }
                    board.append(column);
                }
                content.append(board); return;
            }
            content.append(table(["Task", "Status", "Due Date", "Priority", "Assigned To", "Progress"], rows.map(row => [
                routeLink("Task", row.name, row.subject || row.name), badge(row.status),
                date(row.exp_end_date), row.priority ? __(row.priority) : "—", assignees(row._assign),
                row.progress == null ? "—" : String(row.progress) + "%",
            ])));
        }
        appointments(content, rows) {
            content.append(note("Only explicitly linked project appointments are shown. The week grid covers this page, not employee availability."));
            content.append(note("Recurring appointments show their stored start. Use the native Calendar for recurring occurrences."));
            const info = node("aside", null, "hp-card");
            info.append(note("Select an appointment for details."));
            content.append(table(["Appointment", "Start", "End", "Type", "Status"], rows.map(row => [
                button(row.subject || row.name, () => {
                    info.replaceChildren(node("h4", row.subject || row.name),
                        node("p", date(row.starts_on) + " – " + date(row.ends_on)),
                        node("p", (row.participants || []).map(person => person.name).join(", ") || "—"),
                        routeLink("Event", row.name, __("Open appointment")));
                }, false, false),
                date(row.starts_on), date(row.ends_on), row.event_category ? __(row.event_category) : "—", badge(row.status),
            ])));
            const controls = node("div", null, "hp-actions");
            controls.append(button("Previous week", () => { this.week--; this.renderRows("appointments", this.panels.get("appointments").data); }),
                button("This week", () => { this.week = 0; this.renderRows("appointments", this.panels.get("appointments").data); }),
                button("Next week", () => { this.week++; this.renderRows("appointments", this.panels.get("appointments").data); }));
            content.append(controls, this.weekGrid(rows), info);
        }
        weekGrid(rows) {
            const today = frappe.datetime.get_today();
            const monday = new Date(today + "T12:00:00Z");
            monday.setUTCDate(monday.getUTCDate() - (monday.getUTCDay() + 6) % 7 + this.week * 7);
            const days = Array.from({length: 7}, (_, index) => {
                const day = new Date(monday); day.setUTCDate(day.getUTCDate() + index);
                return day.toISOString().slice(0, 10);
            });
            const participants = [...new Set(rows.flatMap(row =>
                (row.participants || []).map(person => person.doctype + ": " + person.name)))];
            return table(["Participants", ...days.map(value => date(value))], participants.map(person => [
                person, ...days.map(day => {
                    const cell = node("div", null, "hp-week-cell");
                    for (const row of rows.filter(row => (row.participants || []).some(
                        item => item.doctype + ": " + item.name === person)
                        && String(row.starts_on).slice(0, 10) <= day
                        && String(row.ends_on || row.starts_on).slice(0, 10) >= day)) {
                        cell.append(routeLink("Event", row.name, row.subject || row.name));
                    }
                    return cell;
                }),
            ]));
        }
        assignments(content, rows) {
            if (this.frm.fields_dict?.custom_project_responsible) {
                content.append(note("Choose the ERPNext project responsible in the native field above and save the project. This does not change the project team, user permissions or HERO responsibility."));
            }
            content.append(note("Project team membership is edited below. These are direct Project assignments; Task assignments stay with their Tasks."));
            content.append(table(["Assignment", "Assigned To", "Due Date", "Status"], rows.map(row => [
                routeLink("ToDo", row.name, plain(row.description) || row.name),
                row.allocated_to, date(row.date), badge(row.status),
            ])));
        }
        advanced(content) {
            content.append(note("Costs, progress notifications and source information remain in the native fields below. Existing permissions and HERO confirmations are unchanged."));
            this.documentLinks(content);
            content.append(note("Further linked records and project actions remain available in the native dashboard and form menu."));
        }
    }

    frappe.ui.form.on("Project", {
        refresh(frm) {
            owners.get(frm)?.dispose();
            if (!frm.layout?.wrapper?.[0] || !frm.layout.tabs?.length) return;
            const workspace = new Workspace(frm); owners.set(frm, workspace);
            if (frappe.boot?.hero_design_mode === "Native") {
                document.getElementById("hero-project-workspace-css")?.remove();
                if (!frm.is_new() && frm.has_perm("write") && frm.add_custom_button) {
                    workspace.nativeUploadAction = true;
                    frm.remove_custom_button?.(__("Upload project images"));
                    frm.add_custom_button(__("Upload project images"), () => workspace.uploadImages());
                }
            } else workspace.mount();
            if (frm.fields_dict?.custom_project_responsible) {
                frm.set_query("custom_project_responsible", () => ({filters: {enabled: 1, user_type: "System User"}}));
            }
        },
        on_tab_change(frm) { owners.get(frm)?.openActive(); },
        customer(frm) {
            const workspace = owners.get(frm), panel = workspace?.panels.get("details");
            if (panel && frm.is_dirty()) {
                workspace.sequence.set("details", (workspace.sequence.get("details") || 0) + 1);
                panel.loaded = false;
                panel.content.replaceChildren();
                workspace.details(panel.content, {});
                panel.content.append(note("Save the project to refresh customer information."));
                panel.content.setAttribute("aria-busy", "false");
            }
        },
    });
})();
