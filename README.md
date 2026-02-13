# AI Load Test + Externer LLM Load Balancer v2

Dieses Projekt stellt zwei getrennte Komponenten bereit:

1. `LLM Load Tester` (Browser-UI, Port `8088`)
2. `LLM Load Balancer v2` (Produktiv-API + Visual Admin-UI, Port `8090`)

RabbitMQ/Scheduler/Worker wurden aus dem aktiven Deployment entfernt.

## Architektur (v2)

- Der Load Tester schickt Requests an eine frei konfigurierbare `API Base URL`.
- Der Load Balancer v2 bietet OpenAI-kompatible Endpunkte und verteilt Last auf mehrere vLLM/OpenAI-Backends (**Endpoints**).
- Das alte Pool/Target-Modell wurde durch ein flaches **Endpoint-Modell** ersetzt: Jeder Endpoint ist ein direktes Backend mit `base_url`, `api_key`, `name`, Routing-Parametern und Statistiken.
- Das Admin-UI ist ein **Visual Flow Editor** (n8n-Style) mit draggable Nodes und SVG-Bezier-Wires. Endpoints muessen per Wire mit dem "Incoming"-Node verbunden sein, um Traffic zu erhalten.
- Zwei Routing-Modi:
  - **Percentage (Prozentual)**: Token-Quota-basierte Verteilung. Jeder Endpoint hat ein `token_quota_percent`. Der Balancer steuert Traffic so, dass die tatsaechliche Token-Verteilung der Quote entspricht.
  - **Priority (Prioritaet)**: Primaer-Endpoint erhaelt immer Traffic. Overflow-Endpoints uebernehmen Restkapazitaet. Gesteuert via `priority_order` und `max_concurrent`.
- **Token Throughput Tracking**: Jede Antwort wird auf den `usage`-Block geparst. Pro Endpoint und global werden `prompt_tokens` / `completion_tokens` erfasst. Dashboard zeigt tok/s und req/s.
- **Batch-aware fuer vLLM**: Da vLLM intern Batching uebernimmt, verteilt der LB Einzel-Requests. Token-Durchsatz ist die primaere Metrik, nicht Request-Anzahl.
- Konfiguration und Runtime-Metriken werden persistent in SQLite gespeichert (`data/load_balancer.db`).

## Endpunkte

### Load Balancer API (Proxy)

- `POST /v1/chat/completions` – Chat-Completion-Proxy (routet an verbundene Endpoints)
- `POST /v1/{pool_name}/chat/completions` – Legacy-kompatibel (Pool-Name wird ignoriert, Routing via Endpoint-Logik)
- `GET /v1/models` – Aggregierte Modell-Liste aller verbundenen Endpoints
- `GET /v1/{pool_name}/models` – Legacy-kompatibel
- `GET /health` – Healthcheck mit Endpoint-Count, Connected-Count, Routing-Mode, tok/s, req/s

### Load Balancer Admin API

- `GET /admin` – Visual Flow Editor (HTML)
- `GET /admin/api/state` – Vollstaendiger State: Endpoints (mit Stats, Inflight, Cooldown), Routing-Mode, Incoming-Position, globale Stats
- `POST /admin/api/endpoints` – Endpoint anlegen (`name`, `base_url`, `api_key`, `enabled`, `connected`, `token_quota_percent`, `priority_order`, `max_concurrent`, `timeout`, `verify_tls`, `pos_x`, `pos_y`)
- `PATCH /admin/api/endpoints/{eid}` – Endpoint aktualisieren (Partial Update)
- `DELETE /admin/api/endpoints/{eid}` – Endpoint loeschen
- `PATCH /admin/api/routing` – Routing-Modus umschalten (`percentage` | `priority`)
- `POST /admin/api/reset-stats` – Alle Token-/Request-Statistiken zuruecksetzen
- `PATCH /admin/api/incoming-pos` – Position des Incoming-Nodes im Flow Editor speichern

## Schnellstart (Docker)

Vor dem Start:

1. `.env.example` nach `.env` kopieren.
2. `LB_ADMIN_TOKEN` und `LB_CLIENT_TOKENS` auf echte Werte setzen.

```bash
docker compose up -d --build
```

Danach:

- Load Tester: `http://localhost:8088`
- Load Balancer Admin (Visual Flow Editor): `http://localhost:8090/admin`
- Load Balancer Health: `http://localhost:8090/health`

Stoppen:

```bash
docker compose down
```

## Balancer im Load Tester verwenden

1. Im Balancer-Admin (`/admin`) mindestens einen Endpoint anlegen und per Wire mit dem Incoming-Node verbinden.
2. Routing-Modus waehlen (Percentage oder Priority).
3. Im Load Tester pro Karte setzen:
   - `API Base URL`: `http://localhost:8090/v1`
   - `API Key`: Client-Token aus `LB_CLIENT_TOKENS` (Bearer-Token)

Hinweis:
- Wenn `LB_CLIENT_TOKENS` leer ist, ist die Balancer-API ohne Client-Auth erreichbar.
- Fuer Produktion sollte `LB_CLIENT_TOKENS` gesetzt werden.
- Admin-API gibt API-Keys nicht im Klartext zurueck (nur `api_key_preview`).
- Endpoints muessen `connected: true` sein (Wire im Editor), um Traffic zu erhalten.

## Wichtige Umgebungsvariablen (Balancer)

| Variable | Beschreibung | Default |
|---|---|---|
| `LB_ADMIN_TOKEN` | Admin-API-Token (Pflicht in Produktion) | `change-me-admin-token` |
| `LB_CLIENT_TOKENS` | Comma-separated Client-Tokens fuer Proxy-API | (leer = offen) |
| `LB_DB_PATH` | Pfad zur SQLite-Datenbank | `./data/load_balancer.db` |
| `LB_PORT` | Listen-Port des Balancers | `8090` |
| `LB_FAIL_THRESHOLD` | Fehlversuche bis Cooldown | `3` |
| `LB_COOLDOWN_SECONDS` | Cooldown-Dauer in Sekunden | `20` |
| `LB_DEFAULT_TIMEOUT_SECONDS` | Globaler Timeout-Fallback | `120` |
| `LB_MAX_CONNECTIONS` | Max Connections im httpx-Pool | `500` |
| `LB_MAX_KEEPALIVE` | Max Keepalive-Connections | `200` |
| `LB_LOG_LEVEL` | Log-Level (DEBUG, INFO, WARNING, ERROR) | `INFO` |

Entfernt in v2: `LB_DEFAULT_POOL` (nicht mehr relevant, da kein Pool-Modell).

## Persistenz

- Balancer-Konfiguration + Statistiken: `data/load_balancer.db`
- Load-Tester Modell-Konfigurationen: weiterhin ueber `data/` (WebDAV) + LocalStorage Fallback.

## Security-Hinweise

- `LB_ADMIN_TOKEN` sofort auf einen starken Wert setzen.
- Balancer-Port nur intern freigeben oder per Reverse Proxy absichern.
- TLS-Verifikation pro Endpoint nur deaktivieren, wenn zwingend notwendig.
- Admin-API-State maskiert Secrets, zeigt keinen Klartext-API-Key (nur `api_key_preview`).

## Betriebszustand / Healthchecks

- Compose enthaelt Healthchecks fuer:
  - `load-balancer` (`/health`)
  - `ws-proxy` (TCP-Port-Check)
  - `load-tester` (`/` via Nginx)
- `/health` liefert zusaetzlich: `endpoints`, `connected`, `routing_mode`, `tokens_per_second`, `requests_per_second`, `total_inflight`.

## Tests

Automatisierte Tests liegen in `tests/test_load_balancer.py` (9 Tests) und decken ab:

- Admin-Auth (Token-Pruefung)
- Endpoint CRUD + Key-Masking
- Percentage-Routing (Token-Quota-Verteilung)
- Priority-Routing (Primaer + Overflow)
- Token Throughput Tracking (Usage-Parsing)
- Cooldown + Failover
- Streaming-Passthrough
- Persistenz ueber Neustart
- Model-Aggregation (mehrere Backends)

Ausfuehren:

```bash
pytest -q
```
