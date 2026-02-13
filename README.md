# AI Load Test + Externer LLM Load Balancer v3

> Enterprise-grade OpenAI-compatible load balancer with visual flow editor, Prometheus metrics, and token-based routing.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Quick Start](#quick-start)
4. [Load Balancer API Reference](#load-balancer-api-reference)
5. [Admin API Reference](#admin-api-reference)
6. [Configuration Reference](#configuration-reference)
7. [Monitoring & Observability](#monitoring--observability)
8. [Docker & Deployment](#docker--deployment)
9. [Security](#security)
10. [Testing](#testing)
11. [Troubleshooting](#troubleshooting)
12. [Load Producer UI](#load-producer-ui)

---

## Overview

This project provides two independent components:

| Component | Port | Description |
|---|---|---|
| **LLM Load Balancer v3** | `8090` | Production API proxy + Visual Admin UI |
| **LLM Load Producer** | `8088` | Browser-based load testing UI + WebSocket proxy |

The Load Balancer sits between your clients and one or more **vLLM / OpenAI-compatible backends** (called *Endpoints*).  It distributes requests using either **percentage-based** (token quota) or **priority-based** routing and exposes a rich **n8n-style visual flow editor** for administration.

### Key Features

- **OpenAI-compatible proxy** (`/v1/chat/completions`, `/v1/models`)
- **Two routing modes**: Percentage (token-quota) and Priority (primary + overflow)
- **Token throughput tracking**: Real-time tok/s and req/s via sliding windows
- **Visual Flow Editor**: Drag nodes, draw SVG Bezier wires, dark/light theme
- **Prometheus metrics** at `/metrics`
- **TTL response cache** with configurable size and TTL
- **Admin rate limiting** (configurable RPM)
- **Request body size enforcement** (configurable max)
- **CORS support** with configurable origins
- **Graceful shutdown** with connection draining
- **Audit logging** for all admin mutations
- **SQLite WAL** persistence (survives restarts)
- **Pydantic v2** input validation on all admin endpoints
- **35 automated tests** (pytest)
- **Docker multi-stage builds** with non-root users and healthchecks

---

## Architecture

```
                       +------------------+
                       |   Load Producer  |
                       |     :8088        |
                       +--------+---------+
                                |
                                v
+------------------------------------------------------------------+
|  nginx reverse proxy :8088 / :8090                               |
|  (gzip, security headers, WebSocket upgrade)                     |
+------------------------------------------------------------------+
         |                                       |
         v                                       v
+------------------+                 +-------------------------+
|  WS Proxy        |                 |  Load Balancer v3       |
|  (WebSocket      |                 |  :8090                  |
|   rewriting)     |                 |                         |
+------------------+                 |  /v1/chat/completions   |
                                     |  /v1/models             |
                                     |  /health  /metrics      |
                                     |  /admin  (Flow Editor)  |
                                     +--------+--------+-------+
                                              |        |
                               +--------------+--+  +--+--------------+
                               |  Endpoint A     |  |  Endpoint B     |
                               |  (vLLM/OpenAI)  |  |  (vLLM/OpenAI)  |
                               +-----------------+  +-----------------+
```

### Internal Components

| Component | Purpose |
|---|---|
| `SlidingWindow` | Time-bucketed event counting (tok/s, req/s) |
| `TTLCache` | In-memory response cache with max-size eviction |
| `RateLimiter` | Sliding-window rate limiter for admin API |
| `AuditLogger` | JSON audit trail for admin mutations |
| `LoadBalancerError` | Structured error hierarchy with codes |
| `RoutingEngine` | Core routing logic (percentage + priority) |

---

## Quick Start

### Prerequisites

- Docker & Docker Compose v2+
- (Optional) Python 3.12+ for local development

### 1. Clone & Configure

```bash
git clone <repo-url> && cd ai_load_test
```

Create a `.env` file (or copy `.env.example`):

```env
LB_ADMIN_TOKEN=my-strong-admin-token-here
LB_CLIENT_TOKENS=token-for-client-1,token-for-client-2
```

### 2. Start

```bash
docker compose up -d --build
```

### 3. Access

| Service | URL |
|---|---|
| Load Balancer Admin | `http://localhost:8090/admin` |
| Load Balancer Health | `http://localhost:8090/health` |
| Prometheus Metrics | `http://localhost:8090/metrics` |
| Load Producer | `http://localhost:8088` |

### 4. Configure Endpoints

1. Open the Admin UI at `http://localhost:8090/admin`
2. Click **+ New Endpoint** and enter your vLLM/OpenAI backend URL
3. Drag a **wire** from the Incoming node to the Endpoint node
4. Select your **routing mode** (Percentage or Priority)
5. In the Load Producer, set `API Base URL` to `http://localhost:8090/v1`

### 5. Stop

```bash
docker compose down
```

---

## Load Balancer API Reference

### Proxy Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/v1/chat/completions` | Client token | Chat completion proxy (routes to connected endpoints) |
| `POST` | `/v1/{pool_name}/chat/completions` | Client token | Legacy-compatible (pool name ignored) |
| `GET` | `/v1/models` | Client token | Aggregated model list from all connected endpoints |
| `GET` | `/v1/{pool_name}/models` | Client token | Legacy-compatible |

**Response Headers:**
- `X-LB-Endpoint` — Name of the endpoint that handled the request
- `X-LB-Version` — Load Balancer version
- `X-Request-ID` — Unique request identifier (auto-generated if not provided)

### System Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Health check with metrics |
| `HEAD` | `/health` | None | Lightweight health probe |
| `GET` | `/metrics` | None | Prometheus-format metrics |
| `GET` | `/` | None | Redirects to `/admin` |

**Health Response:**
```json
{
  "status": "ok",
  "version": "3.0.0",
  "endpoints": 3,
  "connected": 2,
  "routing_mode": "percentage",
  "tokens_per_second": 1250.5,
  "requests_per_second": 8.3,
  "total_inflight": 4
}
```

---

## Admin API Reference

All admin endpoints require the `X-Admin-Token` header.

### State & Dashboard

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin` | Visual Flow Editor (HTML) |
| `GET` | `/admin/api/state` | Full state: endpoints, routing, stats |

### Endpoint Management

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/api/endpoints` | Create endpoint |
| `PATCH` | `/admin/api/endpoints/{eid}` | Update endpoint (partial) |
| `DELETE` | `/admin/api/endpoints/{eid}` | Delete endpoint |

**Endpoint Fields:**

| Field | Type | Description |
|---|---|---|
| `name` | string | Display name |
| `base_url` | string | Backend URL (validated) |
| `api_key` | string | Upstream API key (write-only, returned as `api_key_preview`) |
| `enabled` | bool | Accept traffic when true |
| `connected` | bool | Wire connected to incoming node |
| `token_quota_percent` | float | Percentage mode: quota (0-100) |
| `priority_order` | int | Priority mode: lower = higher priority |
| `max_concurrent` | int | Max parallel requests |
| `timeout` | float | Per-endpoint timeout (seconds) |
| `verify_tls` | bool | Verify upstream TLS certificate |
| `pos_x`, `pos_y` | float | Canvas position in flow editor |

### Routing

| Method | Path | Description |
|---|---|---|
| `PATCH` | `/admin/api/routing` | Switch mode (`percentage` or `priority`) |

### Operations

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/api/reset-stats` | Reset all token/request statistics |
| `POST` | `/admin/api/cache/clear` | Clear the response cache |
| `POST` | `/admin/api/db/vacuum` | Run SQLite VACUUM |

### Client Tokens

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/api/tokens` | List client tokens |
| `POST` | `/admin/api/tokens` | Create client token |
| `PATCH` | `/admin/api/tokens/{tid}` | Update client token |
| `DELETE` | `/admin/api/tokens/{tid}` | Delete client token |

### Incoming Blocks

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/api/incoming-blocks` | List incoming blocks |
| `POST` | `/admin/api/incoming-blocks` | Create incoming block |
| `PATCH` | `/admin/api/incoming-blocks/{bid}` | Update incoming block |
| `DELETE` | `/admin/api/incoming-blocks/{bid}` | Delete incoming block |
| `PATCH` | `/admin/api/incoming-pos` | Save incoming node position |

---

## Configuration Reference

All settings are configured via environment variables.

### Core Settings

| Variable | Description | Default |
|---|---|---|
| `LB_ADMIN_TOKEN` | **Required.** Admin API authentication token | `change-me-admin-token` |
| `LB_CLIENT_TOKENS` | Comma-separated client tokens for proxy API | *(empty = open)* |
| `LB_DB_PATH` | Path to SQLite database | `./data/load_balancer.db` |
| `LB_PORT` | Listen port | `8090` |
| `LB_LOG_LEVEL` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` |

### Routing & Resilience

| Variable | Description | Default |
|---|---|---|
| `LB_FAIL_THRESHOLD` | Consecutive failures before cooldown | `3` |
| `LB_COOLDOWN_SECONDS` | Cooldown duration (seconds) | `20` |
| `LB_DEFAULT_TIMEOUT_SECONDS` | Global request timeout fallback | `120` |
| `LB_DRAIN_TIMEOUT_SECONDS` | Graceful shutdown drain timeout | `30` |

### Connection Pool

| Variable | Description | Default |
|---|---|---|
| `LB_MAX_CONNECTIONS` | Max connections in httpx pool | `500` |
| `LB_MAX_KEEPALIVE` | Max keepalive connections | `200` |

### Cache & Rate Limiting

| Variable | Description | Default |
|---|---|---|
| `LB_CACHE_MAX_ENTRIES` | Maximum cached responses | `5000` |
| `LB_CACHE_TTL_SECONDS` | Cache entry TTL (seconds) | `300` |
| `LB_ADMIN_RATE_LIMIT_RPM` | Admin API rate limit (requests/min) | `300` |

### Security

| Variable | Description | Default |
|---|---|---|
| `LB_CORS_ORIGINS` | Comma-separated CORS origins | `*` |
| `LB_MAX_REQUEST_BODY_BYTES` | Maximum request body size (bytes) | `10485760` (10 MB) |

---

## Monitoring & Observability

### Prometheus Metrics (`GET /metrics`)

The `/metrics` endpoint exposes Prometheus-compatible metrics:

```
# HELP lb_endpoint_requests_total Total requests per endpoint
# TYPE lb_endpoint_requests_total counter
lb_endpoint_requests_total{endpoint="gpt4-backend"} 1523

# HELP lb_endpoint_tokens_total Total tokens per endpoint
# TYPE lb_endpoint_tokens_total counter
lb_endpoint_tokens_total{endpoint="gpt4-backend",type="prompt"} 245000
lb_endpoint_tokens_total{endpoint="gpt4-backend",type="completion"} 89000

# HELP lb_endpoint_avg_latency_seconds Average latency per endpoint
# TYPE lb_endpoint_avg_latency_seconds gauge
lb_endpoint_avg_latency_seconds{endpoint="gpt4-backend"} 1.23
```

### Health Check (`GET /health`)

Returns `200 OK` with real-time metrics including:
- Endpoint count and connected count
- Current routing mode
- Global tok/s and req/s
- Total in-flight requests
- Version string

### Audit Logging

All admin mutations (create, update, delete) are logged with:
- Timestamp (ISO 8601)
- Action type
- Endpoint ID
- Changed fields

### Request Tracing

Every proxied request includes an `X-Request-ID` header:
- If the client sends one, it is preserved
- Otherwise, a UUID is generated automatically

---

## Docker & Deployment

### Multi-Stage Builds

The Load Balancer Dockerfile uses a multi-stage build:
- **Builder stage**: Installs dependencies into a virtual environment
- **Runtime stage**: Copies only the venv + app code (smaller image, no build tools)

### Security Hardening

| Feature | Description |
|---|---|
| Non-root user | All containers run as `appuser` (UID 1000) |
| `no-new-privileges` | Prevents privilege escalation via `security_opt` |
| Resource limits | Memory capped at 512 MB per container |
| Healthchecks | All containers have Docker `HEALTHCHECK` instructions |
| JSON logging | Docker logging driver with rotation (10 MB, 3 files) |

### nginx Configuration

- **Gzip compression** for text, JSON, JavaScript, CSS, XML, SVG
- **Security headers**: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`
- **Body size limit**: `client_max_body_size 10m`
- **WebSocket upgrade** support for the WS proxy

### Docker Compose Services

| Service | Image | Port | Description |
|---|---|---|---|
| `load-balancer` | Custom (multi-stage) | 8090 | Load Balancer + Admin UI |
| `load-producer` | nginx:alpine | 8088 | Static load producer UI |
| `load-producer-proxy` | Custom | — | WebSocket proxy (internal) |

---

## Security

### Authentication

- **Admin API**: Protected by `X-Admin-Token` header (set via `LB_ADMIN_TOKEN`)
- **Client API**: Optionally protected by Bearer tokens (set via `LB_CLIENT_TOKENS`)
- **API key masking**: Admin state never returns plaintext API keys (only `api_key_preview`)

### Security Headers

Every response includes:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-Request-ID: <uuid>`

### Best Practices

1. **Set `LB_ADMIN_TOKEN`** to a strong random value (>= 16 characters)
2. **Set `LB_CLIENT_TOKENS`** in production to restrict proxy access
3. **Restrict network access** to the balancer port (use internal Docker networks or firewall rules)
4. **Enable TLS verification** on endpoints unless absolutely necessary to disable
5. **Configure `LB_CORS_ORIGINS`** to limit allowed origins in production

---

## Testing

### Running Tests

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run all 35 tests
pytest -v

# Run with short traceback
pytest -v --tb=short

# Run a specific test
pytest tests/test_load_balancer.py::test_health_endpoint -v
```

### Test Coverage

The test suite (`tests/test_load_balancer.py`) covers **35 tests** across these categories:

| Category | Tests | Description |
|---|---|---|
| Health & Monitoring | 3 | GET/HEAD health, Prometheus metrics |
| Security | 3 | Admin auth, security headers, X-Request-ID |
| Endpoint CRUD | 2 | Create + key masking, input validation |
| Routing | 3 | Percentage, priority, no-endpoints 503 |
| Token Management | 3 | Token tracking, client CRUD, incoming blocks |
| Operations | 3 | Reset stats, cache clear, DB vacuum |
| System | 4 | Root redirect, admin HTML, 404, version constant |
| Unit Tests | 5 | SlidingWindow, TTLCache, RateLimiter |
| Integration | 9 | Streaming, persistence, cooldown, model aggregation |

---

## Troubleshooting

### Common Issues

**Container won't start / unhealthy:**
```bash
docker compose logs load-balancer
```
Check that `LB_DB_PATH` points to a writable directory and the data volume is mounted.

**Admin UI shows "Connection lost":**
- Verify the load balancer container is running: `docker compose ps`
- Check the admin token is correct in the UI header field
- Look for rate limiting (429) in browser DevTools Network tab

**Requests return 503:**
- Ensure at least one endpoint is **enabled** and **connected** (wire drawn)
- Check endpoint health in the Admin UI (look for cooldown indicators)
- Verify `base_url` is reachable from the container network

**Slow responses / timeouts:**
- Check `LB_DEFAULT_TIMEOUT_SECONDS` and per-endpoint timeout settings
- Monitor in-flight count via `/health` or the Admin UI footer
- Consider increasing `LB_MAX_CONNECTIONS` for high-throughput scenarios

**"Token not authorized" errors:**
- If `LB_CLIENT_TOKENS` is set, requests must include `Authorization: Bearer <token>`
- Check for trailing whitespace in token values

### Resetting State

```bash
# Clear all stats (via API)
curl -X POST http://localhost:8090/admin/api/reset-stats \
  -H "X-Admin-Token: <your-token>"

# Clear response cache
curl -X POST http://localhost:8090/admin/api/cache/clear \
  -H "X-Admin-Token: <your-token>"

# Compact database
curl -X POST http://localhost:8090/admin/api/db/vacuum \
  -H "X-Admin-Token: <your-token>"

# Full reset (delete database)
docker compose down
rm data/load-balancer/load_balancer.db
docker compose up -d
```

---

## Load Producer UI

The Load Producer (`http://localhost:8088`) provides a browser-based load testing interface:

- Clean black/white UI with accent colors
- Sorted dropdowns for presets, profiles, model configs
- Per-card and global start/stop/reset controls
- Dashboard with request log and event log export
- Local React/Babel libraries (no CDN required at runtime)

**Connecting to the Load Balancer:**

1. In each card, set `API Base URL` to `http://localhost:8090/v1`
2. Set `API Key` to one of your `LB_CLIENT_TOKENS` values
3. Start the load test

See `LOAD_TESTER_UI_GUIDE.md` for detailed field documentation.

---

## License

Internal project — see repository for license details.
