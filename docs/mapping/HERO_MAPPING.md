# HERO ↔ ERPNext: geprüftes Mapping (Stand 01.11.2026)

Dieses Dokument ersetzt den Vorschlag
[HERO_MAPPING_PLAN_vorschlag_2026-11-01.md](HERO_MAPPING_PLAN_vorschlag_2026-11-01.md)
als verbindliche Übersicht. Es führt drei Quellen zusammen:

- den Vorschlag (Schema `Schema.txt`, SHA-256 `7b6d360b…`, Code-Stand `29b32cf`),
- die auf der Dev-Site **aktiven** Mapping-Regeln (`HERO Frappe Mapping`, Status Active, abgefragt am 01.11.2026),
- die Entscheidungen des Inhabers vom 30.10. bis 01.11.2026.

Status-Werte:

| Status | Bedeutung |
| --- | --- |
| **aktiv** | Läuft auf der Site. |
| **entschieden** | Vom Inhaber festgelegt und umgesetzt. |
| **ausgeschlossen** | Bewusst nicht gemappt, mit Begründung. |
| **offen** | Braucht eine Entscheidung des Inhabers. Bis dahin gibt es keine Zuordnung. |
| **später** | Eigener Bereich, noch nicht begonnen. |

## 1. Prüfergebnis zum Vorschlag

**Richtig und übernommen:**
- Identität nur über `HERO Integration Link`.
- Fehlende Werte werden nicht gelöscht.
- Feldweiser Konfliktabgleich.
- Externe Zeitstempel ersetzen nie `creation` und `modified`.
- Mutationen nur einzeln, mit Vorschau und unabhängiger Nachprüfung.
- `birth_date` ausgeschlossen.
- Zuweisungen, Status-Workflows und Löschungen nicht aus Beobachtungen ableiten.

**Überholt durch Entscheidungen (korrigiert):**

| Stelle im Vorschlag | Richtig ist |
| --- | --- |
| `measure` nur über die Titelregel | `measure` bestimmt auch das native **Project Type** (Gewerk). |
| `current_project_match_status*` „kein natives Ziel und kein Writeback“ | Das Feld **Phase** zeigt den HERO-Schritt. Gesendet wird die Phase über `current_project_match_status { step_id }`. Live getestet am Testprojekt KLIM-2676. |
| `step_id` „nicht geplant“ | `ProjectMatchInput.step_id` wird von HERO **ignoriert**, nachgewiesen im Live-Test. Nicht verwenden. |
| `project_match_statuses` | Das ist nur die Status-Historie. Die möglichen Phasen sind `type.project_status_steps`, sortiert nach `status_code`, `sort_order` und `id`, was genau der HERO-Anzeige entspricht. |
| Projekt-ID | HERO-Projekte heißen `<Gewerk>-<display_id>-H`, und `-H` ist reserviert. |
| Ausgangs-Whitelist Projekt: `project_name`, `project_nr`, `project_type`, `display_id` | **Abgelehnt.** Der ERPNext-Titel ist zusammengesetzt und würde den HERO-Projekttitel überschreiben. Nummer, Display-ID und Typ sind Identitäts- bzw. Namensdaten. Code seit `5aba6ae` auf Notes beschränkt. |

**Hinweis zum Vorschlag:** Sein Verfasser hat am Testprojekt 11997764 selbst eine
Notiz-Mutation ausgeführt und wieder zurückgesetzt. Den Ausgangszustand hat er in
`/tmp/hero-test-project-rggsdwm7.json` gespeichert. HERO-Historie und
Änderungszeitpunkt des Testprojekts enthalten diese Vorgänge.

## 2. Projekt (`HeroProjectMatches` → Project)

### Eingehend

| HERO | ERPNext | Status |
| --- | --- | --- |
| `id` | HERO Integration Link, `custom_hero_source_id` | aktiv |
| `display_id` + `measure` | Projekt-ID `<Gewerk>-<display_id>-H`; „Project Nr.“ | aktiv / entschieden |
| `measure` | **Project Type** = Gewerk (Photovoltaik, Elektrohandwerk, Klimatechnik, Dachsanierung, Absturzsicherung, Sonstiges) | entschieden |
| Titelbestandteile (`project_nr`, `measure`, `customer.title/full_name`, `address`) | `project_name` nach Titelregel. Eine reine Kundenänderung benennt nicht um. Anrede optional. Ab 140 Zeichen abgelehnt. | aktiv |
| `customer_id` | `customer` über den Kunden-Link. Ohne eindeutigen Link → Freigabe nötig. | aktiv |
| `customer_id` | `company` (bestätigte ERPNext-Firma) | aktiv |
| `address.full_address` | „Project Address“. Fehlt sie → Freigabe nötig. | aktiv |
| `current_project_match_status.*` | `custom_hero_observed_status_*` (ausgeblendet) | aktiv |
| `current_project_match_status.step_id` + Schrittname | **Phase** (`custom_hero_phase`) | entschieden |
| `created` / `modified` | „Creation“ / „Modified“ (eigene Felder, keine Systemzeitstempel) | aktiv |
| `project_id` | „Project ID“ | aktiv |
| `partner_notes` | „Notes“ (`custom_hero_partner_notes`), nicht `Project.notes` | aktiv |
| `project_nr`, `project_type` (HERO-Wert, z. B. „unmanaged“) | ausgeblendete Beobachtungsfelder | aktiv |
| `is_deleted` | keine Löschung. Aufgaben und Termine gelöschter Projekte werden übersprungen. | entschieden |
| `volume` | — | **offen**: Einheit und Bedeutung klären, dann `custom_hero_volume` |
| `ppl_price` | — | **offen**: Bedeutung und Währung klären |
| `measure_id`, `address_id`, `company_branch_id`, `partner_id`, `contact_id`, `type_id`, `marked_*`, `name`, `partner_source`, `relative_id` | — | ausgeschlossen (nur IDs bzw. Flags ohne fachliche Verwendung) |
| `partner`, `company_branch`, `project`, `contact`, `available_actions`, `custom_fields_searchable` | — | ausgeschlossen, bis ein fachlicher Bedarf besteht |
| `file_uploads`, `customer_documents`, `histories`, `project_match_assignments` | — | später (siehe Abschnitt 6) |

### Ausgehend („Send to HERO“, Projektformular)

| ERPNext | HERO | Status |
| --- | --- | --- |
| Notes (`custom_hero_partner_notes`) | `update_project_match.partner_notes` | entschieden, live getestet |
| Phase (Auswahl im Dialog) | `update_project_match.current_project_match_status { step_id }` | entschieden, live getestet |
| Titel, Nummer, Display-ID, Typ, Gewerk, Kunde, Adresse | — | ausgeschlossen |

Regeln:
- Senden dürfen nur System Manager und die Rolle „HERO Writeback“.
- Es gibt einen globalen Schalter (`allow_graphql_mutation_execution`).
- Der Dialog zeigt eine Vorschau mit Fingerabdruck. Ein Feld, das in HERO zwischenzeitlich geändert wurde, wird nicht überschrieben.
- Gesendet wird einmal, mit Audit-Eintrag. Danach wird in HERO nachgelesen, und erst dann zieht die Abgleich-Basis nach.

## 3. Kunde und Kontakt (`HeroCustomers` → Customer, Contact, Address)

### Eingehend

| HERO | ERPNext | Status |
| --- | --- | --- |
| `id` | Link, `custom_hero_source_id` | aktiv |
| `nr` | Kunden-ID (exakt, führende Nullen bleiben) und `custom_hero_customer_number`. Ohne verwendbare Nummer → `HERO-CUSTOMER-<uuid>`. | aktiv |
| `company_name` / `full_name` | `customer_name`, `customer_type` | aktiv |
| `first_name`, `last_name`, `email`, `phone_mobile` | primärer Contact; die Kundenfelder spiegeln ihn | aktiv |
| `phone_home` | Contact Phone, exklusiv für den primären Kontakt | aktiv |
| Telefon als Freitext | erste gültige Nummer plus Volltext als Kommentar | entschieden |
| ungültige E-Mail | wird verworfen | entschieden |
| `address.*` | Address über den Adressadapter. Ohne Land → Deutschland. Unvollständig → Kunde ohne Adresse. | aktiv / entschieden |
| `category`, `type` | `custom_hero_customer_category`, `custom_hero_customer_type` | aktiv |
| `is_contact_person` | Für Ansprechpartner wird kein Kunde angelegt. | aktiv |
| `is_deleted` | Status der Verknüpfung, keine Löschung | aktiv |
| `title` | — | **offen**: Anrede-Wertetabelle für `Contact.salutation` |
| `company_legal_form` | — | **offen**: Entwurf V3 vorhanden, Übernahme entscheiden |
| `url` | — | **offen**: `Customer.website` |
| `position` | — | **offen**: `Contact.designation` |
| `phone_fax` | — | **offen**: Ziel festlegen |
| `birth_date` | — | ausgeschlossen (Datenschutz) |
| übrige IDs, Formatfelder, `offer_options`, `leitweg_nr`, `accounting`, `parent_customer*`, `contacts`, `customer_addresses` | — | ausgeschlossen, bis ein fachlicher Bedarf besteht |

### Ausgehend

Entscheidung 2026-11-02: alle Kundendaten außer der Kundennummer. Button
„Send to HERO“ auf jedem importierten Kunden (`update_contact`), gleiches
Muster wie bei Projekt und Aufgabe (Vorschau liest HERO live, in HERO
geänderte Werte werden nicht überschrieben, globaler Schalter, Audit,
Nachlesen, Vergleichsbasis fortschreiben). Gesendet werden:

| ERPNext | HERO |
| --- | --- |
| Anrede (Kontakt) | `title` (Dr → „Dr.“) |
| Vorname, Nachname (Kontakt) | `first_name`, `last_name` |
| Kundenname, nur bei Firmen | `company_name` |
| E-Mail, Mobil (Kontakt) | `email`, `phone_mobile` |
| weitere Telefonnummer (Kontakt) | `phone_home` |
| Hauptadresse: Straße, Zeile 2, PLZ, Ort | `address { street line_2 zipcode city }` |

Nicht gesendet: Kundennummer, HERO-Kategorie/-Typ (HERO-eigene
Klassifizierung), Land. Live getestet am Testkunden KD-01440 (HERO 7966175):
Nachname und Adresszeile 2 hin und zurück. Ein HERO-Festnetz gleich der
Mobilnummer speichert ERPNext nur einmal; das gilt nicht als Änderung.

## 4. Aufgabe (`HeroTasks` → Task)

| HERO | ERPNext | Status |
| --- | --- | --- |
| `id` | Link | aktiv |
| `title` | `subject`. Leer → Prüfung. | aktiv |
| `comment` | `description`. Leer → keine Prüfung. | aktiv / entschieden |
| `target_project_match_id` | `project`. 0 = kein Projekt, gelöschtes HERO-Projekt → übersprungen, `KONT-` → ohne Projekt. | aktiv / entschieden |
| `due_date` | `custom_hero_due_date` (geschützt) | aktiv |
| `created` | `exp_start_date` (Startdatum). Liegt es nach dem Fälligkeitsdatum, gilt das Fälligkeitsdatum. | aktiv (2026-11-02) |
| `due_date` | zusätzlich `exp_end_date` (Enddatum = „erledigen bis“) | aktiv (2026-11-02) |
| `done_date` | erledigt → `status` Completed + `completed_on` (nie in der Zukunft); leer → wieder Open. ERPNext-„Overdue“ zählt nicht als Änderung. | aktiv (2026-11-02) |
| `target_user_id` | `custom_hero_assignee` (E-Mail des HERO-Benutzers, nur lesbar). Gibt es einen aktiven ERPNext-Benutzer mit dieser E-Mail, bekommt er die Zuweisung (ToDo); ändert HERO den Zuständigen, wird die alte HERO-Zuweisung storniert. Ohne passenden Benutzer bleibt die Aufgabe unzugewiesen (kein Fehler). | aktiv (2026-11-02) |
| `author_user_id` | — | nicht nötig: jeder darf jedem zuweisen |
| `start`, `end` | — | ungenutzt (Start = Erstellung, Ende = Fälligkeit) |
| Ausgehend (`update_task`) | Button „Send to HERO“ auf jeder Aufgabe: Titel, Beschreibung, Enddatum, Erledigt, Zuständiger. In ERPNext angelegte Aufgaben eines HERO-Projekts werden in HERO angelegt und verknüpft. | aktiv, Schalter AUS (2026-11-02) |

HERO-Benutzer kommen aus `company.partners.user { id email }` (HERO hat keine
Benutzerliste); die Liste wird pro Aufgabenlauf einmal gelesen. Ist sie nicht
lesbar, bleibt der Zuständige unverändert. Für vorhandene Aufgaben ohne
Vergleichsbasis übernimmt der erste Lauf die HERO-Werte, solange ERPNext noch
leer bzw. offen ist; sonst Prüfung.

**Senden an HERO (Aufgaben).** Wie beim Projekt: Vorschau liest HERO live,
ein seit dem letzten Import in HERO geänderter Wert wird nicht überschrieben,
ein Mutation-Aufruf hinter dem globalen Schalter und dem Mutation-Audit, danach
Nachlesen in HERO und Fortschreiben der Vergleichsbasis. Nur „System Manager“
und „HERO Writeback“. Zuständiger: genau eine offene ERPNext-Zuweisung, deren
Benutzer-E-Mail einem HERO-Benutzer entspricht (HERO kennt nur einen
Zuständigen). Erkenntnisse aus dem Live-Test an Testprojekt KLIM-2676-H
(HERO-Aufgabe 2305745): HERO ignoriert Zeitstempel mit Mikrosekunden und
`null`; ein leerer String leert einen Wert (öffnet eine erledigte Aufgabe
wieder). Datumswerte werden als 12:00 Ortszeit gesendet (Erledigt heute: jetzt).

## 5. Termin (`HeroCalendarEvents` → Event)

| HERO | ERPNext | Status |
| --- | --- | --- |
| `id`, `title`, `description`, `start`, `end`, `all_day`, `full_address` | Link, `subject`, `description`, `starts_on`, `ends_on`, `all_day`, `location` | aktiv |
| leerer Titel | neu: Terminart; bestehend: Betreff bleibt | entschieden |
| `project_match_id` | `custom_hero_project` (gelöschtes Projekt → übersprungen) | aktiv |
| `is_recurring` | nur der gelieferte Termin, Freigabe nötig | entschieden |
| `partners`, `resources` | eigene Tabellen, keine Einladung | aktiv |
| `category_id`, `color`, `deleted`, `is_done`, `provider`, `readonly`, `type`, `localized_type`, `created`, `modified`, `partner_id` | `custom_hero_*` | aktiv |
| `url`, `title_lines` | — | ausgeschlossen |
| Ausgehend `update_calendar_event` | — | später |

## 6. Spätere Bereiche (aus dem Vorschlag übernommen, nicht begonnen)

| Bereich | HERO-Quelle | Voraussetzung |
| --- | --- | --- |
| Projektlogbuch | `project_histories`, Mutation `add_logbook_entry` (Pflichtfeld `custom_text`) | Lesevertrag, Sichtbarkeit, Paginierung |
| Dokumente | `customer_documents` | Dokumentarten-, Steuer- und Buchungsmodell |
| Zeiterfassung | `tracking_times`, `update_tracking_time` | Zuordnung Mitarbeiter, Zeitzone, Freigabe |
| Dateien | `file_uploads` | Lebensdauer der URLs, Sicherheit, Speicherort |
| Artikel und Leistungen | `supply_products`, `supply_services` | Einheiten, Preise, Steuern |
| Abwesenheiten | `absences` | HRMS-Abwesenheitsarten, Genehmigung |
| Kundenadressen ausgehend | `update_customer_address` | `street` vs. `line_1`, Länder-IDs |

## 7. Entscheidungen, die das Mapping erweitern würden

1. Projekt: Bedeutung und Einheit von `volume` sowie `ppl_price`.
2. Kunde: Anrede-Tabelle (`title`), Rechtsform, Website, Position, Fax.
3. Kunde ausgehend: ja oder nein, und welche Felder.
4. Aufgaben: Zuordnung HERO-Benutzer ↔ ERPNext-Mitarbeiter (Voraussetzung für Zuweisungen), Bedeutung von `done_date`, `start` und `end`.
5. Reihenfolge der späteren Bereiche (Abschnitt 6).
