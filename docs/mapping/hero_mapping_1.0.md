# HERO ↔ ERPNext: geprüftes Mapping

Dieses Dokument ist die verbindliche Übersicht. Es ersetzt den Vorschlag
[mapping_plan_1.0.md](mapping_plan_1.0.md).
Es führt drei Quellen zusammen:

- den Vorschlag (Schema `Schema.txt`, SHA-256 `7b6d360b…`),
- die aktiven Mapping-Regeln (`HERO Frappe Mapping`, Status Active),
- die Entscheidungen des Inhabers.

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
| `current_project_match_status*` „kein natives Ziel und kein Writeback“ | Das Feld **Phase** zeigt den HERO-Schritt. Die App sendet die Phase über `current_project_match_status { step_id }`. Live getestet. |
| `step_id` „nicht geplant“ | HERO **ignoriert** `ProjectMatchInput.step_id`. Der Live-Test belegt das. Nicht verwenden. |
| `project_match_statuses` | Das ist nur die Status-Historie. Die möglichen Phasen stehen in `type.project_status_steps`. Sortiert nach `status_code`, `sort_order` und `id` entsprechen sie genau der HERO-Anzeige. |
| Projekt-ID | HERO-Projekte heißen `<Gewerk>-<display_id>-H`. `-H` ist reserviert. |
| Ausgangs-Whitelist Projekt: `project_name`, `project_nr`, `project_type`, `display_id` | **Abgelehnt.** Der ERPNext-Titel ist zusammengesetzt und würde den HERO-Projekttitel überschreiben. Nummer, Display-ID und Typ sind Identitäts- und Namensdaten. Der Code sendet nur Notes. |

## 2. Projekt (`HeroProjectMatches` → Project)

### Eingehend

| HERO | ERPNext | Status |
| --- | --- | --- |
| `id` | HERO Integration Link, `custom_hero_source_id` | aktiv |
| `display_id` + `measure` | Projekt-ID `<Gewerk>-<display_id>-H`; „Project Nr.“ | aktiv / entschieden |
| `measure` | **Project Type** = Gewerk (Photovoltaik, Elektrohandwerk, Klimatechnik, Dachsanierung, Absturzsicherung, Sonstiges) | entschieden |
| Titelbestandteile (`project_nr`, `measure`, `customer.title/full_name`, `address`) | `project_name` nach Titelregel, **ohne Anrede**. Vorhandene Titel wurden einmalig lokal bereinigt; lokal bearbeitete Titel bleiben unverändert. Eine reine Kundenänderung benennt nicht um. Titel ab 140 Zeichen werden abgelehnt und manuell bearbeitet. | aktiv |
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
| `volume` | `custom_hero_volume` (Währung, nur lesbar): Projektvolumen in Euro | aktiv |
| `ppl_price` | — | nicht zugeordnet: Bedeutung unbekannt |
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
- Ein globaler Schalter (`allow_graphql_mutation_execution`) gibt das Senden frei.
- Der Dialog zeigt eine Vorschau mit Fingerabdruck. Hat HERO ein Feld zwischenzeitlich geändert, überschreibt die App es nicht.
- Die App sendet einmal und schreibt einen Audit-Eintrag. Danach liest sie in HERO nach. Erst dann zieht die Abgleich-Basis nach.

### Logbuch (Aktivität)

Das HERO-Logbuch (`ProjectMatch.histories`) erscheint in der „Aktivität“ des
ERPNext-Projekts. Jeder Eintrag ist eine Info-Zeile „HERO Logbuch · Titel ·
Autor“ mit Text und HERO-Datum.

Die App liest das Logbuch nach jedem Projektlauf (Execute). Die Abfrage liegt
verschachtelt in der Projektliste, mit 25 Projekten je Abfrage. HERO lehnt
einen globalen `project_histories`-Abruf und 100 Projekte je Abfrage ab. Je
Projekt liest die App bis zu 500 Einträge.

Neue Einträge kommen dazu, keiner doppelt (Link-Typ `project_history`). Die App
sendet nichts an HERO.

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
| `title` | `Contact.salutation` über die Anrede-Tabelle | aktiv |
| `company_legal_form` | — | nicht übernommen (eingehend nur die Anrede-Tabelle) |
| `url` | — | nicht übernommen |
| `position` | — | nicht übernommen |
| `phone_fax` | — | nicht übernommen |
| `birth_date` | — | ausgeschlossen (Datenschutz) |
| übrige IDs, Formatfelder, `offer_options`, `leitweg_nr`, `accounting`, `parent_customer*`, `contacts`, `customer_addresses` | — | ausgeschlossen, bis ein fachlicher Bedarf besteht |

### Ausgehend

Entscheidung: Die App sendet alle Kundendaten außer der Kundennummer. Jeder
importierte Kunde hat den Button „Send to HERO“ (`update_contact`).

Der Ablauf folgt dem Muster von Projekt und Aufgabe: Die Vorschau liest HERO
live. In HERO geänderte Werte überschreibt die App nicht. Es gelten globaler
Schalter, Audit, Nachlesen und Fortschreiben der Vergleichsbasis. Gesendet
werden:

| ERPNext | HERO |
| --- | --- |
| Anrede (Kontakt) | `title` (Dr → „Dr.“) |
| Vorname, Nachname (Kontakt) | `first_name`, `last_name` |
| Kundenname, nur bei Firmen | `company_name` |
| E-Mail, Mobil (Kontakt) | `email`, `phone_mobile` |
| weitere Telefonnummer (Kontakt) | `phone_home` |
| Hauptadresse: Straße, Zeile 2, PLZ, Ort | `address { street line_2 zipcode city }` |

Nicht gesendet: Kundennummer, HERO-Kategorie und -Typ (HERO-eigene
Klassifizierung), Land. Der Live-Test hat Nachname und Adresszeile 2 hin und
zurück übertragen. Ist das HERO-Festnetz gleich der Mobilnummer, speichert
ERPNext die Nummer nur einmal. Das gilt nicht als Änderung.

## 4. Aufgabe (`HeroTasks` → Task)

| HERO | ERPNext | Status |
| --- | --- | --- |
| `id` | Link | aktiv |
| `title` | `subject`. Leer → Prüfung. | aktiv |
| `comment` | `description`. Leer → keine Prüfung. | aktiv / entschieden |
| `target_project_match_id` | `project`. 0 = kein Projekt, gelöschtes HERO-Projekt → übersprungen, `KONT-` → ohne Projekt. | aktiv / entschieden |
| `due_date` | `custom_hero_due_date` (geschützt) | aktiv |
| `created` | `exp_start_date` (Startdatum). Liegt es nach dem Fälligkeitsdatum, gilt das Fälligkeitsdatum. | aktiv |
| `due_date` | zusätzlich `exp_end_date` (Enddatum = „erledigen bis“) | aktiv |
| `done_date` | erledigt → `status` Completed + `completed_on` (nie in der Zukunft); leer → wieder Open. ERPNext-„Overdue“ zählt nicht als Änderung. | aktiv |
| `target_user_id` | `custom_hero_assignee` (E-Mail des HERO-Benutzers, nur lesbar). Hat ein aktiver ERPNext-Benutzer diese E-Mail, bekommt er die Zuweisung (ToDo). Ändert HERO den Zuständigen, storniert die App die alte HERO-Zuweisung. Ohne passenden Benutzer bleibt die Aufgabe unzugewiesen (kein Fehler). | aktiv |
| `author_user_id` | — | nicht nötig: jeder darf jedem zuweisen |
| `start`, `end` | — | ungenutzt (Start = Erstellung, Ende = Fälligkeit) |
| Ausgehend (`update_task`) | Button „Send to HERO“ auf jeder Aufgabe: Titel, Beschreibung, Enddatum, Erledigt, Zuständiger. Legt ERPNext eine Aufgabe in einem HERO-Projekt an, legt die App sie in HERO an und verknüpft sie. | aktiv, Schalter AUS |

HERO hat keine Benutzerliste. Die App liest die HERO-Benutzer aus
`company.partners.user { id email }`, einmal pro Aufgabenlauf. Ist die Liste
nicht lesbar, bleibt der Zuständige unverändert.

Vorhandene Aufgaben ohne Vergleichsbasis: Ist ERPNext noch leer bzw. offen,
übernimmt der erste Lauf die HERO-Werte. Sonst geht die Aufgabe in die Prüfung.

**Senden an HERO (Aufgaben).** Der Ablauf gleicht dem Projekt:

- Die Vorschau liest HERO live.
- Hat HERO einen Wert seit dem letzten Import geändert, überschreibt die App ihn nicht.
- Die App ruft die Mutation einmal auf, hinter dem globalen Schalter und dem Mutation-Audit.
- Danach liest sie in HERO nach und schreibt die Vergleichsbasis fort.
- Senden dürfen nur „System Manager“ und „HERO Writeback“.

HERO kennt nur einen Zuständigen. Die App sendet ihn nur bei genau einer
offenen ERPNext-Zuweisung, deren Benutzer-E-Mail einem HERO-Benutzer
entspricht.

Erkenntnisse aus dem Live-Test: HERO ignoriert Zeitstempel mit Mikrosekunden
und `null`. Ein leerer String leert einen Wert und öffnet so eine erledigte
Aufgabe wieder. Die App sendet Datumswerte als 12:00 Ortszeit. Für „Erledigt
heute“ sendet sie die aktuelle Zeit.

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

## 7. Entscheidungen

Getroffen:

1. Projekt: `volume` ist das Projektvolumen in Euro. `ppl_price` bleibt ohne Zuordnung, bis seine Bedeutung bekannt ist.
2. Kunde eingehend: nur die Anrede-Tabelle (`title`). Rechtsform, Website, Position und Fax werden nicht übernommen.
3. Kunde ausgehend: alle Kundendaten außer der Kundennummer, über den Button „Send to HERO“.
4. Aufgaben: Benutzer werden per E-Mail zugeordnet. Jeder darf jedem Aufgaben zuweisen. Erledigt ist eine Aufgabe, wenn der Zuständige sie abhakt (`done_date`). Startdatum ist das Erstellungsdatum, Enddatum das Fälligkeitsdatum; `start` und `end` bleiben ungenutzt.

Offen:

5. Reihenfolge der späteren Bereiche (Abschnitt 6).
