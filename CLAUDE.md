# Linux-Prüfungsvorbereitung

Material zum Linux-Kurs (LPIC-orientiert). Was im Kurs behandelt wurde, steht in `themenliste.txt` – das ist maßgeblich für Prüfungsinhalte. Unterlagen in `unterlagen/`, `folien/`, `buecher/`.

Git-Repo: github.com/joerbel/mc-exam-tool. **Kursunterlagen und `themenliste.txt` dürfen nicht ins Repo** – die `.gitignore` arbeitet deshalb als Whitelist (nur `pruefung/`, `README.md`, `CLAUDE.md`, `.gitignore`). Versuche in `pruefung/attempts/` sind ebenfalls ausgeschlossen.

Die echte Prüfung: ca. 60 Minuten, auf Papier, überwiegend Multiple Choice („wähle die richtige“ / „wähle die x richtigen“) plus einige Freitextfragen.

## Prüfungsplattform (`pruefung/`)

Starten: `python3 pruefung/server.py` → http://127.0.0.1:8765 (nur Python-Standardbibliothek).
Prüfungsdateien validieren: `python3 pruefung/server.py --check`

### Prüfungen erstellen: `pruefung/exams/<id>.json`

Der Dateiname (ohne `.json`) ist die ID, z.B. `pp01.json`. Dateien mit `_` am Anfang werden ignoriert; `exams/_beispiel.json` zeigt alle Fragetypen.

Vorhanden: `pp01`–`pp05` (je 32 MC/Kurzantwort + 2 Freitext, 38 Punkte, jede Prüfung deckt alle Themen ab) und `training.json` mit `"trainingOnly": true` – solche Dateien erscheinen nicht in der Prüfungsliste, ihre Fragen aber im freien Training und in Zufallsprüfungen. Neue Fragen, die keine eigene Prüfung bilden, kommen dorthin (neue, eindeutige `id` vergeben).

```json
{
  "title": "Probeprüfung 1",
  "description": "optional",
  "durationMinutes": 60,
  "passPercent": 60,
  "questions": [ ... ]
}
```

Jede Frage: `id` (stabil halten, eindeutig pro Datei – Statistik und Fehler-Training hängen an `<datei>::<id>`), `type`, `topic`, `points` (Standard 1), `text`, optional `code` (wird als Codeblock angezeigt) und `explanation` (wird nach Abgabe angezeigt).

- `single`: `options` (Liste), `correct` (Index ab 0)
- `multi`: `options`, `correct` (Liste von Indizes). Die Oberfläche zeigt „Wählen Sie N Antworten“ mit N = Anzahl richtiger. Bewertung: alles oder nichts.
- `short`: Kurzantwort, automatisch bewertet. `accept` = Liste akzeptierter Antworten (Leerzeichen normalisiert); Einträge mit `re:` sind reguläre Ausdrücke (fullmatch). `caseSensitive` (Standard false), optional `placeholder`.
- `text`: Freitext. `modelAnswer` (Musterantwort), optional `keywords` (Stichworte, Alternativen mit `|`). Wird vom Nutzer selbst bewertet. Üblich: `points: 3`.

In `text`, `options`, `explanation` und `modelAnswer` gehen `` `code` ``, `**fett**`, Zeilenumbrüche und ```` ``` ````-Blöcke.

`topic` bitte nur aus dieser Liste nehmen, damit die Themenstatistik aussagekräftig bleibt:
Geschichte & Lizenzen · Shell-Grundlagen · Dateien anzeigen & Textwerkzeuge · Variablen & Umgebung · Befehlstypen · Editoren (vim, nano) · sudo & su · Hilfe & Dokumentation · History · Sonderzeichen & Quoting · Ein-/Ausgabe-Umleitung & Pipes · Benutzer & Gruppen · Dateirechte · Spezialrechte & ACLs · Dateitypen & Links · Dateisystem-Hierarchie · Partitionierung · Hardware · Logs (journalctl) · Prozesse · systemd & Dienste · Bootprozess · Paketverwaltung · Netzwerk · SSH

Nach dem Erstellen immer `python3 pruefung/server.py --check` laufen lassen.

### Versuche auswerten: `pruefung/attempts/<id>.json`

Prüfungen, Zufallsprüfungen (`examId: _zufall`) und Trainingssitzungen (`examId: _training`, eine Datei pro Sitzung, wird nach jeder Antwort fortgeschrieben) landen hier. Jeder Versuch enthält eine Kopie der Fragen (`questions`, inkl. Lösungen), `answers` (nach Frage-Key; bei single/multi sind das die Original-Indizes) und `results` (`points`, `max`, `status`: correct/wrong/partial/pending/unanswered). `points: null` = Freitext noch nicht bewertet. Punktsummen werden beim Laden neu berechnet.

Wenn der Nutzer um Auswertung bittet:
- Freitexte bewerten: in `results[key]` `points` setzen, `status` passend (correct/partial/wrong) und `"gradedBy": "claude"`.
- Feedback eintragen: `review.overall` (Gesamtfeedback, z.B. Schwächen und was zu wiederholen ist) und `review.questions[key]` (Anmerkung pro Frage). Beides wird in der Oberfläche angezeigt.
