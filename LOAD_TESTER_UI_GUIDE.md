# Load Tester UI Guide (React)

Diese Datei beschreibt die neue React-basierte Load-Tester-Oberflaeche (`index.html`) im Detail.

## Ziele der UI

- reproduzierbare Lasttests mit mehreren Karten
- saubere Trennung von Konfiguration, Laufsteuerung und Auswertung
- schnelle Fehlersuche ueber Request-Log und Event-Log
- klare Bedienung durch sortierte Dropdowns und ausfuehrliche `?`-Hilfetexte

## Layout und Bereiche

1. Kopfbereich
- WebSocket-Status (`verbunden`/`getrennt`)
- lokale Uhrzeit
- Tastaturkuerzel (`Ctrl+S` Start Alle, `Ctrl+X` Stop Alle)

2. Runs und Presets
- Preset-Auswahl nach Kategorien (`Baselines`, `Durchsatz`, `Vergleich`)
- globale Buttons fuer Start/Stop/Reset
- Karten hinzufügen

3. Profile und Modell-Konfiguration
- Benchmark-Profile (kompletter Kartensatz, lokal)
- Modell-Konfigurationen (einzelne Karte, `/data` + lokaler Fallback)
- gezieltes Laden auf eine aktive Karte

4. Session und Logs
- Session-Export/Import
- Event-Log-Export
- Log-Leerfunktionen
- manuelles WebSocket-Reconnect

5. Kartenbereich
- jede Karte kapselt ein Lastszenario
- jede Karte hat eigene Laufzeitmetriken, Fehlerliste und Recent-Latenz

6. Dashboard und Request-Log
- globale KPIs ueber alle Karten
- Histogramme fuer Latenz und Completion-Tokens
- sortierbare Vergleichstabelle
- detailliertes Request-Log mit Filtern

## Kartenfelder im Detail

### API und Modell
- `API Base URL`: Ziel-Basis, `.../v1` wird automatisch normalisiert
- `API Key`: Bearer-Token fuer Zielsystem
- `Modell`: Model-ID fuer `/v1/chat/completions`
- `Modelle laden`: fragt `/v1/models` ab und fuellt sortierte Vorschlagsliste

### Prompting
- `System Prompt`: optionaler Steuertext
- `User Prompt`: Hauptprompt je Request

### Laststeuerung
- `Requests`: Anzahl Requests pro Karte (`0` = Endlosschleife)
- `Max Parallel`: gleichzeitige In-Flight Requests (`0` = unbegrenzt)
- `Intervall ms`: Pause zwischen Dispatches
- `Max Tokens`: max Completion-Tokens
- `Temperatur`: Sampling-Temperatur
- `Prioritaet`: optionales Scheduling-Feld im Request-Body
- `Streaming`: SSE-Modus an/aus
- `TLS Verifikation`: nur fuer Test gegen self-signed Zertifikate deaktivieren
- `Batch Marker`: Kennzahl fuer spaetere Log-Auswertung

## Presets

### Baselines
- `Latenz-Baseline`: sequentieller Lauf mit niedrigem Rauschen
- `TTFB-Streaming`: Fokus auf Time-To-First-Byte mit SSE

### Durchsatz
- `Max Durchsatz`: aggressiver Dauerlauf mit hoher Parallelitaet
- `Parallel Scan`: mehrere Karten mit gestaffelter Parallelitaet

### Vergleich
- `Prioritaets Paar`: zwei identische Karten mit unterschiedlichen Prioritaetswerten

## Profile vs. Modell-Konfiguration

- Profil:
  - speichert alle Karten inklusive Reihenfolge
  - ideal fuer komplette Testsaetze
- Modell-Konfiguration:
  - speichert genau eine Karte
  - ideal fuer wiederverwendbare Einzelsetups

## Logs und Exporte

### Request-Log
Zeigt pro Request:
- Zeit, Karte, Modell
- Status
- Latenz, TTFB
- Prompt-/Completion-/Gesamttokens
- Tok/s
- Prioritaet, Batch, Sequenznummer
- gekuerzte Antwort-Preview

### Event-Log
Enthaelt strukturierte Lifecycle-Events:
- Kartenstart
- Kartenstopp
- Kartenende
- Preset- und Profil-Aktionen

## Technische Hinweise

- Die UI nutzt React 18 (UMD) + Babel Standalone zur Laufzeit.
- Die Bibliotheken liegen lokal unter `web_vendor/` und werden vom Nginx-Container mit ausgeliefert (kein externer CDN-Zwang im Laufzeitbetrieb).
- Requests werden primaer ueber den WS-Proxy (`/ws`) gesendet.
- Bei deaktivierter TLS-Verifikation (nicht-streaming) wird `/proxy-insecure` genutzt.
- Lokale Persistenz erfolgt ueber `localStorage`.
- Serverseitige Modellkonfigurationen liegen unter `/data`.

## Bekannte Betriebsregeln fuer stabile Benchmarks

1. Prompt, Modell und Max-Tokens zwischen Vergleichslaeufen konstant halten.
2. Pro Experiment nur wenige Parameter aendern (z. B. nur Parallelitaet).
3. Endlose Laeufe (`Requests=0`) nur mit klarer manueller Stop-Strategie nutzen.
4. Bei hoher Last auf Event-Log und Error-Liste achten, nicht nur auf Durchschnittswerte.

## Troubleshooting

### WebSocket bleibt getrennt
- pruefen, ob `load-producer-proxy` laeuft
- Browser neu laden
- Button `WS neu verbinden` nutzen

### `Modelle laden` liefert Fehler
- API Base URL testen
- API Key pruefen
- Bei self-signed TLS temporär `TLS Verifikation` deaktivieren

### Viele Fehler trotz niedriger Last
- Zielendpoint auf Rate-Limits und Timeouts pruefen
- Parallelitaet reduzieren
- Intervall erhoehen

### Werte wirken inkonsistent
- Profil neu laden
- Karte resetten
- bei Streaming beachten: TTFB ist nur im Stream-Modus sinnvoll
