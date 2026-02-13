# Anforderungsliste: Externer LLM Load Balancer v2 + Load Tester Integration

Stand: 13.02.2026

## 1. Zielbild

- Bereitstellung eines kundentauglichen, separaten Load-Balancer-Dienstes mit eigener API und Visual-Flow-Admin-UI (n8n-Style).
- Das alte Pool/Target-Modell ist durch ein flaches **Endpoint-Modell** ersetzt. Jeder Endpoint ist ein direktes vLLM/OpenAI-Backend.
- Zwei Routing-Modi: **Percentage** (Token-Quota-basiert) und **Priority** (Primaer + Overflow).
- Token Throughput Tracking als primaere Metrik (batch-aware fuer vLLM).
- Der Load Tester darf keine interne Queue-/RabbitMQ-Logik mehr enthalten.
- Alle Balancer-Konfigurationsdaten muessen persistent gespeichert werden.

## 2. Scope

- Im Scope:
  - OpenAI-kompatibler API-Proxy fuer Chat Completions und Models.
  - Balancing ueber mehrere Endpoints (direkte Backends, kein Pool-Konzept).
  - Visual Flow Editor (n8n-Style) mit draggable Nodes und SVG-Bezier-Wires zur Verwaltung von Endpoints.
  - Zwei Routing-Modi: Percentage (Token-Quota) und Priority (Primaer/Overflow).
  - Token Throughput Tracking (prompt_tokens, completion_tokens) pro Endpoint und global.
  - Dashboard mit tok/s und req/s Metriken.
  - Persistenz der Konfiguration und Laufzeitstatistiken in SQLite.
  - Containerisiertes Deployment mit separatem Port.
  - Automatisierte Testabdeckung (9 Tests) inkl. Routing-, Token-Tracking-, Persistenz- und Streaming-Faellen.

- Nicht im Scope:
  - Multi-Region Active/Active zwischen mehreren Balancer-Instanzen.
  - Externe IAM/SSO-Integration.
  - Verteilte Tracing-Backends (z.B. Jaeger, OTEL Collector).

## 3. Funktionale Anforderungen

### 3.1 Load Balancer API

- FR-001: Der Balancer MUSS `POST /v1/chat/completions` bereitstellen.
- FR-002: Der Balancer MUSS `POST /v1/{pool_name}/chat/completions` als Legacy-kompatiblen Pfad bereitstellen (Pool-Name wird ignoriert, Routing via Endpoint-Logik).
- FR-003: Der Balancer MUSS `GET /v1/models` bereitstellen (aggregiert ueber alle verbundenen Endpoints).
- FR-004: Der Balancer MUSS `GET /v1/{pool_name}/models` als Legacy-kompatiblen Pfad bereitstellen.
- FR-005: Der Balancer MUSS Streaming-Requests (`stream=true`) transparent weiterleiten.
- FR-006: Der Balancer MUSS non-streaming Antworten inklusive JSON-Body transparent weiterleiten.
- FR-007: Der Balancer MUSS pro Antwort Routing-Metadaten (`X-LB-Endpoint`) liefern.
- FR-008: Der Balancer MUSS relevante Rate-Limit-Header vom Upstream weiterreichen.

### 3.2 Balancing und Routing

- FR-009: Der Balancer MUSS Requests nur auf **verbundene** (`connected: true`) und aktivierte (`enabled: true`) Endpoints routen.
- FR-010: Der Balancer MUSS Endpoints in Cooldown temporaer vom Routing ausschliessen.
- FR-011: **Percentage-Modus**: Der Balancer MUSS Token-Quota-basierte Lastverteilung unterstuetzen (`token_quota_percent`). Traffic wird so gesteuert, dass die tatsaechliche Token-Verteilung der Quote entspricht.
- FR-011a: **Priority-Modus**: Der Balancer MUSS Priority-basiertes Routing unterstuetzen. Primaer-Endpoint (`priority_order`) erhaelt immer Traffic. Overflow-Endpoints uebernehmen Requests, wenn `max_concurrent` des Primaer-Endpoints erreicht ist.
- FR-012: Der Balancer MUSS bei Fehlern pro Endpoint Fail-Counter fuehren.
- FR-013: Der Balancer MUSS bei Erreichen des Failure-Schwellwerts Cooldown setzen.
- FR-014: Der Balancer MUSS nach erfolgreicher Antwort den Failure-Status zuruecksetzen.
- FR-015: Der Balancer MUSS den `usage`-Block jeder Antwort parsen und `prompt_tokens`/`completion_tokens` pro Endpoint erfassen (Token Throughput Tracking).
- FR-016: Der Balancer MUSS globale Metriken berechnen: `tokens_per_second`, `requests_per_second`, `total_inflight`.

### 3.3 Admin UI und Admin API

- FR-020: Es MUSS ein Browser-Admin-UI unter `/admin` als **Visual Flow Editor** (n8n-Style) bereitgestellt werden.
- FR-021: Endpoints MUESSEN als draggable Nodes im Flow Editor dargestellt werden.
- FR-022: Verbindungen (Wires) MUESSEN als SVG-Bezier-Kurven zwischen Incoming-Node und Endpoint-Nodes angezeigt werden.
- FR-023: Nur Endpoints mit aktiver Wire-Verbindung (`connected: true`) erhalten Traffic.
- FR-024: Endpoints MUESSEN angelegt, aktualisiert und geloescht werden koennen (`POST/PATCH/DELETE /admin/api/endpoints`).
- FR-025: Folgende Endpoint-Felder MUESSEN gepflegt werden koennen: `name`, `base_url`, `api_key`, `enabled`, `connected`, `token_quota_percent`, `priority_order`, `max_concurrent`, `timeout`, `verify_tls`, `pos_x`, `pos_y`.
- FR-026: Der Routing-Modus MUSS umschaltbar sein (`PATCH /admin/api/routing` mit `percentage` | `priority`).
- FR-027: Laufzeitinformationen (inflight, cooldown_remaining_s, Tokens, Erfolg, Latenz) MUESSEN pro Endpoint sichtbar sein.
- FR-028: Admin-Endpunkte MUESSEN per Admin-Token geschuetzt sein (`X-Admin-Token` Header).
- FR-028a: Admin-State/Endpoint-Responses duerfen API Keys nicht im Klartext zurueckgeben (nur `api_key_preview`).
- FR-029: Token-/Request-Statistiken MUESSEN zuruecksetzbar sein (`POST /admin/api/reset-stats`).
- FR-030: Position des Incoming-Nodes MUSS persistierbar sein (`PATCH /admin/api/incoming-pos`).

### 3.4 Load Tester Integration

- FR-040: Der Load Tester MUSS interne Queue-/Scheduler-Optionen entfernt haben.
- FR-041: Der Load Tester MUSS weiterhin OpenAI-kompatible Base-URLs verwenden koennen.
- FR-042: Der Load Tester MUSS mit Balancer-Base-URL (`/v1`) nutzbar sein.
- FR-043: RabbitMQ-Abhaengigkeit MUSS im aktiven Deployment entfernt sein.

## 4. Datenhaltung und Persistenz

- FR-050: Balancer-Konfiguration MUSS persistent in SQLite gespeichert werden.
- FR-051: Persistierte Daten MUESSEN Neustarts ueberleben.
- FR-052: Folgende Entitaeten MUESSEN gespeichert werden: Endpoints (inkl. Routing-Parameter und Positionen), Settings (Routing-Mode, Incoming-Position), Endpoint-Statistiken (Tokens, Requests, Latenz).
- FR-053: Persistenzpfad MUSS per Umgebungsvariable konfigurierbar sein (`LB_DB_PATH`).

## 5. Security-Anforderungen

- NFR-001: Admin API MUSS Token-basiert abgesichert sein (`LB_ADMIN_TOKEN`).
- NFR-002: Client-API SOLL optional per Tokenliste absicherbar sein (`LB_CLIENT_TOKENS`).
- NFR-003: Upstream-API-Keys DUERFEN nur serverseitig gespeichert und genutzt werden.
- NFR-003a: API-Key-Klartext darf nicht ueber Admin-Read-Endpunkte exfiltrierbar sein (nur `api_key_preview`).
- NFR-004: TLS-Verifikation MUSS pro Endpoint konfigurierbar sein.
- NFR-005: Standardwerte fuer produktive Secrets DUERFEN nicht unveraendert bleiben.

## 6. Betriebs- und Deploy-Anforderungen

- NFR-006: Der Balancer MUSS als eigener Container laufen.
- NFR-007: Der Balancer MUSS auf separatem Port bereitgestellt werden (Default `8090`).
- NFR-008: Docker Compose MUSS nur noch relevante Services enthalten (Load Tester, WS Proxy, Load Balancer).
- NFR-009: RabbitMQ/Scheduler/Worker duerfen in der aktiven Compose-Topologie nicht mehr vorkommen.
- NFR-010: Health-Endpunkt MUSS vorhanden sein (`/health`) und Endpoint-Count, Connected-Count, Routing-Mode sowie tok/s und req/s liefern.
- NFR-010a: Container-Healthchecks fuer Balancer, WS-Proxy und Load-Tester SOLLEN aktiviert sein.

## 7. Performance- und Stabilitaetsanforderungen

- NFR-011: Der Balancer MUSS parallele Requests verarbeiten koennen.
- NFR-012: HTTP Connection Reuse MUSS aktiviert sein (Keepalive-Pool via httpx).
- NFR-013: Timeouts MUESSEN pro Endpoint konfigurierbar sein.
- NFR-014: Fehler einzelner Endpoints duerfen den Gesamtservice nicht sofort blockieren.
- NFR-015: Model-Liste SOLL aus mehreren verbundenen Endpoints aggregiert werden.
- NFR-016: Token Throughput Tracking MUSS performant ueber ein Time-Window (Sliding Window) erfolgen.

## 8. Testanforderungen

- TST-001: Admin-Auth (Token-Pruefung) muss automatisch getestet sein.
- TST-002: Endpoint CRUD + API-Key-Masking muss automatisch getestet sein.
- TST-003: Percentage-Routing (Token-Quota-Verteilung) muss automatisch getestet sein.
- TST-004: Priority-Routing (Primaer + Overflow) muss automatisch getestet sein.
- TST-005: Token Throughput Tracking (Usage-Parsing) muss automatisch getestet sein.
- TST-006: Cooldown + Failover muss automatisch getestet sein.
- TST-007: Streaming-Passthrough muss automatisch getestet sein.
- TST-008: Persistenz ueber Neustart muss automatisch getestet sein.
- TST-009: Model-Aggregation (mehrere Backends) muss automatisch getestet sein.
- TST-010: Testausfuehrung muss reproduzierbar lokal mit `pytest` erfolgen.

## 9. Abnahmekriterien

- AK-001: Im Load Tester sind keine Queue/Scheduler-Auswahlfelder mehr vorhanden.
- AK-002: `docker compose up -d --build` startet ohne RabbitMQ/Scheduler/Worker.
- AK-003: Admin-UI (Visual Flow Editor) ist unter `http://<host>:8090/admin` erreichbar.
- AK-004: Ein angelegter Endpoint wird nach Balancer-Neustart weiter angezeigt (Persistenz).
- AK-005: Load-Tester Requests ueber `http://<host>:8090/v1` funktionieren fuer stream/non-stream.
- AK-006: Bei Upstream-Fehlern wird ein Endpoint in Cooldown gesetzt und ein alternatives Endpoint verwendet.
- AK-007: Automatisierte Tests (9 Tests) laufen erfolgreich durch.
- AK-008: Admin-State liefert nur `api_key_preview` statt Klartext-Key.
- AK-009: Visual Flow Editor zeigt Endpoints als Nodes mit SVG-Bezier-Wires zum Incoming-Node.
- AK-010: Routing-Modus ist zwischen Percentage und Priority umschaltbar.
- AK-011: Token-Statistiken (tok/s, req/s) werden im Dashboard angezeigt.

## 10. Konfigurationsmatrix

| ID | Variable | Beschreibung | Default |
|---|---|---|---|
| ENV-001 | `LB_ADMIN_TOKEN` | Verpflichtend fuer produktiven Betrieb | `change-me-admin-token` |
| ENV-002 | `LB_CLIENT_TOKENS` | Optionale API-Schutzschicht (comma-separated) | (leer) |
| ENV-003 | `LB_DB_PATH` | Pfad zur SQLite-Datenbank | `./data/load_balancer.db` |
| ENV-004 | `LB_PORT` | Listen-Port des Balancers | `8090` |
| ENV-005 | `LB_FAIL_THRESHOLD` | Fehlversuche bis Cooldown | `3` |
| ENV-006 | `LB_COOLDOWN_SECONDS` | Cooldown-Dauer in Sekunden | `20` |
| ENV-007 | `LB_DEFAULT_TIMEOUT_SECONDS` | Globaler Timeout-Fallback | `120` |
| ENV-008 | `LB_MAX_CONNECTIONS` | Max Connections im httpx-Pool | `500` |
| ENV-009 | `LB_MAX_KEEPALIVE` | Max Keepalive-Connections | `200` |
| ENV-010 | `LB_LOG_LEVEL` | Log-Level (DEBUG, INFO, WARNING, ERROR) | `INFO` |

Entfernt in v2: `LB_DEFAULT_POOL` (nicht mehr relevant, da kein Pool-Modell).

## 11. Produktions-Checkliste

- OPS-001: Starken `LB_ADMIN_TOKEN` setzen.
- OPS-002: `LB_CLIENT_TOKENS` fuer Kundenbetrieb aktivieren.
- OPS-003: Reverse Proxy/WAF/IP-ACL vor den Admin-Endpunkt setzen.
- OPS-004: Backup-Strategie fuer `load_balancer.db` festlegen.
- OPS-005: Monitoring fuer `5xx`, `429`, Latenz, Cooldown-Ereignisse und Token-Durchsatz aktivieren.
- OPS-006: Rollout mit Canary/Smoke-Tests gegen produktive Ziel-APIs durchfuehren.
- OPS-007: Routing-Modus (Percentage vs. Priority) passend zum Einsatzszenario waehlen.
