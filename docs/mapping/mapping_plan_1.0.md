# HERO ↔ ERPNext: Mapping-Plan

Schema: HERO-Schemadatei `Schema.txt`, SHA-256 `7b6d360ba876aad51369c1fd7e6f82a6f3b1c4a683f2a7a4f347793101d33f7c`. Plattform: Frappe/ERPNext v16.

Der Plan aktiviert nichts, importiert keine Daten und führt keine Mutation aus. Das Schema belegt Feldnamen und Typen. Es belegt keine Berechtigungen, kein Verhalten, keine Vollständigkeit und keine aktive Site-Konfiguration. Die Vorschläge ergänzen bestehende Verträge und ändern keine Freigaben.

## Gemeinsame Regeln

- Identität: Der HERO Integration Link hält Quellsystem, Quellinstanz, Objekttyp und externe ID. Verknüpfte Objekte nur über belegte Identitäten auflösen. Keine Zuordnung über Namen oder E-Mail.
- Eingehend: Nur freigegebene Queries und Feldregeln nutzen. Nicht gelieferte Werte bleiben erhalten. NULL oder Leerzeichen löschen erst nach vereinbarter Semantik. Externe Zeitstempel nie als Frappe creation/modified einsetzen.
- Konflikte: Pro Feld den bestätigten Ausgangszustand vergleichen. Gleichzeitige Änderungen zur Prüfung halten. Erneute Verarbeitung muss idempotent sein. Löschungen, Status, Zuweisungen und Einladungen nicht aus Beobachtungen ableiten.
- Ausgehend: Geprüften Link, Berechtigung, Zieloperation, Eingabetyp, Feld, Vorschau und Änderungsstand binden. Mutationsantwort und unabhängigen Readback auswerten. Bei unklarer Antwort nicht blind wiederholen. Bestehende Mutation-Drafts geben keine Ausführung frei.
- Zeitpunkt, Zeitzone, Pagination und Sortierung nicht aus Feldtypen ableiten. Offset-Scans auf Dopplungen und Vollständigkeit prüfen. Bei instabiler Quelle keinen vollständigen Lauf melden.

## Kernbereiche: jedes direkte HERO-Ausgabefeld

Die Zielangaben in diesen Tabellen sind Planungspositionen. Bestehende Implementierung und Site-Aktivierung getrennt prüfen. „Prüfen“ bezeichnet keine freigegebene Zuordnung. Nicht spezifizierte Felder laufen einzeln als Beobachtung oder Beziehungskandidat. Sie fließen nicht automatisch in native Geschäftsprozesse.

### Projekt / ProjectMatch

| HERO-Feld | Typ | Geplante Behandlung |
| --- | --- | --- |
| `project_type` | `String` | Project.custom_hero_project_type; nicht automatisch Project.project_type |
| `measure_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `measure` | `Measure` | Gewerk über bestehende Projekttitelregel; keine Identitätsableitung |
| `customer_id` | `Int` | Project.customer; über Customer-Link auflösen |
| `customer` | `Customer` | Customer-Link plus bestehende Kundenbeobachtungen |
| `address_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `address` | `Address` | Project.custom_hero_address_full_address als Beobachtung; strukturierte Projektadresse separat prüfen |
| `company_id` | `Int` | externe Beobachtung; Project.company aus bestätigter ERPNext Company |
| `company_branch_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `partner_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `current_project_match_status_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `marked_company` | `Boolean` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `marked_later` | `Boolean` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `company_branch` | `CompanyBranch` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `partner` | `Partner` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `current_project_match_status` | `ProjectMatchStatus` | Project.custom_hero_observed_status_*; kein Workflow |
| `project_match_statuses` | `[ProjectMatchStatus]` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `file_uploads` | `[FileUpload]` | Dateiverknüpfung; Vorbereitung Dateien |
| `customer_documents` | `[CustomerDocument]` | Dokumentverknüpfung; Vorbereitung Dokumente |
| `created` | `DateTime` | Project.custom_hero_source_created |
| `modified` | `DateTime` | Project.custom_hero_source_modified |
| `histories` | `[History]` | Projektaktivität; separates vollständiges History-Importkonzept |
| `contact` | `Customer` | Kontaktbeziehung nur über externe Identität |
| `contact_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `project_match_assignments` | `[ProjectMatchAssignment]` | Zuweisung nur mit geprüftem Identitäts- und Autoritätsvertrag |
| `name` | `String` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `partner_source` | `String` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `relative_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `partner_notes` | `String` | Project.custom_hero_partner_notes; nicht Project.notes |
| `display_id` | `String` | Project.custom_hero_display_id |
| `project_nr` | `String` | Project.custom_hero_project_number |
| `project` | `Project` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `volume` | `Float` | Kandidat custom_hero_volume, Quelleinheit klären |
| `project_title` | `String` | Project.project_name; bestehende Titelregel prüfen |
| `is_deleted` | `Boolean` | Link-Lebenszyklus beobachten; kein automatisches Löschen |
| `project_id` | `Int` | Project.custom_hero_related_project_id; reine Beobachtung |
| `id` | `Int` | HERO Integration Link.source_id; Project.custom_hero_source_id; Identität |
| `available_actions` | `[ProjectMatchAction]` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `ppl_price` | `Float` | Kandidat custom_hero_ppl_price, Bedeutung/Währung klären |
| `type` | `ProjectType` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `type_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `custom_fields_searchable` | `SearchableValue` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |

### Kunde und Kontakt / Customer

| HERO-Feld | Typ | Geplante Behandlung |
| --- | --- | --- |
| `user_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `type` | `String` | Customer.custom_hero_customer_type, Beobachtung |
| `title` | `String` | Contact.salutation nur über bestätigte Wertetabelle |
| `title_custom` | `String` | nur mit bestätigter Anrederegel; kein automatischer Fallback |
| `first_name` | `String` | Contact.first_name über primären Kontaktadapter |
| `last_name` | `String` | Contact.last_name über primären Kontaktadapter |
| `company_name` | `String` | Customer.customer_name nach bestehender Namensregel |
| `company_legal_form` | `String` | vorgeschlagen Customer.custom_hero_company_legal_form; noch separat übernehmen |
| `phone_home` | `String` | Contact Phone.phone über exklusiven primären Kontaktadapter |
| `phone_mobile` | `String` | Contact Phone.phone über primären Kontaktadapter |
| `phone_fax` | `String` | Fax-Ziel und Primärkontakt/Adresse erst festlegen |
| `url` | `String` | Customer.website nach URL- und Eigentumsregel |
| `address_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `reachability` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `source` | `String` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `position` | `String` | Contact.designation nur bei bestätigter Personenzuordnung |
| `user` | `User` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `address` | `Address` | Address.address_line1/city/pincode/country/address_line2 über Adressadapter |
| `customer_addresses` | `[CustomerAddress]` | separate Address-Links; Identität und Besitz klären |
| `project_matches` | `[ProjectMatch]` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `category` | `String` | Customer.custom_hero_customer_category, Beobachtung |
| `company_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `created` | `DateTime` | externe Zeit separat; Frappe creation nicht ersetzen |
| `modified` | `DateTime` | externe Zeit separat; Frappe modified nicht ersetzen |
| `nr` | `String` | Customer.custom_hero_customer_number |
| `parent_customer_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `email` | `String` | Contact Email.email_id über primären Kontaktadapter |
| `offer_options` | `JSON` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `parent_customer` | `Customer` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `contacts` | `[Customer]` | separate Contact-Links; Identität und Besitz klären |
| `is_deleted` | `Boolean` | Link-Lebenszyklus beobachten, nicht automatisch löschen |
| `full_name` | `String` | Customer.customer_name nach bestehender Namensregel; kein Überschreiben durch Auswahl ohne Prüfung |
| `phone_home_formatted` | `String` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `phone_mobile_formatted` | `String` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `is_invoice_recipient` | `Boolean` | Beobachtung; keine Rechnungsberechtigung ableiten |
| `reachability_string` | `String` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `initial_name` | `String` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `category_name` | `String` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `contact_match_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `is_contact_person` | `Boolean` | Kontaktklassifikation; keinen Customer für Kontaktperson anlegen |
| `contact_match` | `ProjectMatch` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `leitweg_nr` | `String` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `id` | `Int` | HERO Integration Link.source_id; Customer.custom_hero_source_id |
| `partner_notes` | `String` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `birth_date` | `Date` | ausgeschlossen bis Zweck und Aufbewahrung geklärt |
| `accounting` | `Accounting_Accounting` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |

### Aufgabe / Task

| HERO-Feld | Typ | Geplante Behandlung |
| --- | --- | --- |
| `author_user_id` | `Int` | externe Identität beobachten; kein automatischer Benutzerlink |
| `author` | `User` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `company_id` | `Int` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `target_user_id` | `Int` | externe Identität beobachten; kein automatisches Zuweisen |
| `target_user` | `User` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `title` | `String` | Task.subject; erforderliches Feld im nativen Task-Vertrag |
| `comment` | `String` | Task.description, nur gewählte Regel |
| `target_project_match_id` | `Int` | Task.project über Projekt-Link |
| `target_project_match` | `ProjectMatch` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `due_date` | `DateTime` | separates geschütztes externes Fälligkeitsfeld; Zeitzone/NULL prüfen |
| `done_date` | `DateTime` | externe Beobachtung; kein automatischer Abschluss |
| `created` | `DateTime` | externe Zeit separat |
| `modified` | `DateTime` | externe Zeit separat |
| `start` | `DateTime` | externe Zeit beobachten; Bedeutung prüfen |
| `end` | `DateTime` | externe Zeit beobachten; Bedeutung prüfen |
| `ephemeral_id` | `Mixed` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `is_deleted` | `Boolean` | externe Beobachtung; nicht löschen oder schließen |
| `id` | `Int` | HERO Integration Link für Task bzw. bestehende externe Projektion |

### Kalender / CalendarEvent

| HERO-Feld | Typ | Geplante Behandlung |
| --- | --- | --- |
| `category` | `CalendarEventCategory` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `category_id` | `Int` | Event.custom_hero_category_id; kein nativer Kategorieübergang |
| `project_match` | `ProjectMatch` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `project_match_id` | `Int` | Event.custom_hero_project über Projekt-Link |
| `title` | `String` | Event.subject |
| `description` | `String` | Event.description |
| `start` | `DateTime` | Event.starts_on plus externe Originalzeit; Zeitzonenvertrag erforderlich |
| `end` | `DateTime` | Event.ends_on plus externe Originalzeit; Zeitzonenvertrag erforderlich |
| `all_day` | `Boolean` | Event.all_day; Tagesgrenzen klären |
| `deleted` | `Boolean` | Event.custom_hero_deleted; kein automatisches Löschen |
| `color` | `String` | Event.custom_hero_color; native Farbe nur nach Formatprüfung |
| `partners` | `[Partner]` | HERO Event Partner Beobachtung; keine Einladung |
| `resources` | `[CompanyResource]` | HERO Event Resource Beobachtung; kein Employee-Link |
| `is_done` | `Boolean` | Event.custom_hero_is_done; keinen Status ableiten |
| `is_recurring` | `Boolean` | Event.custom_hero_is_recurring; keine Wiederholungsregel ableiten |
| `provider` | `String` | Event.custom_hero_provider |
| `id` | `Int` | HERO Integration Link; Event.custom_hero_source_id |
| `modified` | `DateTime` | Event.custom_hero_modified, externe Zeit |
| `created` | `DateTime` | Event.custom_hero_created, externe Zeit |
| `type` | `String` | Event.custom_hero_type |
| `localized_type` | `String` | Event.custom_hero_localized_type |
| `url` | `String` | Quellbeobachtung prüfen; vorerst kein natives Ziel und kein Writeback |
| `full_address` | `String` | Event.location |
| `title_lines` | `[String]` | Beziehung separat identifizieren und fachlich prüfen; keine automatische Zuordnung |
| `partner_id` | `Int` | Event.custom_hero_partner_id als externe Beobachtung |
| `readonly` | `Boolean` | Event.custom_hero_readonly; Quellschutz beachten |

## Ausgangsrichtung: konkreter Vertragsentwurf

Die Code-Whitelist umfasst nur Customer und Project. Alle weiteren Zeilen sind Kandidaten und laufen nicht. Jeder Schreibvorgang braucht eine nachweislich gültige HERO-ID, ein Schema und eine verbindliche Semantik.

| ERPNext-Quelle | HERO-Mutation / Eingabe | Status und Regel |
| --- | --- | --- |
| `Customer.custom_hero_customer_number` | `update_contact.contact.nr` | Whitelist; Nummernhoheit klären |
| `Customer.first_name / last_name` | `update_contact.contact.first_name / last_name` | Whitelist; Zuordnung zum gebundenen Kontakt prüfen |
| `Customer.customer_name` | `update_contact.contact.full_name` | Whitelist; Firmen-/Personenname fachlich prüfen |
| `Customer.email_id / mobile_no` | `update_contact.contact.email / phone_mobile` | Whitelist; primären Kontakt und geteilte Datensätze beachten |
| `Project.project_name` | `update_project_match.project_match.project_title` | Whitelist; zusammengesetzten Titel gegen Rohwert prüfen |
| `Project.custom_hero_project_number / custom_hero_project_type` | `update_project_match.project_match.project_nr / project_type` | Whitelist; Nummer und Werteliste prüfen |
| `Project.custom_hero_partner_notes / custom_hero_display_id` | `update_project_match.project_match.partner_notes / display_id` | Whitelist; Besitz und Bearbeitbarkeit prüfen |
| `Customer.website` | `update_contact.contact.url` | Kandidat; URL und Besitz prüfen |
| `Contact.first_name / last_name / designation` | `update_contact.contact.first_name / last_name / position` | Kandidat; Kontaktidentität/Beziehung prüfen |
| `Contact Email.email_id / Contact Phone.phone` | `update_contact.contact.email / phone_mobile / phone_home` | Kandidat; Auswahl primärer Werte und gemeinsame Kontakte klären |
| `Address.address_line1 / address_line2 / city / pincode / country` | `update_customer_address.customer_address.address` | Kandidat; verschachtelten AddressInput und Zielidentität prüfen |
| `Project-Aktivitätskommentar` | `add_logbook_entry.logbook_entry.custom_text` | Kandidat; target und target_project_match_id sowie Sichtbarkeit prüfen; idempotente Anlage und Rücklesung erforderlich |
| `Task.subject / description / project` | `update_task.task.title / comment / target_project_match_id` | Kandidat; Task- und Projektlinks, Autorität prüfen |
| `Task-Termin / Abschluss` | `update_task.task.due_date / done_date / start / end` | Kandidat; Zeitzone, Bedeutung und Statusentscheidung prüfen |
| `Event.subject / description` | `update_calendar_event.calendar_event.title / description` | Kandidat; Event-Link und readonly prüfen |
| `Event.starts_on / ends_on / all_day` | `update_calendar_event.calendar_event.start / end / all_day` | Kandidat; Zeitzonen und Ganztagsgrenzen prüfen |
| `Event-Projekt / Teilnehmer` | `update_calendar_event.calendar_event.project_match_id / partner_ids / resource_ids` | Kandidat; geprüfte Identitätszuordnung und Einladungsfolgen klären |

### Nicht automatisch schreiben

IDs, externe Systemzeitstempel, berechnete Summen, lokale Company, Status-/Workflowwechsel, Löschflags und Zuordnungen von Benutzern oder Ressourcen sind keine generischen Feldmappings. Die Mutation braucht die bestehende Identität. Diese Identität ist kein frei editierbares Nutzfeld. Das Schema allein belegt keine Partial-Updates und kein NULL-Löschen.

## Vollständigkeit der HERO-Eingabefelder für die Kernbereiche

Die Tabellen führen jedes Eingabefeld auf. „Nicht geplant“ heißt: Der Plan schlägt kein automatisches Ausgangsmapping vor. Ein Feld im Input belegt noch kein sicheres Update. `id` dient nur als geprüfte Quellidentität.

### CustomerInput

| HERO-Eingabefeld | Typ | ERPNext-Quelle / Entscheidung |
| --- | --- | --- |
| `user_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `type` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `title` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `title_custom` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `first_name` | `String` | Customer.first_name |
| `last_name` | `String` | Customer.last_name |
| `company_name` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `company_legal_form` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `phone_home` | `String` | Contact Phone.phone (Kandidat) |
| `phone_mobile` | `String` | Customer.mobile_no |
| `phone_fax` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `url` | `String` | Customer.website (Kandidat) |
| `address_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `reachability` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `source` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `position` | `String` | Contact.designation (Kandidat) |
| `category` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `company_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `created` | `DateTime` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `modified` | `DateTime` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `nr` | `String` | Customer.custom_hero_customer_number |
| `parent_customer_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `email` | `String` | Customer.email_id |
| `offer_options` | `JSON` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `is_deleted` | `Boolean` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `full_name` | `String` | Customer.customer_name |
| `phone_home_formatted` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `phone_mobile_formatted` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `is_invoice_recipient` | `Boolean` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `reachability_string` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `initial_name` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `category_name` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `contact_match_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `is_contact_person` | `Boolean` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `leitweg_nr` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `id` | `Int` | Identität aus HERO Integration Link |
| `address` | `AddressInput` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `partner_notes` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `birth_date` | `Date` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |

### ProjectMatchInput

| HERO-Eingabefeld | Typ | ERPNext-Quelle / Entscheidung |
| --- | --- | --- |
| `project_type` | `String` | Project.custom_hero_project_type |
| `measure_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `customer_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `address_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `company_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `company_branch_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `partner_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `current_project_match_status_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `marked_company` | `Boolean` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `marked_later` | `Boolean` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `created` | `DateTime` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `modified` | `DateTime` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `contact_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `name` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `partner_source` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `relative_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `partner_notes` | `String` | Project.custom_hero_partner_notes |
| `display_id` | `String` | Project.custom_hero_display_id |
| `project_nr` | `String` | Project.custom_hero_project_number |
| `volume` | `Float` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `project_title` | `String` | Project.project_name |
| `is_deleted` | `Boolean` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `project_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `id` | `Int` | Identität aus HERO Integration Link |
| `current_project_match_status` | `ProjectMatchStatusInput` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `project` | `ProjectInput` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `contact` | `CustomerInput` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `type_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `step_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `ppl_price` | `Float` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |

### TaskInput

| HERO-Eingabefeld | Typ | ERPNext-Quelle / Entscheidung |
| --- | --- | --- |
| `author_user_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `company_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `target_user_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `title` | `String` | Task.subject (Kandidat) |
| `comment` | `String` | Task.description (Kandidat) |
| `target_project_match_id` | `Int` | Task.project über Link (Kandidat) |
| `due_date` | `DateTime` | externes Fälligkeitsfeld (Kandidat) |
| `done_date` | `DateTime` | Abschluss nur nach Statusregel (Kandidat) |
| `created` | `DateTime` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `modified` | `DateTime` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `start` | `DateTime` | Task-Termin nach Semantikprüfung (Kandidat) |
| `end` | `DateTime` | Task-Termin nach Semantikprüfung (Kandidat) |
| `is_deleted` | `Boolean` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `id` | `Int` | Identität aus HERO Integration Link |

### CalendarEventInput

| HERO-Eingabefeld | Typ | ERPNext-Quelle / Entscheidung |
| --- | --- | --- |
| `category_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `project_match_id` | `Int` | Event.custom_hero_project über Link (Kandidat) |
| `title` | `String` | Event.subject (Kandidat) |
| `description` | `String` | Event.description (Kandidat) |
| `start` | `DateTime` | Event.starts_on (Kandidat) |
| `end` | `DateTime` | Event.ends_on (Kandidat) |
| `all_day` | `Boolean` | Event.all_day (Kandidat) |
| `deleted` | `Boolean` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `color` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `is_done` | `Boolean` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `is_recurring` | `Boolean` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `provider` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `id` | `Int` | Identität aus HERO Integration Link |
| `modified` | `DateTime` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `created` | `DateTime` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `partner_ids` | `[Int]` | Teilnehmer über Identitätsvertrag (Kandidat) |
| `resource_ids` | `[Int]` | Ressourcen über Identitätsvertrag (Kandidat) |
| `partner_id` | `Int` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `type` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |
| `localized_type` | `String` | Nicht geplant; Bedeutung, Besitz und Nebenwirkungen prüfen |

## Vorbereitung weiterer Bereiche

| Bereich | HERO-Quelle und konkrete Kandidaten | ERPNext-Zielentwurf | Ausgang / offene Entscheidung |
| --- | --- | --- | --- |
| Dokumente | `customer_documents`: id, nr, date, status_code, type, value, vat, currency, project_match_id, file_upload | zunächst eigenes Dokumentregister mit Project-Link und privatem File; Buchungsdokument nicht erraten | `upload_document`, `create_document` nur nach Dokumentart-, Steuer-, Status-, Datei- und Buchungsmodell; keine automatische Rechnungsbuchung |
| Zeiterfassung | `tracking_times`: id/uuid, project_match_id, partner_id, start, end, duration_in_seconds, comment, status_code | Timesheet und Time Log als Entwurf; Employee, Projekt und Activity Type über geprüfte Links | `update_tracking_time`; Mitarbeiteridentität, Zeitzone, Rundung, Freigabe und Duplikate klären |
| Bilder und Dateien | `file_uploads`: id/uuid, filename, size, type, url_download, image_category, is_deleted, file_upload_links | privates File mit berechtigtem Dokumentlink; Inhalt und Metadaten gesondert | `upload_image` / `associate_file_uploads`; Download-URL-Lebensdauer, MIME, Größe, Sicherheit und Besitz klären |
| Artikel | `supply_products`, `supply_product_versions`: Produkt-/Versions-ID, nr, base_data, unit, prices, vat, deletion | Item / Item Price nur nach Varianten-, Einheit-, Währungs-, Preislisten- und Steuervertrag | `create_supply_product_version` / `update_supply_product_version`; Inventur und Bewertung gesondert |
| Leistungen | `supply_services`: id, nr, name, description, unit_type, net_price_per_unit, vat_percent, quantity, time_minutes | nicht lagergeführter Item und Item Price als Kandidat | `create_supply_service` / `update_supply_service`; Einheit, Preis, Steuer, Kostenstelle und Rundung klären |
| Abwesenheiten | `absences`: id, partner_id, type, start, end, status, duration | HRMS Leave Application nur wenn HRMS vorhanden; sonst eingeschränkte Beobachtung | `create_absence` / `update_absence`; Employee-, Leave-Type-, Tagesgrenzen-, Datenschutz- und Genehmigungsmodell klären |

## Umsetzung und Abnahme

1. Bestand der Ziel-Site erfassen: aktiver Snapshot-Fingerprint, Mapping- und Serviceversionen, vorhandene Custom Fields. Historische Dokumente belegen keinen Laufzeitstand.
2. Kernbereiche als Entwurf pro Richtung anlegen. Jeder Eintrag nennt Feldtyp, Transform, Identitäts- und Konfliktregel. Jedes „prüfen“-Feld einzeln entscheiden. Fachlich unklare Ableitungen nicht aktivieren.
3. Projektlogbuch als eigenen Lesevertrag auf `project_histories(project_match_id, first/last, offset, orderBy, show_system_histories)` entwerfen. Inhalt: IDs pro Projekt und Quelle, `created` für Anzeige, `modified` für Aktualisierung, `custom_title`, `custom_text`, `author_name`, Sichtbarkeit. Reihenfolge, serverseitige Limits, Seitennavigation und Löschsemantik gegen den HERO-Vertrag prüfen. Ausgabe in der Timeline mit escaped Inhalt und Projekt-Leseberechtigung, ohne Duplikate.
4. Ausgabe-Mappings auf einzeln geprüfte Mutationen begrenzen. Preview, Konfliktbehandlung, idempotente Fehlerbehandlung und Readback testen. Anlegen und Aktualisieren getrennt behandeln.
5. Synthetische Tests: Identitätskollisionen, Wiederholung, Pagination bei Datenänderung, Null/fehlend, gemeinsam genutzte Kontakte/Adressen, Zeitzonen/DST, fehlende Rechte, Konflikt, Zeitüberschreitung und unklare Mutationsantwort. Frappe-Integrationstests nur in einer isolierten Test-Site ausführen. Der Plan erlaubt keine Live- oder Produktionsausführung.

## Nachprüfung des Mutation-Schemas und Testzugriffs

- `add_logbook_entry(logbook_entry: LogbookEntryInput)` liefert `History`. Pflichtfeld im Input ist `custom_text: String!`. `target` ist `HistoryTargetEnum` mit `project_match` oder `field_service_job`. Zusätzlich sind `target_id`, `target_project_match_id`, `type_code`, `target_users` und `role_visibility` möglich. Der Input enthält kein `custom_title`. Das Schema legt nicht fest, welche Ziel-ID-Kombination und Sichtbarkeit HERO verlangt.
- `update_customer_address(customer_address: CustomerAddressInput)` akzeptiert `id`, `customer_id`, `address_id` und verschachtelt `address: AddressInput`. `AddressInput` enthält u. a. `street`, `line_1`, `line_2`, `city`, `zipcode`, `country_id`, `state_id` und `id`. Offen sind die Präzedenz von `street` gegenüber `line_1` sowie Länder- und Regionsidentität.
- `update_tracking_time(tracking_time: Employees_TrackingTimeInput)` akzeptiert u. a. `id`, `uuid`, `project_match_id`, `partner_id`, `start`, `end`, `duration_in_seconds`, `comment`, `status_code` und `tracking_times_category_id`. Zusammenhänge und Bearbeitbarkeit sind unbestätigt.
- GraphQL-Abfragen und Mutationen laufen nur gegen ein ausdrücklich freigegebenes Testprojekt.
- Ablauf eines Schreibtests: Testprojekt lesend identifizieren, Ausgangswerte und Berechtigungen prüfen, eine eng begrenzte Änderung wählen, Mutation einmal ausführen, unabhängig zurücklesen. Nach einer unklaren Antwort nicht stillschweigend zurücksetzen. Logbucheinträge erzeugen bleibende Historie.

## Live-Test an einem Testprojekt

- Die konfigurierte Quellbindung war bestätigt. Die HERO-Abfrage `project_match(project_match_id: …)` lieferte das Testprojekt, nicht als gelöscht markiert.
- `project_histories` mit `first: 1`, `offset: 0` und `offset: 1`, `orderBy: "id"`, `show_system_histories: true` lieferte eine Zeile und danach keine. Die gelieferte Historie trug die ID des Testprojekts in `target_project_match_id` und `type = project_match`. Ein einzelner Eintrag belegt weder die Vollständigkeit größerer Paginierungen noch stabile Sortierung bei gleichzeitigen Änderungen.
- `update_project_match(project_match: {id, partner_notes})` lief einmal am Testprojekt. Die Antwort bestätigte die Projekt-ID. Ein unabhängiger Leseaufruf bestätigte den Testwert. Dieselbe Mutation stellte danach den ursprünglichen Wert wieder her; ein unabhängiger Readback bestätigte ihn. In beiden Readbacks blieben `project_title`, `project_nr`, `project_type`, `display_id` und `customer_id` unverändert. Diese Einzelbeobachtung belegt keine allgemeine Partial-Update-, Nebenwirkungs-, NULL- oder Nebenläufigkeitssemantik.
- Mutationen ändern den Änderungszeitpunkt und die serverseitige Historie des Projekts. Ein Zurücksetzen des Feldwerts stellt beide nicht wieder her.
- Der Test legte keinen Logbucheintrag an. Er mutierte keine Kunden, Kontakte, Aufgaben, Kalendereinträge oder weiteren Objekte. Für diese Bereiche bleiben Feldzuordnungen und Verhalten Entwurf.

## Quellen

- HERO-Schema: `Schema.txt`.
- `docs/mapping_matrix_1.0.md`, `docs/sync_handoff_1.0.md`.
- `hero_integration/services/frappe_writeback.py`, `hero_integration/sources/hero.py`, `hero_integration/services/migration.py`.
- Lokale ERPNext/Frappe v16 DocType-Definitionen für Customer, Contact, Address, Project, Task, Event, Item, Timesheet und File.
