'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

for (const language of input.languages) {
    const messages = input.catalogs[language];
    const window = {};
    const frappe = {_messages: messages, boot: {lang: language, user: {roles: []}}};
    const context = vm.createContext({window, frappe, URLSearchParams, console});
    vm.runInContext(fs.readFileSync(input.frappeTranslate, 'utf8'), context);
    context.__ = window.__;
    for (const file of ['hero_workspace.js', 'hero_tools.js', 'hero_settings.js', 'hero_diagnostics.js', 'hero_migration.js']) {
        vm.runInContext(fs.readFileSync(input.jsDir + '/' + file, 'utf8'), context, {filename: file});
    }
    const expectedTitle = language === 'en' ? 'HERO Settings' : 'HERO-Einstellungen';
    assert.equal(window.hero_integration_i18n.text('HERO Settings'), expectedTitle);
    const expectedError = language === 'en'
        ? 'Line 2: variable "id" is defined more than once.'
        : 'Zeile 2: Die Variable „id“ ist mehrfach definiert.';
    assert.throws(() => window.hero_workspace_tools.graphqlText.parseVariablesText('id := 1\nid := 2'),
        error => error.message === expectedError);
    const summary = window.hero_integration_settings_ui.packagePreviewSummary({snapshot: 'New',
        mapping_package: {Changed: 2}, read_services: [], settings: []});
    assert.ok(summary.includes(language === 'en' ? 'New' : 'Neu'));
    assert.ok(summary.includes(language === 'en' ? 'Changed 2' : 'Geändert 2'));
    const migration = window.hero_integration_migration;
    const fieldConflict = 'Field conflict requires review: subject.';
    assert.equal(migration.diagnostic(fieldConflict), language === 'en' ? fieldConflict : 'Feldkonflikt erfordert Prüfung: subject.');
    assert.equal(migration.phaseSummary('Running', 12), language === 'en' ? 'Running · 12 processed' : 'Läuft · 12 verarbeitet');
    const drift = "HERO customer number changed from 'old' to 'new'. Customer CUST-42 keeps its name and number until reviewed; set custom_hero_customer_number to 'new' to accept the new number.";
    const expectedDrift = language === 'en' ? drift : "HERO-Kundennummer wurde von 'old' auf 'new' geändert. Kunde CUST-42 behält seinen Namen und seine Nummer bis zur Prüfung; setzen Sie custom_hero_customer_number auf 'new', um die neue Nummer zu übernehmen.";
    assert.equal(migration.diagnostic(drift), expectedDrift);
    const collision = "Project 'P-42' already exists without this HERO record's link. The HERO number is not reassigned, suffixed or merged; check whether 'P-42' is the same project and link or rename it.";
    const expectedCollision = language === 'en' ? collision : "Projekt 'P-42' existiert bereits ohne Link zu diesem HERO-Datensatz. Die HERO-Nummer wird weder neu vergeben noch mit einem Zusatz versehen oder zusammengeführt; prüfen Sie, ob 'P-42' dasselbe Objekt (Projekt) ist, und verknüpfen oder benennen Sie es um.";
    assert.equal(migration.diagnostic(collision), expectedCollision);
    const unlinked = 'Existing Customer has no stable HERO link.';
    assert.equal(migration.diagnostic(unlinked), language === 'en' ? unlinked : 'Für das vorhandene Objekt Kunde gibt es keinen stabilen HERO-Link.');
    const longTitle = 'Generated Project.project_name exceeds its 140-character field limit.';
    assert.equal(migration.diagnostic(longTitle), language === 'en' ? longTitle : 'Der erzeugte Project.project_name überschreitet die Feldgrenze von 140 Zeichen.');
    const address = 'The project address is missing or incomplete (ProjectMatch.address.city is required for the ERPNext project title.); the title omits it.';
    const expectedAddress = language === 'en' ? address : 'Die Projektadresse fehlt oder ist unvollständig (ProjectMatch.address.city ist für den ERPNext-Projekttitel erforderlich.); sie wird im Titel ausgelassen.';
    assert.equal(migration.diagnostic(address), expectedAddress);
    const customerReason = 'The HERO customer is deleted in HERO; the Project has no customer.';
    const combinedReview = `${customerReason} ${address}`;
    const expectedReview = language === 'en' ? combinedReview : `Der HERO-Kunde ist in HERO gelöscht; dem Projekt ist kein Kunde zugeordnet. ${expectedAddress}`;
    assert.equal(migration.diagnostic(combinedReview), expectedReview);
    const pending = `Recheck requested: Awaits administrator approval: ${address}`;
    const expectedPending = language === 'en' ? pending : `Erneute Prüfung angefordert: Wartet auf Freigabe durch einen Administrator: ${expectedAddress}`;
    assert.equal(migration.diagnostic(pending), expectedPending);
    const trade = "Project trade 'NEW' has no confirmed HERO-to-ERPNext title mapping.";
    assert.equal(input.tradeError, trade);
    assert.equal(migration.diagnostic(trade), language === 'en' ? trade : "Für das Projektgewerk 'NEW' gibt es kein bestätigtes HERO-zu-ERPNext-Titel-Mapping.");
    const notes = "Target created through native Frappe validation. The HERO email address is not valid in ERPNext; it was not imported. HERO number 'bad/123' cannot be an ERPNext document name (only letters, digits and single inner hyphens, at most 140 characters). A collision-safe internal ID names the document; no replacement business number was generated. The original HERO value stays in its field.";
    assert.ok(notes.includes(input.numberError));
    const expectedNotes = language === 'en' ? notes : "Ziel durch die reguläre Frappe-Validierung erstellt. Die HERO-E-Mail-Adresse ist in ERPNext ungültig; sie wurde nicht importiert. Die HERO-Nummer 'bad/123' kann kein ERPNext-Dokumentname sein (nur Buchstaben, Ziffern und einzelne Bindestriche innerhalb des Namens, höchstens 140 Zeichen). Das Dokument erhält eine kollisionssichere interne ID; es wurde keine Ersatzgeschäftsnummer erzeugt. Der ursprüngliche HERO-Wert bleibt in seinem Feld.";
    assert.equal(migration.diagnostic(notes), expectedNotes);
    const preview = "A new target record would be created named 'PV-123-H'.";
    assert.equal(migration.diagnostic(preview), language === 'en' ? preview : "Ein neuer Zieldatensatz mit dem Namen 'PV-123-H' würde erstellt.");
    const rejection = 'ERPNext rejected the record (ValidationError); see Error Log.';
    assert.equal(migration.diagnostic(rejection), language === 'en' ? rejection : 'ERPNext hat den Datensatz abgelehnt (ValidationError); siehe Fehlerprotokoll.');
    const unavailable = 'Customer is not installed on this site.';
    assert.equal(migration.diagnostic(unavailable), language === 'en' ? unavailable : 'Kunde ist auf dieser Site nicht installiert.');
    const addressConflict = 'HERO customer address changed outside the selected line_2 binding.';
    assert.equal(migration.diagnostic(addressConflict), language === 'en' ? addressConflict : 'Die HERO-Kundenadresse wurde außerhalb der ausgewählten line_2-Zuordnung geändert.');
    const phoneConflict = 'The Customer primary Contact is shared or not linked to this Customer.';
    assert.equal(migration.diagnostic(phoneConflict), language === 'en' ? phoneConflict : 'Der Hauptkontakt des Kunden wird gemeinsam verwendet oder ist nicht mit diesem Kunden verknüpft.');
    const deletedProject = 'The referenced HERO project is deleted; the record is not imported.';
    assert.equal(migration.diagnostic(deletedProject), language === 'en' ? deletedProject : 'Das referenzierte HERO-Projekt ist gelöscht; der Datensatz wird nicht importiert.');
    const deletedCustomer = 'Source customer is deleted; no ERPNext target deletion was performed.';
    assert.equal(migration.diagnostic(deletedCustomer), language === 'en' ? deletedCustomer : 'Der Quellkunde ist gelöscht; das ERPNext-Ziel wurde nicht gelöscht.');
    const graphSummary = '12 graph nodes and 34 graph edges.';
    assert.equal(migration.diagnostic(graphSummary), language === 'en' ? graphSummary : '12 Graphknoten und 34 Graphkanten.');
    const refreshStatus = 'Schema unchanged; the current snapshot stays in use.';
    assert.equal(migration.diagnostic(refreshStatus), language === 'en' ? refreshStatus : 'Schema unverändert; der aktuelle Snapshot bleibt in Verwendung.');
    const expectedSuggestion = language === 'en' ? input.suggestionReason : 'Typkompatible Übereinstimmung von Name und Bezeichnung. Relation: Customer. Select-Werte müssen vor der Aktivierung geprüft werden. Erforderliches Zielfeld; die Zielerstellung benötigt einen zur App gehörenden Vertrag. Für dieses nicht kanonische App-Ziel ist nur ein Entwurf möglich.';
    assert.equal(migration.diagnostic(input.suggestionReason), expectedSuggestion);
    assert.equal(migration.diagnostic('User supplied custom reason.'), 'User supplied custom reason.');
    const base = 'Target created through native Frappe validation.';
    const optionalNotes = [
        'The HERO email address is not valid in ERPNext; it was not imported.',
        'A HERO phone text is not a valid ERPNext number; the first number was imported and the full text kept as a comment.',
    ];
    const addresses = ['', 'Customer primary address is incomplete. It was not imported.',
        'The HERO address has no country; Germany was assumed.'];
    for (let mask = 0; mask < 4; mask++) {
        for (const addressNote of addresses) {
            const parts = [base, ...optionalNotes.filter((_, index) => mask & (1 << index)), ...(addressNote ? [addressNote] : [])];
            const expected = parts.map(part => messages[part] || part).join(' ');
            assert.equal(migration.diagnostic(parts.join(' ')), expected);
        }
    }
}

// The editable record uses the real Frappe translator, formatter and registry.
for (const language of ['en', 'de']) {
    const window = {};
    const frappe = {_messages: input.catalogs[language], ui: {form: {}}, provide: () => { frappe.ui.form.handlers ||= {}; }};
    const $ = {isPlainObject: value => value !== null && typeof value === 'object'};
    const document = {createElement: () => ({textContent: '', hidden: false})};
    const context = vm.createContext({window, frappe, $, jQuery: $, document, cur_frm: null});
    vm.runInContext(fs.readFileSync(input.frappeFormat, 'utf8'), context);
    vm.runInContext(fs.readFileSync(input.frappeTranslate, 'utf8'), context);
    context.__ = window.__;
    vm.runInContext(fs.readFileSync(input.frappeFormScripts, 'utf8'), context);
    vm.runInContext(fs.readFileSync(input.jsDir + '/hero_diagnostics.js', 'utf8'), context);
    vm.runInContext(fs.readFileSync(input.recordForm, 'utf8'), context);
    const detail = 'Field conflict requires review: customer_name.';
    const children = [];
    const field = {$wrapper: [{appendChild: child => children.push(child)}]};
    const frm = {doc: {detail}, fields_dict: {detail: field}};
    frappe.ui.form.get_event_handler_list('HERO Integration Record', 'refresh')[0](frm);
    assert.equal(children.length, 1);
    assert.equal(children[0].hidden, language === 'en');
    if (language === 'de') assert.equal(children[0].textContent, 'Übersetzung: Feldkonflikt erfordert Prüfung: customer_name.');
    assert.equal(frm.doc.detail, detail);
}

// Exercise the actual Frappe event registry across two native DocTypes.
{
    const window = {__: value => value === 'Record is already synchronized.' ? 'Der Datensatz ist bereits synchronisiert.' : value};
    const frappe = {ui: {form: {}}, provide: () => { frappe.ui.form.handlers ||= {}; }};
    const $ = {isPlainObject: value => value !== null && typeof value === 'object'};
    const context = vm.createContext({window, frappe, $, cur_frm: null});
    vm.runInContext(fs.readFileSync(input.frappeFormScripts, 'utf8'), context);
    vm.runInContext(fs.readFileSync(input.jsDir + '/hero_diagnostics.js', 'utf8'), context);
    const native = fs.readFileSync(input.jsDir + '/hero_native_diagnostics.js', 'utf8');
    vm.runInContext(native, context);
    vm.runInContext(native, context);
    for (const [doctype, fieldname] of [['HERO Integration Run', 'error_message'], ['HERO Mutation Audit', 'detail']]) {
        const handlers = frappe.ui.form.get_event_handler_list(doctype, 'refresh');
        assert.equal(handlers.length, 1);
        const value = 'Record is already synchronized.';
        const display = {textContent: value};
        const field = {value, disp_area: display, set_disp_area(text) { this.disp_area.textContent = text; }};
        const frm = {doc: {[fieldname]: value}, fields_dict: {[fieldname]: field}};
        handlers[0](frm);
        assert.equal(display.textContent, 'Der Datensatz ist bereits synchronisiert.');
        field.set_disp_area(value);
        assert.equal(display.textContent, 'Der Datensatz ist bereits synchronisiert.');
        assert.equal(frm.doc[fieldname], value);
    }
}
