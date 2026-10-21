# Frappe-Arbeitsregeln

Bei Frappe-Aufgaben die tatsächlich verfügbaren Skills aus `Frappe_Coding_Rules`
gezielt nach Änderungsumfang und Risiko verwenden. Vor Implementierungen
bestehende Architektur, Schnittstellen und wiederverwendbare Funktionen prüfen;
nur die relevanten Regelabschnitte laden.

Für Entwicklung `frappe-app-dev`, für neue oder verschobene Dateien
`frappe-file-structure`, für betroffene APIs `frappe-api-conformance`, für
Änderungen `frappe-code-review` und bei UI- oder Dokumentationsarbeit die
passenden Spezialregeln verwenden. `/proof_app` dient einer ausdrücklich
angeforderten Gesamtprüfung; keine kleine Änderung startet automatisch einen
Vollaudit.

Prüfungen, Reparaturen, Tests, Migrationen und Veröffentlichung bleiben
getrennte Wirkungs- und Freigabebereiche. Projektverträge und bestätigte
Nutzerentscheidungen nicht durch allgemeine Empfehlungen ersetzen. Konflikte
anhand der konkreten Framework-Version und Quellen auflösen. Keine zusätzlichen
Abstraktionen, Endpoints oder Umstrukturierungen allein zur vermeintlichen
Regelkonformität erzeugen.
