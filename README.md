# mc-exam-tool

Lokale Lernplattform für Multiple-Choice-Probeprüfungen – gebaut zur Vorbereitung auf eine Linux-Prüfung (LPIC-orientierter Kurs), aber mit beliebigen Fragen nutzbar.

## Starten

```bash
python3 pruefung/server.py
```

Öffnet http://127.0.0.1:8765. Benötigt nur Python 3 (keine Abhängigkeiten).

## Funktionen

- **Probeprüfungen** im Papierformat mit 60-Minuten-Timer, Fragenübersicht und Markierungen; Entwürfe überstehen ein Neuladen.
- **Fragetypen:** eine richtige Antwort, mehrere richtige („Wählen Sie N“), Kurzantwort (automatisch bewertet, auch per Regex) und Freitext (Musterantwort + Stichwortcheck + Selbstbewertung).
- **Freies Training:** 1, 5, 10, 20 oder endlos viele Fragen mit sofortiger Auflösung, filterbar nach Thema und nach „zuletzt falsch“ / „noch nie beantwortet“. Tastatur: A–F wählen, Enter prüfen/weiter.
- **Zufallsprüfung** aus allen Fragen im Prüfungsmodus.
- **Statistik** pro Thema, Versuchshistorie.
- Versuche werden als JSON in `pruefung/attempts/` gespeichert (nicht im Repo) und können dort auch extern bewertet/kommentiert werden (`review`-Feld).

## Fragen

Prüfungen liegen als JSON in `pruefung/exams/` – Format siehe [CLAUDE.md](CLAUDE.md) und `pruefung/exams/_beispiel.json`. Prüfen mit:

```bash
python3 pruefung/server.py --check
```
