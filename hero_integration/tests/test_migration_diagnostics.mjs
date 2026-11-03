import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const translations = {
  'Field conflict requires review: {0}.': 'Feldkonflikt erfordert Prüfung: {0}.',
  'Task fields require review: {0}': 'Task-Felder erfordern Prüfung: {0}',
  'Another HERO run is active: {0}.': 'Ein anderer HERO-Lauf ist aktiv: {0}.',
  'Recheck requested: {0}': 'Erneute Prüfung angefordert: {0}',
  'A known static error.': 'Ein bekannter Fehler.',
  'Automatic sync is blocked: {0}': 'Automatischer Sync blockiert: {0}',
  'A known reason.': 'Ein bekannter Grund.',
  "{0} {1} already exists without this HERO record's link. The HERO number is not reassigned, suffixed or merged; check whether {1} is the same {2} and link or rename it.": '{0} {1} existiert bereits; Nummer bleibt unverändert. Prüfen: {1}, {2}.',
  'HERO {0} changed from {1} to {2}. {3} {4} keeps its name and number until reviewed; set {5} to {2} to accept the new number.': 'HERO-{0}: {1} → {2}; {3} {4} bleibt. {5} auf {2} setzen.',
  'customer number':'Kundennummer', 'Customer':'Kunde', 'customer':'Kunde', 'Project':'Projekt', 'project':'Projekt',

  'HERO response omits the source of: {0}.':'HERO-Antwort ohne Quelle: {0}.',
  'Existing {0} has no stable HERO link.':'Vorhandenes {0} hat keinen stabilen HERO-Link.',
  '{0} Local changes were preserved: {1}.':'{0} Lokale Änderungen beibehalten: {1}.',
  'Local field change observed; no outbound HERO mutation was prepared.':'Lokale Änderung erkannt.',

  'Automatic sync is blocked: no materialized runtime is available; no HERO request was made.': 'Kein Laufzeitsystem verfügbar.',
  '{0}: run {1} queued.': '{0}: Lauf {1} wartet.',
  'Polling configuration is not supported ({0}); no HERO request was made.': 'Polling-Konfiguration nicht unterstützt ({0}).',

  'Event Migration': 'Event-Migration', 'Events': 'Termine',
  '{0} needs an approved read service with an active mapping for {1} on a snapshot of the HERO source instance "{2}". Approve one under API & Schema and Mapping.': '{0} benötigt Mapping für {1} in Quelle "{2}".',
};
const frappe = {boot: {lang: 'de'}};
const window = {hero_integration_i18n: {text: value => translations[value] || value}};
vm.runInNewContext(readFileSync(new URL('../public/js/hero_diagnostics.js', import.meta.url), 'utf8'), {window, frappe});
vm.runInNewContext(readFileSync(new URL('../public/js/hero_migration.js', import.meta.url), 'utf8'), {window, frappe});
const {diagnostic} = window.hero_integration_migration;

test('stored diagnostics translate at display time while preserving identifiers', () => {
  assert.equal(diagnostic('Field conflict requires review: custom_id, name.'), 'Feldkonflikt erfordert Prüfung: custom_id, name.');
  assert.equal(diagnostic('Recheck requested: Task fields require review: owner'), 'Erneute Prüfung angefordert: Task-Felder erfordern Prüfung: owner');
  assert.equal(diagnostic('Another HERO run is active: RUN-007.'), 'Ein anderer HERO-Lauf ist aktiv: RUN-007.');
  assert.equal(diagnostic('A known static error.'), 'Ein bekannter Fehler.');
  assert.equal(diagnostic("Customer 'C-42' already exists without this HERO record's link. The HERO number is not reassigned, suffixed or merged; check whether 'C-42' is the same customer and link or rename it."), "Kunde 'C-42' existiert bereits; Nummer bleibt unverändert. Prüfen: 'C-42', Kunde.");
  assert.equal(diagnostic("Project 'P-42' already exists without this HERO record's link. The HERO number is not reassigned, suffixed or merged; check whether 'P-42' is the same project and link or rename it."), "Projekt 'P-42' existiert bereits; Nummer bleibt unverändert. Prüfen: 'P-42', Projekt.");
  const drift = "HERO customer number changed from 'old' to 'new'. Customer CUST-42 keeps its name and number until reviewed; set custom_hero_customer_number to 'new' to accept the new number.";
  assert.equal(diagnostic(drift), "HERO-Kundennummer: 'old' → 'new'; Kunde CUST-42 bleibt. custom_hero_customer_number auf 'new' setzen.");
  const mismatch = drift.replace("to 'new' to accept", "to 'other' to accept");
  assert.equal(diagnostic(mismatch), mismatch);
  assert.equal(diagnostic('HERO response omits the source of: customer_type, phone.'), 'HERO-Antwort ohne Quelle: customer_type, phone.');
  assert.equal(diagnostic('Existing Customer has no stable HERO link.'), 'Vorhandenes Kunde hat keinen stabilen HERO-Link.');
  assert.equal(diagnostic('Local field change observed; no outbound HERO mutation was prepared. Local changes were preserved: subject.'), 'Lokale Änderung erkannt. Lokale Änderungen beibehalten: subject.');
  assert.equal(diagnostic('Automatic sync is blocked: A known reason.'), 'Automatischer Sync blockiert: Ein bekannter Grund.');
  assert.equal(diagnostic('Automatic sync is blocked: no materialized runtime is available; no HERO request was made.'), 'Kein Laufzeitsystem verfügbar.');
  assert.equal(diagnostic('Events: run RUN-123 queued.'), 'Termine: Lauf RUN-123 wartet.');
  assert.equal(diagnostic('Polling configuration is not supported (timezone Mars); no HERO request was made.'), 'Polling-Konfiguration nicht unterstützt (timezone Mars).');
  assert.equal(diagnostic('Event Migration needs an approved read service with an active mapping for Events on a snapshot of the HERO source instance "source-42". Approve one under API & Schema and Mapping.'), 'Event-Migration benötigt Mapping für Termine in Quelle "source-42".');
  assert.equal(diagnostic('External system: arbitrary text'), 'External system: arbitrary text');
});

test('the same stored English message follows the active language', () => {
  window.hero_integration_i18n.text = value => value;
  assert.equal(diagnostic('Field conflict requires review: name.'), 'Field conflict requires review: name.');
  const drift = "HERO customer number changed from 'old' to 'new'. Customer CUST-42 keeps its name and number until reviewed; set custom_hero_customer_number to 'new' to accept the new number.";
  assert.equal(diagnostic(drift), drift);
  const collision = "Customer 'C-42' already exists without this HERO record's link. The HERO number is not reassigned, suffixed or merged; check whether 'C-42' is the same customer and link or rename it.";
  assert.equal(diagnostic(collision), collision);
});


test('dates follow the active Frappe language and handle invalid input', () => {
  const {formatDateTime} = window.hero_integration_migration;
  const date = new Date('2026-05-04T12:34:00');
  const options = {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'};
  frappe.boot.lang = 'de';
  assert.equal(formatDateTime('2026-05-04 12:34:00'), new Intl.DateTimeFormat('de', options).format(date));
  frappe.boot.lang = 'en';
  assert.equal(formatDateTime('2026-05-04 12:34:00'), new Intl.DateTimeFormat('en', options).format(date));
  frappe.boot.lang = 'bad_locale_!';
  assert.equal(formatDateTime('2026-05-04 12:34:00'), new Intl.DateTimeFormat('en', options).format(date));
  assert.equal(formatDateTime('not a date'), '—');
});

test('phase and testing summaries translate status and retain counts', () => {
  const messages = {'Running':'Läuft', 'PENDING':'Ausstehend',
    '{0} · {1} processed':'{0} · {1} verarbeitet',
    '{0} · {1}/{2} HERO requests · {3} records':'{0} · {1}/{2} HERO-Anfragen · {3} Datensätze'};
  window.hero_integration_i18n.text = value => messages[value] || value;
  frappe.boot.lang = 'de';
  const {phaseSummary, testingSummary} = window.hero_integration_migration;
  assert.equal(phaseSummary('Running', 12), 'Läuft · 12 verarbeitet');
  const run = {testing_executed_actions:2, testing_max_actions:5, testing_records_received:7};
  assert.equal(testingSummary(run), 'Ausstehend · 2/5 HERO-Anfragen · 7 Datensätze');
  assert.equal(run.testing_executed_actions, 2);
});

test('invalid language codes do not prevent numeric summaries', () => {
  window.hero_integration_i18n.text = value => value;
  frappe.boot.lang = 'de_DE';
  assert.equal(window.hero_integration_migration.phaseSummary('Running', 1234), 'Running · 1.234 processed');
  frappe.boot.lang = 'bad_locale_!';
  assert.equal(window.hero_integration_migration.phaseSummary('Running', 1234), 'Running · 1,234 processed');
});

test('price conflicts of the product import translate with the price list name', () => {
  const messages = {'The {0} price {1} was changed in ERPNext. The newer HERO article was not applied.': 'Preis {1} ({0}) in ERPNext geändert.',
                    'Standard Selling': 'Standard-Verkauf'};
  window.hero_integration_i18n.text = value => messages[value] || value;
  assert.equal(diagnostic('The Standard Selling price PRICE-7 was changed in ERPNext. The newer HERO article was not applied.'),
               'Preis PRICE-7 (Standard-Verkauf) in ERPNext geändert.');
});
