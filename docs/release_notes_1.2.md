# HERO Integration 1.2.0

## Synchronisation und Migration

- Die bidirektionale Sync Engine verwendet freigegebene Reader und Mutationsmappings sowie Objekt- und Feldbindungen. Der Scheduler verteilt ein gemeinsames Datensatzbudget auf mehrere Reader und speichert den Fortschritt pro Reader.
- Kompatible HERO-Schemaänderungen können bestehende Freigaben über nachprüfbare Kompatibilitätsbelege weiterverwenden. Desk und Bench exportieren die portable Konfiguration mit der freigegebenen Vertragsherkunft.
- Feldbezogene Konflikte lassen sich einzeln prüfen und entscheiden. Persistente Nachverarbeitung, unabhängiges Rücklesen bei unklaren Mutationsergebnissen und Echo-Erkennung unterstützen den Wiederanlauf.
- Präzisere Vergleiche numerischer und nativer Feldwerte, Bindungsnachverarbeitung und Fortschrittssicherung verbessern Konsistenz und Durchsatz.
- „Archivierte Datensätze migrieren“ gilt für Test- und Vollmigration einschließlich Abhängigkeiten. Der Standard ist aus. Die Einstellung wird für jeden Lauf festgehalten und kann über die portable Konfiguration übertragen werden.

## Testbetrieb und Bedienung

- Der reguläre Reset in den HERO-Einstellungen entfernt eindeutig zugeordnete Testdaten und operative Testartefakte. Native Fremddaten, gemeinsam verwendete Dateien einschließlich Rich-Text-Referenzen, Konfiguration und Entwicklungsstand bleiben erhalten. Unklare Herkunftsfälle bleiben zur Prüfung erhalten. Snapshot- und Restore-Befehle gehören zum separat betriebenen Testcontroller.
- Frappe bleibt für Aufgabenhierarchien führend. Die HERO-Hauptaufgabe kann eine generierte Unteraufgabenübersicht erhalten. Der Rücksync behandelt manuelle Änderungen am generierten Abschnitt als Konflikt.
- Im Sync-Monitor zählt eine erfolgreich verarbeitete, budgetbegrenzte Runde als erfolgreiche Ausführung mit Teilabgleich. Fehler und Konflikte werden separat angezeigt.

## Bekannte Grenzen und offene Abnahmen

- Vollständige Bestandsrunden für Projekte und Dokumente sowie die Darstellung der Unteraufgaben im nativen HERO-Client sind separat abzunehmen.
- Ein richtungsspezifischer Mutationsimport benötigt auf dem gewählten Ziel-Snapshot freigegebene Readback-Reader. Ein Kompatibilitätsbeleg für die Runtime überträgt diese Importfreigabe nicht.
- Die Bearbeitung komplexer Mutationsparameter ist im grafischen Mapping-Editor eingeschränkt. Manuelle Synchronisation ist optional.
- Dev-spezifische Branding-, Ansichts- und Teamregeln werden als separates Overlay gepflegt. Verbindungsdaten und Zugangsdaten werden je Site konfiguriert.
