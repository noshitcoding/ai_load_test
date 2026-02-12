# AI Load Test

Web-basierter LLM-Load-Tester (OpenAI-kompatibel) für lokale und remote Endpoints wie **LiteLLM**, **vLLM** oder **Ollama**.

## Features

- Mehrere Modell-Karten gleichzeitig
- Pro Karte eigene API Base URL + API Key
- Priorität pro Request (`priority`)
- Batch Size + Concurrency + Delay + Request-Limit
- Detaillierte Metriken: TTFT, RPS, Tok/s, P95/P99, Prompt/Completion/Total Tokens
- Benchmark-Presets (Latency / Throughput / Priority-Nachweis)
- Speichern/Laden/Löschen von Benchmark-Profilen (LocalStorage)
- JSON Export/Import

## Schnellstart (Docker)

```bash
docker compose up -d
```

Dann im Browser öffnen:

- http://localhost:8088

Stoppen:

```bash
docker compose down
```

## Alternativ ohne Docker

```bash
./start.sh 8080
```

Dann öffnen:

- http://localhost:8080

## Typische Endpoints

- LiteLLM: `http://localhost:4000/v1`
- vLLM: `http://<host>:8102/v1`
- Ollama via LiteLLM empfohlen (statt direkt)

## Priority-Test (Key1 vs Key2)

1. Preset `Priority-Nachweis (Key1 vs Key2)` laden
2. Karte 1 mit Key1, Karte 2 mit Key2
3. Last laufen lassen (`Alle starten`)
4. Vergleichen in Dashboard + Modell-Vergleich:
   - TTFT
   - P95/P99
   - Tok/s
   - Erfolgsrate

Wenn Priorisierung aktiv ist, sollte Key1 unter Last konsistenter niedrigere Warte-/Antwortzeiten zeigen.

## Dateien

- `index.html` – komplette UI + Logik
- `docker-compose.yml` – Nginx Deployment auf Port 8088
- `nginx.conf` – SPA/No-Cache Config
- `start.sh` – lokaler HTTP-Start

## Hinweise

- Browser speichert Benchmark-Profile lokal (`localStorage`) pro Browser/Host.
- Für reproduzierbare Benchmarks Temperatur niedrig halten (z. B. `0.2`).
- Für Throughput-Tests mit vLLM sind höhere `Concurrency`/`Batch Size` sinnvoller als bei Ollama.
