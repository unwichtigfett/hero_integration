# RC16 Kontext- und Sicherheitsdiagnose

- **Datum:** 2026-10-21
- **Wirkung:** `READ_TARGET` plus dieser redigierte `WRITE_REPORT`; keine App-,
  Git-, Bench-, Datenbank-, Dienst- oder Konfigurationsänderung.
- **Ziel:** `/srv/hero-native/cli-fresh-isolated/frappe-bench-cli-fresh`,
  Site `hero-native-cli-fresh.localhost`, Web-Bind `127.0.0.1:18010`.
- **Keine HERO-Kommunikation, keine externe Mutation und keine Credentials.**

## Identität, Revision und Toolchain

Die lesende Zielprüfung lief als `hero-native` mit `HOME=/srv/hero-native` und
dem Bench-Arbeitsverzeichnis als CWD. Der Gunicorn-Prozess auf Port 18010 läuft
unter derselben Identität mit `HOME=/srv/hero-native` und einem `PATH`, der die
isolierten Node-/Shim- und Bench-Verzeichnisse enthält.

| Gegenstand | Befund |
| --- | --- |
| Ziel-App-HEAD | `84d6c5a050671398b149540b2c645c7dfe9f73e5` |
| Lokaler/Remote-Entwicklungsbranch | `release/v1-rc` auf `928a004fdc95c4f915e389a2a5f52f7bd54e5ee7` |
| Abweichung `84d6..928a004` | ausschließlich sechs Theme-/Desk-Testdateien; keine Server-/Hook-Änderung |
| Ziel-Working-Tree | ausschließlich bestehendes `hero_integration.egg-info/` und `__pycache__/` untracked; unverändert gelassen |
| Node | `/srv/hero-native/toolchains/node/bin/node`, ausführbar, `v24.22.0` |
| Corepack | `/srv/hero-native/toolchains/node/bin/corepack` -> Corepack 0.36.0 |
| Yarn | Shim `/srv/hero-native/toolchains/shims/yarn` -> Corepack-Yarn 1.22.22 |
| Bench Python | `env/bin/python` -> Python 3.14.8; Frappe 16.52.0; ERPNext 16.51.2 |

Der frühere Fehler `node: command not found` ist **`PATH_OR_CONTEXT_ERROR`**,
nicht `BINARY_MISSING`, `BROKEN_SYMLINK`, `PERMISSION_ERROR` oder belegte
`VERSION_INCOMPATIBILITY`: Die absoluten Programme existieren, sind
ausführbar und lösen sich im realen Dienst-PATH auf. In einem absichtlich auf
Systempfade verkürzten PATH löst `node` dagegen nicht auf. Gegenhypothese: Ein
späterer Build kann dennoch einen unabhängigen Fehler haben; das ist ohne
erneuten Build `UNVERIFIED`.

## Effektive Sicherheitswerte

Eine begrenzte Frappe-Leseabfrage ergab für die vier angefragten Felder
`(polling_mode, scheduled_sync_enabled, allow_graphql_mutation_execution,
allow_deletion) = (None, 0, 0, 0)`. Die Rohspeicherung enthält nur die beiden
expliziten Nullschalter; `polling_mode` und `allow_deletion` sind nicht als
Singles-Wert gesetzt. Das ist hier kein Schluss allein aus dem Schema: Die
gelesene Settings-Instanz liefert die obigen effektiven Feldwerte.

`services/polling.py:21-27` behandelt den leeren Modus mit Legacy-Schalter 0
als **OFF**. Damit sind automatischer HERO-Zugriff und automatische
Transferaktion effektiv aus. Die Mutation-Ausführung ist angefordert `0` und
damit gemäß `api.py:551-567` effektiv **DISABLED**. Der Löschschalter ist
effektiv **OFF**. Für diese Bench wurde kein Scheduler-Prozess gefunden; ein
sichtbarer `frappe schedule`-Prozess gehört zur anderen Bench
`/srv/hero-native/frappe-bench`. Selbst bei einem später gestarteten Scheduler
wäre der stündliche Einstieg bei Modus OFF ein Rückkehrpfad vor HERO-Zugriff
(`tasks.py:6-16`).

## Bestätigter Lösch-Hook-Befund

| Frage | Evidenz | Ergebnis |
| --- | --- | --- |
| Normaler Frappe-Löschpfad | installierte Frappe 16.52, `frappe/model/delete_doc.py:171-178` | ruft `doc.run_method("on_trash")` auf |
| Dispatcher | `frappe/model/document.py:1247-1269`, `1617-1666` | `run_method` verteilt den tatsächlichen Methodennamen an `doc_events` |
| `before_delete`-Dispatcher | gezielte Suche im installierten Framework-Python | keiner gefunden |
| App-Registrierung | `hero_integration/hooks.py:35-38` | registriert nur `before_delete` |
| App-eigener direkter Weg | `api.py:787-791` | GraphQL-Service-RPC ruft den Guard direkt vor `frappe.delete_doc` auf |
| übrige native Wege | Frappe REST/Client laufen über `frappe.delete_doc` (`client.py:547-569`, API v1/v2) | derzeit nicht durch den nicht ausgelösten App-Hook geschützt |
| Testabdeckung | keine App-Testdatei ruft Guard, `frappe.delete_doc`, `on_trash` oder `before_delete` für den Löschschutz auf | vorhandene Tests beweisen keinen echten Lösch-Dispatch |

Der Befund ist **CONFIRMED / FRAMEWORK_REQUIREMENT** für Frappe 16.52: Die
Hook-Behauptung in `hooks.py:19-21` ist für native direkte DocType-Löschwege
nicht erfüllt. Es wurde keine Löschung ausgeführt.

**Kleinste spätere Korrektur (nicht in diesem Schritt ausgeführt):** Für alle
`_HERO_OWNED_DOCTYPES` ausschließlich den registrierten Event-Namen von
`before_delete` auf `on_trash` ändern; die bestehende Guard-Funktion kann ihre
Signatur behalten. Der direkte RPC-Guard bleibt Defense in Depth. Danach auf
einer isolierten Testsite mit selbst angelegtem HERO-eigenen Dokument und
`allow_deletion=0` über `frappe.delete_doc` eine erwartete
`PermissionError`-Negativprüfung ausführen; nur der eigene Testdatensatz darf
bei einem getrennt autorisierten Positivtest mit Schalter ON gelöscht werden.

## Enger RC16-Abgleich

| Bereich | Befund | Status |
| --- | --- | --- |
| Quelloperationen / Runtime-Felder | `sources/hero.py:10-66` nutzt feste `CUSTOMER_QUERY_V1`/`PROJECT_QUERY_V1`, feste Feldlisten und `PAGE_SIZE=100`; der Reader übernimmt keine ausgewählte Snapshot-Operation oder Runtime-Feldauswahl. | offene Implementierungslücke |
| MONITOR | `polling.py:64-127` zählt Seiten und speichert nur technische Zählwerte. | offene Implementierungslücke: keine feldbezogene MONITOR-Änderungserkennung |
| Vorhandene Feldvergleichslogik | `change_detection.py:10-41` wird aus `migration.py:264-307` für den Import-/Upsert-Pfad aufgerufen; dort bestehen Baseline-/Konfliktregeln. | vorhanden, aber kein Aufrufpfad aus `run_monitor()` belegt |
| Frappe-Änderungstrigger | außer der fehlerhaften Löschregistrierung gibt es in `hooks.py` keine `on_update`/`after_insert`-Registrierung und keinen belegten ausgehenden Dokumentereignis-Auftrag. | offene Implementierungslücke |
| Löschschutz-Reichweite | RPC-Guard nur für GraphQL-Service-Löschen; der globale Hook verfehlt den Framework-Lifecycle. Keine weiteren app-eigenen `db.delete`, `ignore_on_trash` oder `frappe.delete_doc`-Wege gefunden. | bestätigt unvollständig |

## Frappe_Coding_Rules-Evidenz

Verwendet wurde der installierte Pluginstand `frappe-coding-rules` 1.1.0,
gezielt mit `frappe-code-review`, den Lifecycle-/Hook-Referenzen von
`frappe-app-dev` und dem Operating Contract. Der vorhandene Whole-App-Bericht
`FRAPPE_CODING_RULES_REVIEW.md` bezieht sich auf `928a004...`, ist statisch und
nennt bereits PA-001 bis PA-006. Die repo-lokale Regel liegt in `AGENTS.md`;
sie verlangt aufgabenbezogene Nutzung der verfügbaren Rules-Skills und trennt
Prüfung, Reparatur, Tests, Migration und Veröffentlichung. Dieser Bericht
ergänzt nur den konkreten installierten Frappe-16.52-Lifecycle-Befund; er ist
kein erneuter Voll-Audit.

## Nächster sicherer Schritt

Ein separat autorisierter, schmaler Quell-Patch für die Hook-Registrierung mit
dem beschriebenen isolierten Negativtest. Seine Wirkungsklasse wäre
`SOURCE_CHANGE` gefolgt von `ISOLATED_TEST`; danach müssen die betroffenen
Build-/Installations-/Deployment-Nachweise für den neuen Commit erneuert
werden.

## RC16 remediation follow-up — candidate `3457ae69a80419c7c633edbbc57cc370a4156f58`

This section supersedes the historical open-status conclusions above for the
listed paths. Work ran only in the owned isolated test bench
`/srv/hero-native/rc16-delete-test-20261021T221210Z-1205700/frappe-bench` as
`hero-native`, with the supplied isolated Node/Yarn toolchain and Bench 5.31.0.
No HERO request, credential, or external mutation was made.

| Area | RC16 correction and evidence |
| --- | --- |
| Deletion lifecycle | `on_trash` is registered for all app-owned DocTypes. `frappe.delete_doc()` with Allow deletion OFF raises the expected PermissionError; 2 native tests passed. |
| PA-001 | All listed write RPCs require POST. Static API contract and native Frappe dispatcher test passed. |
| PA-002 | `tabSingles` reservation uses an advisory first-use lock, durable row lock and fail-closed contention result. Two independent Frappe DB connections produced exactly one Running and one Blocked run. |
| PA-003 | A unique active audit reservation is committed before transport and finalized after it. Two independent connections produced exactly one active reservation; no transport was called. |
| PA-004 | Target, required relation and Link use one savepoint. A real Customer insertion with a synthetic relation failure left neither Customer nor Link. |
| PA-005/006 | Runtime consumes the retained polling controls; only OFFSET and a 1–120s timeout are accepted, page scans are bounded and repeated offsets fail safely. Focused contract tests passed. |
| Frappe change trigger | A real Customer `on_update` wrote a redacted technical observation only; no outbound HERO action was prepared. |
| Helpdesk | `integration_profiles()` returned `Helpdesk profile needs both ERPNext and Helpdesk.` and `DISABLED_NO_APPROVED_HERO_TICKET_CONTRACT`. |

Focused remote modules passed: `test_rc16_native_safety`, `test_deletion_hook`,
`test_api_method_contract`, `test_workspace_settings_contract`,
`test_polling_contract`, and `test_project_company_migration`. Redis port
17379 was started only for each test run and verified stopped afterward.

Publication, GitHub-only fresh installation, RC15 upgrade proof, deployment,
and authenticated browser acceptance are deliberately not claimed by this
section.

## GitHub fresh-install attempt — 2026-10-22

`bench` 5.31.0 was located under the isolated `hero-native` toolchain and
used from an owned RC16 workspace. A first initialisation exposed a Bench port
scan bug in a pre-existing sibling configuration; no global configuration was
changed. A nested isolated retry then correctly selected Python 3.14.8,
cloned Frappe v16 and ERPNext v16, and used `bench get-app` with
`https://github.com/unwichtigfett/hero_integration.git --branch release/v1-rc`.
The cloned HERO app resolved to `c99e5353851d19f20215b2d38f4cc70898a1e6f1`.

The proof stopped before `bench new-site`/`install-app`: neither `hero-native`
nor the SSH system root can authenticate to the local MariaDB root socket.
No database password or existing credential was inspected, no database/site
was created, and no fallback SQLite proof was substituted for the required
MariaDB-compatible evidence. Fresh installation, RC15 upgrade and deployment
therefore remain **NOT_STARTED** pending a scoped database bootstrap
prerequisite.
